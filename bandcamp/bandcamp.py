"""Bandcamp — search releases, artists and tracks, and read lyrics.

Design notes:

* Everything that talks to Bandcamp lives in :mod:`api` next to this file,
  which has no framework imports and resolves a Bandcamp URL through the
  search endpoint (the site answers a datacenter IP with a JavaScript
  challenge, so scraping a page for ids is not an option).
* Lists and detail pages are ``discord.ui.View``s with fixed ``custom_id``s,
  and a data-less placeholder view is registered in ``cog_load`` so a click on
  a button whose message is long gone gets an answer instead of a silent
  interaction timeout.
* An expected failure (nothing found, a link that does not resolve, a rate
  limit, a timeout) is an embed, never a raised exception — Red would print a
  traceback summary that reads like a broken bot.
* The commands are hybrid, so they work as ``[p]bandcamp …`` and as
  ``/bandcamp …``.
"""

from __future__ import annotations

import asyncio
import contextlib
import io
import logging
import time
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Dict, List, Optional, Sequence, Tuple

import aiohttp
import discord
from discord.ext import tasks
from redbot.core import Config, checks, commands
from redbot.core.bot import Red
from redbot.core.utils import bounded_gather

from .api import (
    DEFAULT_PLAY_MIB,
    KIND_ALBUM,
    KIND_ARTIST,
    KIND_TRACK,
    MAX_AUDIO_MIB,
    BandcampError,
    BandcampService,
    audio_filename,
    known_keys,
    collect_bandcamp_urls,
    format_date,
    format_discography_date,
    format_duration,
    format_price,
    format_tags,
    format_tracklist,
    new_releases,
    plural,
    rank_results,
    result_row,
    split_text,
    strip_markup,
    track_art_url,
    truncate,
)

log = logging.getLogger("red.freak_cogs.bandcamp")

DEFAULT_PER_PAGE = 10
MAX_PER_PAGE = 25
VIEW_TIMEOUT = 600
#: Embed budget for one page, leaving room for the footer inside Discord's 6000.
PAGE_BUDGET = 4800
#: A link posted twice within this window is only answered once.
AUTOEMBED_WINDOW = 300
AUTOEMBED_MAX_LINKS = 2

CID_SEARCH_PREV = "bandcamp:list_prev"
CID_SEARCH_NEXT = "bandcamp:list_next"
CID_SEARCH_PICK = "bandcamp:list_pick"
CID_PAGE_PREV = "bandcamp:page_prev"
CID_PAGE_NEXT = "bandcamp:page_next"
CID_BACK = "bandcamp:back"
CID_PLAY_TRACK = "bandcamp:play_track"
CID_PLAY_ONE = "bandcamp:play_one"
#: A select menu holds 25 options, so a longer tracklist needs a second menu.
PLAY_OPTIONS_PER_MENU = 25

#: Watchlist: how often the ticker runs, and what a guild may configure.
WATCH_TICK_MINUTES = 5
WATCH_MIN_INTERVAL = 15
WATCH_MAX_INTERVAL = 360
WATCH_DEFAULT_INTERVAL = 30
MAX_FOLLOWS = 50
MAX_ANNOUNCE_PER_BAND = 3

KIND_EMOJI = {KIND_ARTIST: "🎤", KIND_ALBUM: "💿", KIND_TRACK: "🎵"}

__red_end_user_data_statement__ = (
    "This cog stores guild settings only (results per page and the automatic "
    "link preview setting with its channel list) in Red's Config. Search "
    "queries and Bandcamp answers are cached in memory for up to 30 minutes and "
    "dropped when the bot restarts. Queries are sent to bandcamp.com; no "
    "Discord user data leaves the bot."
)


# --------------------------------------------------------------------------
# small helpers
# --------------------------------------------------------------------------


def _label(text: str, limit: int) -> str:
    """A masked-link label: escaped markdown, no newlines, length-capped."""
    cleaned = " ".join((text or "").split())
    return truncate(discord.utils.escape_markdown(cleaned, as_needed=False), limit)


def _plain(text: str, limit: int) -> str:
    """Text for inside a code span: no backticks, no newlines."""
    cleaned = " ".join((text or "").replace("`", "'").split())
    return truncate(cleaned, limit)


def _link(text: str, url: Optional[str], limit: int = 120) -> str:
    """``[label](url)``, or the plain label when there is no URL."""
    label = _label(text, limit)
    return f"[{label}]({url})" if url else label


def _embed_size(embed: discord.Embed) -> int:
    total = len(embed.title or "") + len(embed.description or "")
    if embed.footer and embed.footer.text:
        total += len(embed.footer.text)
    if embed.author and embed.author.name:
        total += len(embed.author.name)
    total += sum(
        len(field.name or "") + len(field.value or "") for field in embed.fields
    )
    return total


def _footer(kind: str, label: Optional[str] = None) -> str:
    """Provenance line: what the card is, and the label when there is one."""
    return f"Bandcamp · {kind} · {label}" if label else f"Bandcamp · {kind}"


def _stamp_pages(pages: Sequence[discord.Embed], footer: str) -> List[discord.Embed]:
    """Set one footer on every page, numbering them when there are several."""
    result: List[discord.Embed] = []
    for index, page in enumerate(pages, start=1):
        text = footer if len(pages) == 1 else f"{footer} · {index}/{len(pages)}"
        page.set_footer(text=truncate(text, 2000))
        result.append(page)
    return result


def _split_field_pages(
    base: discord.Embed,
    blocks: Sequence[Tuple[str, str]],
    *,
    budget: int = PAGE_BUDGET,
) -> List[discord.Embed]:
    """Spread ``(name, value)`` blocks over copies of ``base``, one page at a time."""
    pages: List[discord.Embed] = [base]
    current = base
    used = _embed_size(base)
    fields = len(base.fields)
    for name, value in blocks:
        if not value:
            continue
        value = truncate(value, 1024)
        cost = len(name) + len(value) + 10
        if fields >= 25 or (fields and used + cost > budget) or used + cost > 6000:
            current = base.copy()
            current.clear_fields()
            current.description = ""
            current.set_image(url=None)
            pages.append(current)
            used = _embed_size(current)
            fields = 0
        current.add_field(name=truncate(name, 250), value=value, inline=False)
        used += cost
        fields += 1
    return pages


def error_embed(
    title: str, detail: str, *, colour: Optional[discord.Colour] = None
) -> discord.Embed:
    """An expected failure (empty result, unresolved link, rate limit)."""
    return discord.Embed(
        title=truncate(title, 240), description=truncate(detail, 2000), colour=colour
    )


# --------------------------------------------------------------------------
# embed builders
# --------------------------------------------------------------------------


@dataclass
class Detail:
    """A rendered detail page plus what its playback controls may act on."""

    pages: List[discord.Embed]
    link: Optional[str] = None
    album: Optional[Any] = None
    track: Optional[Any] = None

    @property
    def playable(self) -> List[Any]:
        """The tracks the view offers to play."""
        if self.track is not None:
            return [self.track]
        return list(getattr(self.album, "tracks", None) or [])


def _artist_of(release: Any) -> Any:
    """The ``BCArtist`` of an album/track, or ``None``."""
    return getattr(release, "artist", None)


def _display_artist(release: Any) -> str:
    """The performer credit, falling back to the page owner."""
    return (
        getattr(release, "tralbum_artist", None)
        or getattr(_artist_of(release), "name", None)
        or "Unknown artist"
    )


def build_search_embed(
    rows: Sequence[Dict[str, Any]],
    *,
    query: str,
    page: int,
    pages: int,
    total: int,
    colour: discord.Colour,
) -> discord.Embed:
    """One page of search hits: a marker, a link and a metadata line each."""
    embed = discord.Embed(title=truncate(f"🔎 Bandcamp · {query}", 240), colour=colour)
    if not rows:
        embed.description = "Nothing on Bandcamp matched that."
    else:
        lines: List[str] = []
        for position, row in enumerate(rows, start=1):
            marker = KIND_EMOJI.get(row.get("kind", ""), "•")
            line = (
                f"`{position:02d}` {marker} "
                f"{_link(row.get('title') or 'Untitled', row.get('url'), 110)}"
            )
            subtitle = _plain(row.get("subtitle") or "", 120)
            if subtitle:
                line += f" — `{subtitle}`"
            lines.append(line)
        embed.description = "\n".join(lines)[:4096]
    embed.set_footer(
        text=truncate(
            f"page {page}/{max(1, pages)} · {plural(total, 'result')} · pick one below",
            2000,
        )
    )
    return embed


def build_discography_embed(
    rows: Sequence[Dict[str, Any]],
    *,
    artist_name: str,
    page: int,
    pages: int,
    total: int,
    colour: discord.Colour,
) -> discord.Embed:
    """One page of an artist's releases, pickable like a search result."""
    embed = discord.Embed(
        title=truncate(f"💿 {artist_name} · discography", 240), colour=colour
    )
    if not rows:
        embed.description = "This artist lists no releases on Bandcamp."
    else:
        lines: List[str] = []
        for position, row in enumerate(rows, start=1):
            line = f"`{position:02d}` {_plain(row.get('title') or 'Untitled', 110)}"
            subtitle = _plain(row.get("subtitle") or "", 120)
            if subtitle:
                line += f" — `{subtitle}`"
            lines.append(line)
        embed.description = "\n".join(lines)[:4096]
    embed.set_footer(
        text=truncate(
            f"page {page}/{max(1, pages)} · {plural(total, 'release')} · pick one below",
            2000,
        )
    )
    return embed


def build_album_embeds(
    album: Any, *, colour: discord.Colour, with_lyrics: bool = False
) -> List[discord.Embed]:
    """The release view: facts, tags, price, tracklist, credits."""
    artist = _artist_of(album)
    performer = _display_artist(album)
    embed = discord.Embed(
        title=truncate(getattr(album, "title", None) or "Untitled", 240),
        url=getattr(album, "url", None),
        colour=colour,
    )
    embed.set_author(
        name=truncate(performer, 240),
        url=getattr(artist, "url", None),
        icon_url=getattr(artist, "image_url", None),
    )
    about = strip_markup(getattr(album, "about", None))
    if about:
        embed.description = truncate(about, 700)
    art = getattr(album, "art_url", None)
    if art:
        embed.set_image(url=art)

    embed.add_field(
        name="Released",
        value=format_date(getattr(album, "release_date", None)),
        inline=True,
    )
    tracks = getattr(album, "tracks", None) or []
    total_tracks = getattr(album, "total_tracks", 0) or len(tracks)
    embed.add_field(name="Tracks", value=str(total_tracks), inline=True)
    embed.add_field(
        name="Price",
        value=format_price(
            getattr(album, "price", None),
            is_free=bool(getattr(album, "is_free", False)),
            is_set_price=bool(getattr(album, "is_set_price", False)),
            is_purchasable=bool(getattr(album, "is_purchasable", False)),
            is_preorder=bool(getattr(album, "is_preorder", False)),
        ),
        inline=True,
    )
    tags = format_tags(getattr(album, "tags", None))
    if tags:
        embed.add_field(name="Tags", value=truncate(tags, 400), inline=False)

    blocks: List[Tuple[str, str]] = []
    owner_name = getattr(artist, "name", None)
    if (
        getattr(album, "tralbum_artist", None)
        and owner_name
        and performer != owner_name
    ):
        blocks.append(
            (
                "Label release",
                f"Performed by **{_label(performer, 120)}** — published on the "
                f"{_link(owner_name, getattr(artist, 'url', None), 120)} page.",
            )
        )
    for index, chunk in enumerate(format_tracklist(tracks)):
        blocks.append(
            ("Tracklist" if index == 0 else f"Tracklist · {index + 1}", chunk)
        )
    credits = strip_markup(getattr(album, "credits", None))
    if credits:
        for index, chunk in enumerate(split_text(credits, 900)[:4]):
            blocks.append(
                ("Credits" if index == 0 else f"Credits · {index + 1}", chunk)
            )
    pages = _split_field_pages(embed, blocks)
    return _stamp_pages(pages, _footer("album", getattr(album, "label", None)))


def build_track_embeds(track: Any, *, colour: discord.Colour) -> List[discord.Embed]:
    """The track view: who, which release, how long, where to listen."""
    embed = discord.Embed(
        title=truncate(getattr(track, "title", None) or "Untitled", 240),
        url=getattr(track, "url", None),
        colour=colour,
    )
    artist = _artist_of(track)
    embed.set_author(
        name=truncate(_display_artist(track), 240),
        url=getattr(artist, "url", None),
        icon_url=getattr(artist, "image_url", None),
    )
    about = strip_markup(getattr(track, "about", None))
    if about:
        embed.description = truncate(about, 600)
    art = getattr(track, "art_url", None)
    if art:
        embed.set_image(url=art)

    album_title = getattr(track, "album_title", None)
    if album_title:
        # The track payload carries the album's title and id, not its slug, so
        # the title is shown without a link instead of inventing a URL.
        embed.add_field(name="Album", value=_label(album_title, 200), inline=True)
    number = getattr(track, "track_number", None)
    if number:
        embed.add_field(name="Track", value=f"#{int(number)}", inline=True)
    embed.add_field(
        name="Duration",
        value=format_duration(getattr(track, "duration", None)),
        inline=True,
    )
    if getattr(track, "has_lyrics", False):
        embed.add_field(
            name="Lyrics",
            value="available — send this track's link to `bandcamp lyrics`",
            inline=False,
        )
    credits = strip_markup(getattr(track, "credits", None))
    if credits:
        embed.description = truncate(
            "\n\n".join(part for part in (embed.description, credits) if part), 1200
        )
    return _stamp_pages([embed], _footer("track", getattr(track, "label", None)))


def build_artist_embeds(
    artist: Any,
    *,
    colour: discord.Colour,
    releases: Optional[Sequence[Dict[str, Any]]] = None,
) -> List[discord.Embed]:
    """The artist view: portrait, bio, tags and how much they published."""
    embed = discord.Embed(
        title=truncate(getattr(artist, "name", None) or "Unknown artist", 240),
        url=getattr(artist, "url", None),
        colour=colour,
    )
    embed.set_author(name="Label" if getattr(artist, "is_label", False) else "Artist")
    image = getattr(artist, "image_url", None)
    if image:
        embed.set_thumbnail(url=image)
    location = getattr(artist, "location", None)
    if location:
        embed.add_field(name="Location", value=truncate(location, 200), inline=True)
    genre = getattr(artist, "genre", None)
    if genre:
        embed.add_field(name="Genre", value=truncate(genre, 200), inline=True)
    if releases is not None:
        albums = sum(1 for row in releases if row.get("item_type", "album") == "album")
        singles = len(releases) - albums
        summary = plural(albums, "release")
        if singles:
            summary += f" · {plural(singles, 'track')}"
        embed.add_field(name="Discography", value=summary, inline=True)

    blocks: List[Tuple[str, str]] = []
    bio = strip_markup(getattr(artist, "bio", None))
    for index, chunk in enumerate(split_text(bio, 900)[:5]):
        blocks.append(("About" if index == 0 else f"About · {index + 1}", chunk))
    tags = format_tags(getattr(artist, "tags", None), limit=10)
    if tags:
        blocks.append(("Tags", truncate(tags, 700)))
    if releases:
        latest = [
            f"`{format_discography_date(row.get('release_date'))}` "
            f"{_plain(row.get('title') or '', 90)}"
            for row in releases[:6]
        ]
        blocks.append(("Latest releases", "\n".join(latest)))
    pages = _split_field_pages(embed, blocks)
    return _stamp_pages(pages, "Bandcamp · artist")


def build_lyrics_embeds(
    track: Any, lyrics: Optional[str], *, colour: discord.Colour
) -> List[discord.Embed]:
    """Lyrics of one track, split over as many embeds as the text needs."""
    text = (lyrics or "").replace("\r\n", "\n").strip()
    pages: List[discord.Embed] = []
    chunks = split_text(text, 3800) or [""]
    for index, chunk in enumerate(chunks):
        embed = discord.Embed(
            title=truncate(getattr(track, "title", None) or "Untitled", 240),
            url=getattr(track, "url", None),
            colour=colour,
        )
        embed.set_author(name=truncate(_display_artist(track), 240))
        if index == 0:
            artist = _artist_of(track)
            if getattr(artist, "image_url", None):
                embed.set_thumbnail(url=artist.image_url)
            art = getattr(track, "art_url", None)
            if art:
                embed.set_image(url=art)
        embed.description = chunk or "_Bandcamp lists no lyrics for this track._"
        pages.append(embed)
    return _stamp_pages(pages, "Bandcamp · lyrics")


def build_album_lyrics_embeds(
    album: Any, *, colour: discord.Colour
) -> List[discord.Embed]:
    """One lyric page per track that has lyrics."""
    tracks = [track for track in (getattr(album, "tracks", None) or []) if track.lyrics]
    if not tracks:
        return []
    pages: List[discord.Embed] = []
    total = len(tracks)
    for track in tracks:
        text = (track.lyrics or "").replace("\r\n", "\n").strip()
        chunks = split_text(text, 3800) or [""]
        for index, chunk in enumerate(chunks):
            embed = discord.Embed(
                title=truncate(track.title, 240),
                url=getattr(track, "url", None),
                colour=colour,
            )
            embed.set_author(
                name=truncate(
                    f"{_display_artist(track)} · {getattr(album, 'title', '')}", 240
                ),
                url=getattr(album, "url", None),
            )
            if index == 0 and getattr(album, "art_url", None):
                embed.set_thumbnail(url=album.art_url)
            embed.description = chunk
            pages.append(embed)
    footer = f"Bandcamp · {plural(total, 'track')} with lyrics · from {getattr(album, 'title', '')}"
    return _stamp_pages(pages, footer)


def build_release_announcement(
    item: Any, *, kind: str, colour: discord.Colour, band_name: str = ""
) -> List[discord.Embed]:
    """A new release as one card: facts and cover, not the whole tracklist.

    An announcement reuses the release card but keeps only its first page and
    replaces the footer, so a 20-track album does not arrive as four embeds.
    """
    if kind == KIND_TRACK:
        pages = build_track_embeds(item, colour=colour)
    else:
        pages = build_album_embeds(item, colour=colour)
    first = pages[0]
    first.set_footer(text=truncate(f"New on Bandcamp · {band_name}".strip(" ·"), 2000))
    return [first]


def build_row_announcement(
    row: Dict[str, Any], *, colour: discord.Colour, band_name: str = ""
) -> discord.Embed:
    """A discography row as a card, for a release whose details did not load."""
    embed = discord.Embed(
        title=truncate(row.get("title") or "New release", 240),
        colour=colour,
    )
    embed.set_author(
        name=truncate(band_name or row.get("band_name") or "Bandcamp", 240)
    )
    released = format_discography_date(row.get("release_date"))
    if released:
        embed.add_field(name="Released", value=released, inline=True)
    kind = row.get("item_type") or KIND_ALBUM
    embed.add_field(
        name="Type", value="track" if kind == KIND_TRACK else "release", inline=True
    )
    art = track_art_url(row.get("art_id"))
    if art:
        embed.set_image(url=art)
    embed.set_footer(text=truncate(f"New on Bandcamp · {band_name}".strip(" ·"), 2000))
    return embed


def _clamp_interval(value: Any) -> int:
    """A stored poll interval in minutes, inside the allowed range."""
    try:
        minutes = int(value)
    except (TypeError, ValueError):
        minutes = WATCH_DEFAULT_INTERVAL
    return max(WATCH_MIN_INTERVAL, min(WATCH_MAX_INTERVAL, minutes))


def role_mention(
    guild: Optional[discord.Guild], role_id: Any
) -> Tuple[Optional[str], bool]:
    """The mention text for a stored role id, and whether it means @everyone.

    ``Role.mention`` renders ``<@&guild-id>`` for the default role, which Discord
    shows as ``@@everyone`` and pings nobody, so that case becomes the literal
    text plus an allowed-mentions flag.
    """
    if guild is None or not role_id:
        return None, False
    try:
        number = int(role_id)
    except (TypeError, ValueError):
        return None, False
    if number == guild.id:
        return "@everyone", True
    role = guild.get_role(number)
    if role is None:
        return None, False
    return role.mention, False


def build_help_embed(prefix: str, colour: discord.Colour) -> discord.Embed:
    """What the cog does, in one card."""
    embed = discord.Embed(
        title="🎧 Bandcamp",
        description=(
            "Search Bandcamp's catalogue, open releases with their tracklist, "
            "walk an artist's discography and read lyrics — links work anywhere "
            "a search phrase works."
        ),
        colour=colour,
    )
    commands_text = [
        f"`{prefix}bandcamp search <query>` — artists, albums and tracks",
        f"`{prefix}bandcamp album <query|link>` — a release with tracklist",
        f"`{prefix}bandcamp track <query|link>` — one track",
        f"`{prefix}bandcamp artist <query|link>` — profile and release count",
        f"`{prefix}bandcamp discography <artist>` — every release, pickable",
        f"`{prefix}bandcamp play <query|link>` — post a track as a playable file",
        f"`{prefix}bandcamp lyrics <query|link>` — lyrics of a track or album",
        f"`{prefix}bandcamp follow <artist>` — announce that band's new releases",
        f"`{prefix}bandcamp following` — what this server watches",
        f"`{prefix}bandcamp link <url>` — whatever that link points at",
        f"`{prefix}bandcamp autoembed` — answer pasted links automatically",
    ]
    embed.add_field(name="Commands", value="\n".join(commands_text), inline=False)
    embed.set_footer(text="Data: bandcamp.com · alias: bc")
    return embed


# --------------------------------------------------------------------------
# views
# --------------------------------------------------------------------------


class PlaceholderView(discord.ui.View):
    """Answers clicks on components whose message is gone (restart, reload)."""

    MESSAGE = "That Bandcamp view has expired — run the command again."

    def __init__(self, cog: "Bandcamp") -> None:
        super().__init__(timeout=None)
        self.cog = cog
        for custom_id, label, style in (
            (CID_SEARCH_PREV, "‹ Previous", discord.ButtonStyle.secondary),
            (CID_SEARCH_NEXT, "Next ›", discord.ButtonStyle.secondary),
            (CID_PAGE_PREV, "‹ Previous", discord.ButtonStyle.secondary),
            (CID_PAGE_NEXT, "Next ›", discord.ButtonStyle.secondary),
            (CID_BACK, "‹ Back", discord.ButtonStyle.secondary),
            (CID_PLAY_ONE, "▶ Play", discord.ButtonStyle.success),
        ):
            button = discord.ui.Button(label=label, style=style, custom_id=custom_id)
            button.callback = self._expired
            self.add_item(button)
        select = discord.ui.Select(
            custom_id=CID_SEARCH_PICK,
            placeholder="Pick a result",
            options=[discord.SelectOption(label="—", value="none")],
            disabled=True,
        )
        select.callback = self._expired
        self.add_item(select)
        play = discord.ui.Select(
            custom_id=CID_PLAY_TRACK,
            placeholder="▶ Play a track",
            options=[discord.SelectOption(label="—", value="none")],
            disabled=True,
        )
        play.callback = self._expired
        self.add_item(play)

    async def _expired(self, interaction: discord.Interaction) -> None:
        with contextlib.suppress(discord.HTTPException):
            await interaction.response.send_message(self.MESSAGE, ephemeral=True)


class RowListView(discord.ui.View):
    """A paginated, pickable list: search hits or an artist's releases.

    The rows are already in memory, so paging is local — Bandcamp answers a
    search with at most 50 hits and a discography in one go, which is also why
    there is no ``source`` callback here.
    """

    def __init__(
        self,
        cog: "Bandcamp",
        *,
        rows: Sequence[Dict[str, Any]],
        render: Callable[[Sequence[Dict[str, Any]], int, int, int], discord.Embed],
        on_pick: Callable[[discord.Interaction, Dict[str, Any]], Awaitable[None]],
        per_page: int,
        placeholder: str = "Open a result",
        timeout: int = VIEW_TIMEOUT,
    ) -> None:
        super().__init__(timeout=timeout)
        self.cog = cog
        self.rows = list(rows)
        self.render = render
        self.on_pick_row = on_pick
        self.per_page = max(1, min(MAX_PER_PAGE, per_page))
        self.page = 1
        self.message: Optional[discord.Message] = None

        self._prev = discord.ui.Button(
            label="‹ Previous",
            style=discord.ButtonStyle.secondary,
            custom_id=CID_SEARCH_PREV,
        )
        self._prev.callback = self._on_prev
        self._next = discord.ui.Button(
            label="Next ›",
            style=discord.ButtonStyle.secondary,
            custom_id=CID_SEARCH_NEXT,
        )
        self._next.callback = self._on_next
        self._pick = discord.ui.Select(
            custom_id=CID_SEARCH_PICK,
            placeholder=placeholder,
            min_values=1,
            max_values=1,
            options=[discord.SelectOption(label="—", value="none")],
        )
        self._pick.callback = self._on_pick
        self.add_item(self._prev)
        self.add_item(self._next)
        self.add_item(self._pick)
        self._sync()

    @property
    def pages(self) -> int:
        return max(1, -(-len(self.rows) // self.per_page))

    @property
    def slice(self) -> List[Dict[str, Any]]:
        start = (self.page - 1) * self.per_page
        return self.rows[start : start + self.per_page]

    def embed(self) -> discord.Embed:
        return self.render(self.slice, self.page, self.pages, len(self.rows))

    def _sync(self) -> None:
        self._prev.disabled = self.page <= 1
        self._next.disabled = self.page >= self.pages
        options = [
            discord.SelectOption(
                label=truncate(row.get("title") or "Untitled", 100),
                description=truncate(row.get("subtitle") or "", 100),
                value=str(index),
            )
            for index, row in enumerate(self.slice)
            if row.get("title")
        ]
        self._pick.options = options or [discord.SelectOption(label="—", value="none")]
        self._pick.disabled = not options
        if not options:
            self._pick.placeholder = "Nothing to open"

    async def start(self, ctx: commands.Context) -> None:
        self.message = await ctx.send(embed=self.embed(), view=self)

    async def _show(self, page: int, interaction: discord.Interaction) -> None:
        self.page = max(1, min(self.pages, page))
        self._sync()
        await interaction.response.edit_message(embed=self.embed(), view=self)

    async def _on_prev(self, interaction: discord.Interaction) -> None:
        await self._show(self.page - 1, interaction)

    async def _on_next(self, interaction: discord.Interaction) -> None:
        await self._show(self.page + 1, interaction)

    async def _on_pick(self, interaction: discord.Interaction) -> None:
        raw = (self._pick.values or [""])[0]
        try:
            row = self.slice[int(raw)]
        except (ValueError, IndexError):
            await interaction.response.send_message(
                "That entry is gone — run the command again.", ephemeral=True
            )
            return
        await self.on_pick_row(interaction, row)

    async def on_timeout(self) -> None:
        for item in self.children:
            item.disabled = True
        if self.message is not None:
            with contextlib.suppress(discord.HTTPException):
                await self.message.edit(view=self)

    async def on_error(
        self,
        interaction: discord.Interaction,
        error: Exception,
        item: Optional[discord.ui.Item] = None,
    ) -> None:
        log.exception("Bandcamp list view failed", exc_info=error)
        await _answer_error(interaction)


class PageView(discord.ui.View):
    """A paginated detail view, optionally with a way back to a list."""

    def __init__(
        self,
        cog: "Bandcamp",
        pages: Sequence[discord.Embed],
        *,
        link: Optional[str] = None,
        back: Optional[Callable[[discord.Interaction], Awaitable[None]]] = None,
        play: Sequence[Any] = (),
        on_play: Optional[Callable[[discord.Interaction, Any], Awaitable[None]]] = None,
        extras: Sequence[discord.ui.Item] = (),
        timeout: int = VIEW_TIMEOUT,
    ) -> None:
        super().__init__(timeout=timeout)
        self.cog = cog
        self.pages = list(pages) or [discord.Embed(title="Nothing to show")]
        self.index = 0
        self.message: Optional[discord.Message] = None
        for item in extras:
            self.add_item(item)
        if play and on_play is not None:
            self._add_play_controls(list(play), on_play)
        self._prev: Optional[discord.ui.Button] = None
        self._next: Optional[discord.ui.Button] = None
        self._back = back
        if len(self.pages) > 1:
            self._prev = discord.ui.Button(
                label="‹ Previous",
                style=discord.ButtonStyle.secondary,
                custom_id=CID_PAGE_PREV,
            )
            self._prev.callback = self._on_prev
            self._next = discord.ui.Button(
                label="Next ›",
                style=discord.ButtonStyle.secondary,
                custom_id=CID_PAGE_NEXT,
            )
            self._next.callback = self._on_next
            self.add_item(self._prev)
            self.add_item(self._next)
        if back is not None:
            back_button = discord.ui.Button(
                label="‹ Results",
                style=discord.ButtonStyle.secondary,
                custom_id=CID_BACK,
            )
            back_button.callback = self._on_back
            self.add_item(back_button)
        if link:
            self.add_item(
                discord.ui.Button(
                    label="Open on Bandcamp", style=discord.ButtonStyle.link, url=link
                )
            )
        self._sync()

    def _add_play_controls(
        self,
        tracks: Sequence[Any],
        on_play: Callable[[discord.Interaction, Any], Awaitable[None]],
    ) -> None:
        """One Play button for a single track, select menus for a tracklist.

        A button per tracklist row is not possible: Discord caps a message at 25
        components, a label at 80 characters, and five components per row — so a
        13-track album would spend all five rows on numbered buttons whose
        meaning lives in the text above them. A menu carries the titles instead.
        """
        if len(tracks) == 1:
            track = tracks[0]

            async def play_one(interaction: discord.Interaction) -> None:
                await on_play(interaction, track)

            button = discord.ui.Button(
                label="▶ Play",
                style=discord.ButtonStyle.success,
                custom_id=CID_PLAY_ONE,
            )
            button.callback = play_one
            self.add_item(button)
            return
        for menu_index, start in enumerate(
            range(0, len(tracks), PLAY_OPTIONS_PER_MENU)
        ):
            chunk = list(tracks[start : start + PLAY_OPTIONS_PER_MENU])
            options: List[discord.SelectOption] = []
            for offset, track in enumerate(chunk):
                number = getattr(track, "track_number", 0) or start + offset + 1
                label = truncate(
                    f"{int(number):02d} · {getattr(track, 'title', '') or 'Untitled'}",
                    100,
                )
                description = format_duration(getattr(track, "duration", None))
                if not getattr(track, "streaming_url", None):
                    description += " · no preview"
                options.append(
                    discord.SelectOption(
                        label=label,
                        description=truncate(description, 100),
                        value=str(start + offset),
                    )
                )
            select = discord.ui.Select(
                custom_id=(
                    CID_PLAY_TRACK
                    if menu_index == 0
                    else f"{CID_PLAY_TRACK}_{menu_index}"
                ),
                placeholder="▶ Play a track" if menu_index == 0 else "▶ More tracks",
                options=options,
            )

            async def play_selected(
                interaction: discord.Interaction, *, menu: discord.ui.Select = select
            ) -> None:
                raw = (menu.values or [""])[0]
                try:
                    chosen = tracks[int(raw)]
                except (ValueError, IndexError):
                    await interaction.response.send_message(
                        "That track is gone — open the release again.", ephemeral=True
                    )
                    return
                await on_play(interaction, chosen)

            select.callback = play_selected
            self.add_item(select)

    @property
    def embed(self) -> discord.Embed:
        return self.pages[self.index]

    def _sync(self) -> None:
        if self._prev is not None:
            self._prev.disabled = self.index <= 0
        if self._next is not None:
            self._next.disabled = self.index >= len(self.pages) - 1

    async def _on_prev(self, interaction: discord.Interaction) -> None:
        self.index = max(0, self.index - 1)
        self._sync()
        await interaction.response.edit_message(embed=self.embed, view=self)

    async def _on_next(self, interaction: discord.Interaction) -> None:
        self.index = min(len(self.pages) - 1, self.index + 1)
        self._sync()
        await interaction.response.edit_message(embed=self.embed, view=self)

    async def _on_back(self, interaction: discord.Interaction) -> None:
        if self._back is not None:
            await self._back(interaction)

    async def on_timeout(self) -> None:
        for item in self.children:
            if not getattr(item, "url", None):
                item.disabled = True
        if self.message is not None:
            with contextlib.suppress(discord.HTTPException):
                await self.message.edit(view=self)

    async def on_error(
        self,
        interaction: discord.Interaction,
        error: Exception,
        item: Optional[discord.ui.Item] = None,
    ) -> None:
        log.exception("Bandcamp detail view failed", exc_info=error)
        await _answer_error(interaction)


async def _answer_error(interaction: discord.Interaction) -> None:
    """Tell the clicker that something broke, without raising further."""
    message = "That went wrong — check the bot log."
    with contextlib.suppress(discord.HTTPException):
        if interaction.response.is_done():
            await interaction.followup.send(message, ephemeral=True)
        else:
            await interaction.response.send_message(message, ephemeral=True)


# --------------------------------------------------------------------------
# cog
# --------------------------------------------------------------------------


class Bandcamp(commands.Cog):
    """Search Bandcamp, open releases and artists, and read lyrics."""

    def __init__(self, bot: Red) -> None:
        self.bot = bot
        self.config = Config.get_conf(
            self, identifier="bandcamp", force_registration=True
        )
        self.config.register_guild(
            per_page=DEFAULT_PER_PAGE,
            autoembed=False,
            autoembed_channels=[],
            play=True,
            play_mib=DEFAULT_PLAY_MIB,
            watch={},
            watch_channel=None,
            watch_role=None,
            watch_interval=WATCH_DEFAULT_INTERVAL,
        )
        self.session: Optional[aiohttp.ClientSession] = None
        self.service: Optional[BandcampService] = None
        #: ``(channel_id, url key)`` -> monotonic time of the last preview.
        self._recent: Dict[Tuple[int, str], float] = {}
        #: guild id -> monotonic time of the last watchlist poll.
        self._last_poll: Dict[int, float] = {}

    # -- lifecycle --------------------------------------------------------
    async def cog_load(self) -> None:
        self.session = aiohttp.ClientSession()
        self.service = BandcampService(self.session)
        with contextlib.suppress(Exception):
            self.bot.add_view(PlaceholderView(self))
        self._watch_tick.start()

    def cog_unload(self) -> None:
        """Drop the client and close its session.

        ``cog_unload`` is synchronous and Red never awaits it, so the close is
        scheduled on the running loop; without that guard a reload leaks one
        session per load.
        """
        if self._watch_tick.is_running():
            self._watch_tick.cancel()
        session = self.session
        self.session = None
        self.service = None
        if session is None or session.closed:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            log.debug("No running loop on unload; letting the session be collected")
            return
        loop.create_task(session.close())

    # -- helpers ----------------------------------------------------------
    @property
    def api(self) -> BandcampService:
        if self.service is None:  # pragma: no cover - only before cog_load
            raise RuntimeError("The Bandcamp client is not ready yet.")
        return self.service

    async def _per_page(self, guild: Optional[discord.Guild]) -> int:
        if guild is None:
            return DEFAULT_PER_PAGE
        value = await self.config.guild(guild).per_page()
        try:
            number = int(value)
        except (TypeError, ValueError):
            return DEFAULT_PER_PAGE
        return max(1, min(MAX_PER_PAGE, number))

    async def _defer(self, ctx: commands.Context) -> None:
        """Acknowledge before anything slow, so an interaction never expires."""
        if ctx.interaction is not None:
            if not ctx.interaction.response.is_done():
                with contextlib.suppress(discord.HTTPException):
                    await ctx.defer()
            return
        with contextlib.suppress(discord.HTTPException):
            await ctx.typing()

    def _view_for(
        self,
        detail: Detail,
        *,
        back: Optional[Callable[[discord.Interaction], Awaitable[None]]] = None,
    ) -> PageView:
        """The detail view, with playback controls when the release is playable."""
        return PageView(
            self,
            detail.pages,
            link=detail.link,
            back=back,
            play=detail.playable,
            on_play=self.play_track,
        )

    async def _send_pages(
        self,
        ctx: commands.Context,
        detail: Detail,
        *,
        back: Optional[Callable[[discord.Interaction], Awaitable[None]]] = None,
    ) -> None:
        """Post a detail view."""
        if not detail.pages:
            detail = Detail(
                [error_embed("Nothing to show", "Bandcamp answered empty.")]
            )
        view = self._view_for(detail, back=back)
        message = await ctx.send(embed=detail.pages[0], view=view)
        view.message = message

    async def _send_error(
        self,
        ctx: commands.Context,
        title: str,
        detail: str,
        colour: Optional[discord.Colour] = None,
    ) -> None:
        await ctx.send(embed=error_embed(title, detail, colour=colour))

    async def _detail_for(
        self, row: Dict[str, Any], *, colour: discord.Colour
    ) -> Detail:
        """Turn a list row into the embeds of its detail page."""
        resolved = await self.api.open_result(row)
        return await self._embeds_for_resolved(resolved, colour=colour)

    async def _embeds_for_resolved(
        self, resolved: Any, *, colour: discord.Colour
    ) -> Detail:
        kind, item = resolved.kind, resolved.item
        link = getattr(item, "url", None)
        if kind == KIND_ALBUM:
            return Detail(build_album_embeds(item, colour=colour), link, album=item)
        if kind == KIND_TRACK:
            return Detail(build_track_embeds(item, colour=colour), link, track=item)
        if kind == KIND_ARTIST:
            releases = None
            with contextlib.suppress(BandcampError):
                releases = await self.api.discography(item.id)
            return Detail(
                build_artist_embeds(item, colour=colour, releases=releases), link
            )
        return Detail(
            [
                error_embed(
                    "Unknown result",
                    "That is not something I can open.",
                    colour=colour,
                )
            ],
            None,
        )

    async def _open_row(
        self,
        interaction: discord.Interaction,
        row: Dict[str, Any],
        *,
        back: Optional[Callable[[discord.Interaction], Awaitable[None]]] = None,
    ) -> None:
        """Open a picked list row in the list's own message."""
        if not interaction.response.is_done():
            await interaction.response.defer()
        try:
            detail = await self._detail_for(
                row, colour=await self._colour(interaction.channel)
            )
        except BandcampError as error:
            await interaction.followup.send(
                embed=error_embed("Bandcamp did not answer", str(error)), ephemeral=True
            )
            return
        if not detail.pages:
            await interaction.followup.send(
                embed=error_embed("Nothing to show", "Bandcamp answered empty."),
                ephemeral=True,
            )
            return
        view = self._view_for(detail, back=back)
        message = await interaction.edit_original_response(
            embed=detail.pages[0], view=view
        )
        view.message = message

    async def _colour(self, location: Any = None) -> discord.Colour:
        """Red's embed colour for a channel or context, with a fallback.

        Red resolves the colour from a ``Messageable`` (a context, a channel, a
        message), so a view callback passes its interaction's channel rather
        than the guild — a guild has no ``.guild`` and always falls back.
        """
        if location is None:
            return discord.Colour.default()
        try:
            colour = await self.bot.get_embed_color(location)
        except Exception:  # pragma: no cover - defensive
            colour = None
        return colour or discord.Colour.default()

    # -- commands ---------------------------------------------------------
    @commands.hybrid_group(name="bandcamp", aliases=["bc"], invoke_without_command=True)
    @commands.bot_has_permissions(embed_links=True)
    async def bandcamp(self, ctx: commands.Context) -> None:
        """Search Bandcamp, open releases and artists, read lyrics."""
        prefix = ctx.clean_prefix
        await ctx.send(
            embed=build_help_embed(prefix, await self._colour(ctx)),
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @bandcamp.command(name="search")
    @commands.bot_has_permissions(embed_links=True)
    @commands.cooldown(4, 60, commands.BucketType.user)
    async def bandcamp_search(self, ctx: commands.Context, *, query: str) -> None:
        """Search artists, albums and tracks on Bandcamp.

        Pick an entry from the menu to open it; the arrows page through results.
        """
        await self._defer(ctx)
        colour = await self._colour(ctx)
        try:
            results = await self.api.search(query)
        except BandcampError as error:
            await self._send_error(ctx, "Bandcamp did not answer", str(error), colour)
            return
        rows = [result_row(item) for item in rank_results(results, query)]
        rows = [row for row in rows if row.get("title")]
        if not rows:
            await self._send_error(
                ctx,
                "Nothing found",
                f"Nothing on Bandcamp matched **{truncate(query, 120)}**.",
                colour,
            )
            return

        def render(
            page_rows: Sequence[Dict[str, Any]], page: int, pages: int, total: int
        ) -> discord.Embed:
            return build_search_embed(
                page_rows,
                query=query,
                page=page,
                pages=pages,
                total=total,
                colour=colour,
            )

        async def pick(interaction: discord.Interaction, row: Dict[str, Any]) -> None:
            await self._open_row(interaction, row, back=back)

        view = RowListView(
            self,
            rows=rows,
            render=render,
            on_pick=pick,
            per_page=await self._per_page(ctx.guild),
        )

        async def back(interaction: discord.Interaction) -> None:
            await interaction.response.edit_message(embed=view.embed(), view=view)

        await view.start(ctx)

    @bandcamp.command(name="album", aliases=["release"])
    @commands.bot_has_permissions(embed_links=True)
    @commands.cooldown(6, 60, commands.BucketType.user)
    async def bandcamp_album(self, ctx: commands.Context, *, query: str) -> None:
        """Open a release: tracklist, price, tags and credits.

        Takes a search phrase or a bandcamp.com album link.
        """
        await self._defer(ctx)
        colour = await self._colour(ctx)
        try:
            resolved = await self.api.resolve(query, kind=KIND_ALBUM)
            if resolved.kind != KIND_ALBUM:
                raise BandcampError("That is not an album or track link.")
            detail = await self._embeds_for_resolved(resolved, colour=colour)
        except BandcampError as error:
            await self._send_error(
                ctx, "Could not open that release", str(error), colour
            )
            return
        await self._send_pages(ctx, detail)

    @bandcamp.command(name="track")
    @commands.bot_has_permissions(embed_links=True)
    @commands.cooldown(6, 60, commands.BucketType.user)
    async def bandcamp_track(self, ctx: commands.Context, *, query: str) -> None:
        """Open a single track: release, length and where to listen."""
        await self._defer(ctx)
        colour = await self._colour(ctx)
        try:
            resolved = await self.api.resolve(query, kind=KIND_TRACK)
            if resolved.kind != KIND_TRACK:
                raise BandcampError("That is not a track link.")
            detail = await self._embeds_for_resolved(resolved, colour=colour)
        except BandcampError as error:
            await self._send_error(ctx, "Could not open that track", str(error), colour)
            return
        await self._send_pages(ctx, detail)

    @bandcamp.command(name="artist", aliases=["band"])
    @commands.bot_has_permissions(embed_links=True)
    @commands.cooldown(6, 60, commands.BucketType.user)
    async def bandcamp_artist(self, ctx: commands.Context, *, query: str) -> None:
        """Open an artist or label profile."""
        await self._defer(ctx)
        colour = await self._colour(ctx)
        try:
            resolved = await self.api.resolve(query, kind=KIND_ARTIST)
            if resolved.kind != KIND_ARTIST:
                raise BandcampError("That is not a band page.")
            detail = await self._embeds_for_resolved(resolved, colour=colour)
        except BandcampError as error:
            await self._send_error(
                ctx, "Could not open that artist", str(error), colour
            )
            return
        await self._send_pages(ctx, detail)

    @bandcamp.command(name="discography", aliases=["releases"])
    @commands.bot_has_permissions(embed_links=True)
    @commands.cooldown(4, 60, commands.BucketType.user)
    async def bandcamp_discography(self, ctx: commands.Context, *, query: str) -> None:
        """List every release of an artist, newest first."""
        await self._defer(ctx)
        colour = await self._colour(ctx)
        try:
            resolved = await self.api.resolve(query, kind=KIND_ARTIST)
            if resolved.kind != KIND_ARTIST:
                raise BandcampError("That is not a band page.")
            await self._send_discography(ctx, resolved.item, colour=colour)
        except BandcampError as error:
            await self._send_error(
                ctx, "Could not list that discography", str(error), colour
            )
            return

    async def _send_discography(
        self, ctx: commands.Context, artist: Any, *, colour: discord.Colour
    ) -> None:
        rows = discography_rows(await self.api.discography(artist.id))
        name = getattr(artist, "name", "Unknown artist")
        if not rows:
            await self._send_error(
                ctx,
                "No releases",
                f"{_label(name, 100)} lists no releases on Bandcamp.",
                colour,
            )
            return

        def render(
            page_rows: Sequence[Dict[str, Any]], page: int, pages: int, total: int
        ) -> discord.Embed:
            return build_discography_embed(
                page_rows,
                artist_name=name,
                page=page,
                pages=pages,
                total=total,
                colour=colour,
            )

        async def pick(interaction: discord.Interaction, row: Dict[str, Any]) -> None:
            await self._open_row(interaction, row, back=back)

        view = RowListView(
            self,
            rows=rows,
            render=render,
            on_pick=pick,
            per_page=await self._per_page(ctx.guild),
            placeholder="Open a release",
        )

        async def back(interaction: discord.Interaction) -> None:
            await interaction.response.edit_message(embed=view.embed(), view=view)

        await view.start(ctx)

    @bandcamp.command(name="lyrics")
    @commands.bot_has_permissions(embed_links=True)
    @commands.cooldown(4, 60, commands.BucketType.user)
    async def bandcamp_lyrics(self, ctx: commands.Context, *, query: str) -> None:
        """Read the lyrics of a track, or of every track of an album."""
        await self._defer(ctx)
        colour = await self._colour(ctx)
        try:
            resolved = await self.api.resolve(query)
            if resolved.kind == KIND_ARTIST:
                raise BandcampError(
                    "That is a band page — give me a track or an album instead."
                )
            if resolved.kind == KIND_TRACK:
                item = resolved.item
                if not getattr(item, "has_lyrics", False):
                    raise BandcampError(
                        f"**{truncate(item.title, 100)}** has no lyrics on Bandcamp."
                    )
                track, lyrics = await self.api.track_lyrics(item.artist.id, item.id)
                await self._send_pages(
                    ctx,
                    Detail(
                        build_lyrics_embeds(track, lyrics, colour=colour),
                        getattr(track, "url", None),
                    ),
                )
                return
            album = await self.api.album_lyrics(
                resolved.item.artist.id, resolved.item.id
            )
            pages = build_album_lyrics_embeds(album, colour=colour)
            if not pages:
                raise BandcampError(
                    f"**{truncate(album.title, 100)}** has no lyrics on Bandcamp."
                )
            await self._send_pages(ctx, Detail(pages, getattr(album, "url", None)))
        except BandcampError as error:
            await self._send_error(ctx, "No lyrics to show", str(error), colour)
            return

    @bandcamp.command(name="link", aliases=["open", "url"])
    @commands.bot_has_permissions(embed_links=True)
    @commands.cooldown(6, 60, commands.BucketType.user)
    async def bandcamp_link(self, ctx: commands.Context, *, url: str) -> None:
        """Open whatever a Bandcamp link points at."""
        await self._defer(ctx)
        colour = await self._colour(ctx)
        try:
            resolved = await self.api.resolve(url)
            detail = await self._embeds_for_resolved(resolved, colour=colour)
        except BandcampError as error:
            await self._send_error(ctx, "Could not open that link", str(error), colour)
            return
        await self._send_pages(ctx, detail)

    # -- watchlist --------------------------------------------------------
    @tasks.loop(minutes=WATCH_TICK_MINUTES)
    async def _watch_tick(self) -> None:
        """Poll the followed bands of every guild that is due."""
        await self.bot.wait_until_ready()
        for guild in list(self.bot.guilds):
            try:
                await self._poll_guild(guild)
            except Exception:  # pragma: no cover - a bad guild must not stop the rest
                log.exception("Bandcamp watch poll failed for guild %s", guild.id)

    async def _poll_guild(self, guild: discord.Guild, *, force: bool = False) -> int:
        """One guild's poll. Returns how many releases were announced."""
        settings = await self.config.guild(guild).all()
        watch = dict(settings.get("watch") or {})
        channel_id = settings.get("watch_channel")
        if not watch or not channel_id:
            return 0
        channel = guild.get_channel(int(channel_id))
        if channel is None:
            return 0
        if await self.bot.cog_disabled_in_guild(self, guild):
            return 0
        interval = _clamp_interval(settings.get("watch_interval"))
        now = time.monotonic()
        last = self._last_poll.get(guild.id, 0.0)
        if not force and last and now - last < interval * 60:
            return 0
        self._last_poll[guild.id] = now
        colour = await self._colour(channel)
        mention, everyone = role_mention(guild, settings.get("watch_role"))

        async def fetch(band_id: str) -> Tuple[str, Optional[List[Dict[str, Any]]]]:
            try:
                return band_id, await self.api.discography(band_id, fresh=True)
            except BandcampError as error:
                log.debug("Watch: could not read band %s: %s", band_id, error)
                return band_id, None

        results = await bounded_gather(*[fetch(band_id) for band_id in watch], limit=3)
        announced = 0
        changed = False
        for band_id, rows in results:
            if rows is None:
                continue
            entry = dict(watch.get(band_id) or {})
            fresh = new_releases(
                rows, entry.get("known") or [], limit=MAX_ANNOUNCE_PER_BAND
            )
            keys = known_keys(rows)
            if keys != list(entry.get("known") or []):
                entry["known"] = keys
                changed = True
            band_name = (
                entry.get("name")
                or (rows[0].get("band_name") if rows else "")
                or "Bandcamp"
            )
            for position, row in enumerate(fresh):
                first = position == 0 and announced == 0
                posted = await self._announce(
                    channel,
                    row,
                    band_name=band_name,
                    colour=colour,
                    mention=mention if first else None,
                    everyone=everyone and first,
                )
                announced += 1 if posted else 0
            watch[band_id] = entry
        if changed:
            await self.config.guild(guild).watch.set(watch)
        if announced:
            log.info(
                "Bandcamp watch announced %s release(s) in guild %s",
                announced,
                guild.id,
            )
        return announced

    async def _announce(
        self,
        channel: Any,
        row: Dict[str, Any],
        *,
        band_name: str,
        colour: discord.Colour,
        mention: Optional[str] = None,
        everyone: bool = False,
    ) -> bool:
        """Post one new release, falling back to the raw row if it will not load."""
        band_id = row.get("band_id")
        item_id = row.get("item_id")
        kind = KIND_TRACK if (row.get("item_type") or "") == KIND_TRACK else KIND_ALBUM
        detail: Detail
        try:
            if kind == KIND_TRACK:
                item = await self.api.track(band_id, item_id)
                detail = Detail(
                    build_release_announcement(
                        item, kind=kind, colour=colour, band_name=band_name
                    ),
                    getattr(item, "url", None),
                    track=item,
                )
            else:
                item = await self.api.album(band_id, item_id)
                detail = Detail(
                    build_release_announcement(
                        item, kind=kind, colour=colour, band_name=band_name
                    ),
                    getattr(item, "url", None),
                    album=item,
                )
        except BandcampError as error:
            log.debug("Watch: release %s did not load: %s", item_id, error)
            detail = Detail(
                [build_row_announcement(row, colour=colour, band_name=band_name)]
            )
        content = f"New from **{_label(band_name, 100)}**"
        if mention:
            content = f"{mention} {content}"
        view = self._view_for(detail)
        try:
            message = await channel.send(
                content=content,
                embed=detail.pages[0],
                view=view,
                allowed_mentions=discord.AllowedMentions(
                    everyone=everyone, roles=not everyone, users=False
                ),
            )
        except discord.HTTPException:
            log.debug("Watch: could not post the announcement for %s", item_id)
            return False
        view.message = message
        return True

    def _announcement_channel(self, ctx: commands.Context) -> Optional[Any]:
        """Where to announce when nothing is configured yet: this channel.

        A thread cannot be stored — polling resolves the id with ``get_channel``,
        which does not return threads — so its parent is taken instead. The
        channel is only used when the bot may actually post there.
        """
        channel: Any = getattr(ctx, "channel", None)
        parent = getattr(channel, "parent", None)
        if parent is not None and hasattr(parent, "permissions_for"):
            channel = parent
        if channel is None or not hasattr(channel, "permissions_for"):
            return None
        guild = getattr(channel, "guild", None) or getattr(ctx, "guild", None)
        me = getattr(guild, "me", None)
        if me is None:
            return None
        with contextlib.suppress(Exception):
            permissions = channel.permissions_for(me)
            if permissions.send_messages and permissions.embed_links:
                return channel
        return None

    async def _watch_summary(self, guild: discord.Guild) -> discord.Embed:
        """The current watchlist as a card."""
        settings = await self.config.guild(guild).all()
        watch = dict(settings.get("watch") or {})
        channel_id = settings.get("watch_channel")
        embed = discord.Embed(
            title="Bandcamp watchlist",
            description=(
                "New releases from followed artists and labels are posted "
                f"to {f'<#{channel_id}>' if channel_id else '**nowhere yet**'}."
            ),
            colour=await self._colour(guild),
        )
        if watch:
            entries = sorted(watch.items(), key=lambda item: item[1].get("name") or "")
            lines = [
                f"• [{_label(entry.get('name') or band_id, 90)}]"
                f"({entry.get('url') or 'https://bandcamp.com'}) · "
                f"{len(entry.get('known') or [])} releases known"
                for band_id, entry in entries[:20]
            ]
            if len(entries) > 20:
                lines.append(f"… and {len(entries) - 20} more")
            embed.add_field(
                name="Following", value="\n".join(lines)[:1024], inline=False
            )
        else:
            embed.add_field(
                name="Following",
                value="Nothing yet — an admin can add one with "
                "`bandcamp follow <artist>`.",
                inline=False,
            )
        role = settings.get("watch_role")
        embed.add_field(
            name="Ping", value=f"<@&{role}>" if role else "off", inline=True
        )
        embed.add_field(
            name="Interval",
            value=f"{_clamp_interval(settings.get('watch_interval'))} min",
            inline=True,
        )
        return embed

    # -- playback ---------------------------------------------------------
    async def _play_cap(self, guild: Optional[discord.Guild]) -> int:
        """Bytes this guild may upload for one track.

        The per-guild MiB setting is clamped to Discord's own bot ceiling of
        20 MiB and, when the guild allows less, to the guild's file size limit.
        """
        settings = {"play_mib": DEFAULT_PLAY_MIB}
        if guild is not None:
            settings = await self.config.guild(guild).all()
        try:
            mib = int(settings.get("play_mib") or DEFAULT_PLAY_MIB)
        except (TypeError, ValueError):
            mib = DEFAULT_PLAY_MIB
        mib = max(1, min(MAX_AUDIO_MIB, mib))
        cap = mib * 1024 * 1024
        if guild is not None:
            with contextlib.suppress(Exception):
                cap = min(cap, int(guild.filesize_limit))
        return cap

    async def play_track(self, target: Any, track: Any) -> None:
        """Post one track's preview MP3, which Discord turns into a player.

        A bot cannot put playable audio *inside* an embed — the player only
        exists for an uploaded file — so this posts the file as its own message
        next to the card that asked for it. ``target`` is either an
        ``Interaction`` (a menu pick or button) or a command context.
        """
        interaction = target if isinstance(target, discord.Interaction) else None
        guild = getattr(target, "guild", None)

        async def answer(text: str) -> None:
            """A short note, ephemeral when we can be."""
            with contextlib.suppress(discord.HTTPException):
                if interaction is not None:
                    await interaction.followup.send(text, ephemeral=True)
                else:
                    await target.send(text)

        if guild is not None:
            settings = await self.config.guild(guild).all()
            if not settings.get("play", True):
                await answer(
                    "Playback is switched off on this server — an admin can "
                    "enable it with `bandcamp set play on`."
                )
                return
        cap = await self._play_cap(guild)
        if interaction is not None:
            if not interaction.response.is_done():
                await interaction.response.defer(thinking=True)
        else:
            with contextlib.suppress(discord.HTTPException):
                await target.typing()
        artist_id = int(getattr(getattr(track, "artist", None), "id", 0) or 0)
        try:
            fresh, data, _content_type = await self.api.audio(
                artist_id, track.id, max_bytes=cap
            )
        except BandcampError as error:
            await answer(f"Could not load that track: {error}")
            return
        caption = (
            f"🎵 **{_label(fresh.title, 100)}** — "
            f"{_label(_display_artist(fresh), 100)} · "
            f"{format_duration(fresh.duration)} · "
            f"{len(data) / (1024 * 1024):.1f} MiB"
        )
        upload = {
            "content": caption,
            "file": discord.File(
                io.BytesIO(data),
                filename=audio_filename(_display_artist(fresh), fresh.title),
                description=f"{_display_artist(fresh)} — {fresh.title}",
            ),
        }
        try:
            if interaction is not None:
                await interaction.followup.send(**upload)
            else:
                await target.send(**upload)
        except discord.HTTPException:
            await answer(
                "Discord refused that upload — the bot may be missing "
                "**Attach Files**, or the server's file limit is lower than the track."
            )

    @bandcamp.command(name="play")
    @commands.bot_has_permissions(embed_links=True, attach_files=True)
    @commands.cooldown(2, 60, commands.BucketType.user)
    async def bandcamp_play(self, ctx: commands.Context, *, query: str) -> None:
        """Load one track's preview and post it as a playable file.

        Takes a search phrase or a Bandcamp link. The file is a 128 kbps preview
        of the track, not a download of the release.
        """
        await self._defer(ctx)
        colour = await self._colour(ctx)
        try:
            resolved = await self.api.resolve(query, kind=KIND_TRACK)
            if resolved.kind != KIND_TRACK:
                raise BandcampError(
                    "That is not a track — give me a track or its link."
                )
            track = resolved.item
        except BandcampError as error:
            await self._send_error(ctx, "Could not find that track", str(error), colour)
            return
        await self.play_track(ctx, track)

    # -- watchlist commands -----------------------------------------------
    @bandcamp.command(name="follow")
    @commands.guild_only()
    @commands.bot_has_permissions(embed_links=True)
    @checks.admin_or_permissions(manage_guild=True)
    @commands.cooldown(4, 60, commands.BucketType.guild)
    async def bandcamp_follow(self, ctx: commands.Context, *, query: str) -> None:
        """Follow an artist or label and announce their new releases."""
        await self._defer(ctx)
        colour = await self._colour(ctx)
        try:
            resolved = await self.api.resolve(query, kind=KIND_ARTIST)
            if resolved.kind != KIND_ARTIST:
                raise BandcampError("That is not a band or label page.")
            artist = resolved.item
            rows = await self.api.discography(artist.id)
        except BandcampError as error:
            await self._send_error(ctx, "Could not follow that", str(error), colour)
            return
        key = str(artist.id)
        watch = dict(await self.config.guild(ctx.guild).watch() or {})
        if key in watch:
            await ctx.send(
                f"Already following **{_label(artist.name, 100)}** — "
                f"`{ctx.clean_prefix}bandcamp following` lists the watchlist."
            )
            return
        if len(watch) >= MAX_FOLLOWS:
            await ctx.send(
                f"This server already follows {MAX_FOLLOWS} artists — unfollow one first."
            )
            return
        watch[key] = {
            "name": artist.name,
            "url": getattr(artist, "url", None),
            "image": getattr(artist, "image_url", None),
            "known": known_keys(rows),
            "followed": int(time.time()),
        }
        await self.config.guild(ctx.guild).watch.set(watch)
        settings = await self.config.guild(ctx.guild).all()
        channel_id = settings.get("watch_channel")
        adopted = False
        if not channel_id:
            fallback = self._announcement_channel(ctx)
            if fallback is not None:
                await self.config.guild(ctx.guild).watch_channel.set(fallback.id)
                channel_id = fallback.id
                adopted = True
        embed = discord.Embed(
            title=f"Following {_label(artist.name, 100)}",
            description=(
                f"**{len(watch[key]['known'])}** existing releases are recorded, "
                "so only genuinely new ones get announced."
            ),
            colour=colour,
        )
        if getattr(artist, "url", None):
            embed.url = artist.url
        if getattr(artist, "image_url", None):
            embed.set_thumbnail(url=artist.image_url)
        if channel_id:
            embed.add_field(
                name="Announcements",
                value=f"<#{channel_id}>" + (" — this channel" if adopted else ""),
                inline=True,
            )
        else:
            embed.add_field(
                name="Announcements",
                value=(
                    "nowhere yet — I cannot post embeds here, use "
                    f"`{ctx.clean_prefix}bandcamp notifications channel #channel`"
                ),
                inline=True,
            )
        embed.add_field(
            name="Interval",
            value=f"{_clamp_interval(settings.get('watch_interval'))} min",
            inline=True,
        )
        await ctx.send(embed=embed)

    @bandcamp.command(name="unfollow")
    @commands.guild_only()
    @checks.admin_or_permissions(manage_guild=True)
    @commands.cooldown(6, 60, commands.BucketType.guild)
    async def bandcamp_unfollow(self, ctx: commands.Context, *, query: str) -> None:
        """Stop watching an artist or label (name, link or id)."""
        watch = dict(await self.config.guild(ctx.guild).watch() or {})
        if not watch:
            await ctx.send("This server follows nothing yet.")
            return
        raw = (query or "").strip()
        key = raw if raw in watch else None
        if key is None:
            with contextlib.suppress(BandcampError):
                resolved = await self.api.resolve(raw, kind=KIND_ARTIST)
                if resolved.kind == KIND_ARTIST and str(resolved.item.id) in watch:
                    key = str(resolved.item.id)
        if key is None:
            await ctx.send(
                "That one is not on the watchlist — "
                f"`{ctx.clean_prefix}bandcamp following` shows what is."
            )
            return
        name = (watch.get(key) or {}).get("name") or key
        watch.pop(key, None)
        await self.config.guild(ctx.guild).watch.set(watch)
        await ctx.send(f"Unfollowed **{_label(name, 100)}**.")

    @bandcamp.command(name="following", aliases=["watchlist"])
    @commands.guild_only()
    @commands.bot_has_permissions(embed_links=True)
    async def bandcamp_following(self, ctx: commands.Context) -> None:
        """List the artists and labels this server follows."""
        await ctx.send(embed=await self._watch_summary(ctx.guild))

    @bandcamp.group(
        name="notifications", aliases=["notify"], invoke_without_command=True
    )
    @commands.guild_only()
    @commands.bot_has_permissions(embed_links=True)
    async def bandcamp_notifications(self, ctx: commands.Context) -> None:
        """Where and how often new releases are announced."""
        embed = await self._watch_summary(ctx.guild)
        embed.set_footer(
            text=(
                f"{ctx.clean_prefix}bandcamp notifications channel #channel · "
                f"role @role · interval <{WATCH_MIN_INTERVAL}-{WATCH_MAX_INTERVAL}> · "
                f"check"
            )
        )
        await ctx.send(embed=embed)

    @bandcamp_notifications.command(name="channel")
    @checks.admin_or_permissions(manage_guild=True)
    async def bandcamp_notifications_channel(
        self, ctx: commands.Context, channel: discord.TextChannel
    ) -> None:
        """Announce new releases in this channel."""
        permissions = channel.permissions_for(ctx.guild.me)
        if not (permissions.send_messages and permissions.embed_links):
            await ctx.send(
                f"I need **Send Messages** and **Embed Links** in {channel.mention}."
            )
            return
        await self.config.guild(ctx.guild).watch_channel.set(channel.id)
        followed = len(await self.config.guild(ctx.guild).watch() or {})
        await ctx.send(
            f"New releases will be announced in {channel.mention}"
            + (f" — {plural(followed, 'band')} followed." if followed else ".")
        )

    @bandcamp_notifications.command(name="off", aliases=["stop"])
    @checks.admin_or_permissions(manage_guild=True)
    async def bandcamp_notifications_off(self, ctx: commands.Context) -> None:
        """Stop announcing new releases (the watchlist stays)."""
        await self.config.guild(ctx.guild).watch_channel.set(None)
        await ctx.send("Announcements are **off** — the watchlist itself is kept.")

    @bandcamp_notifications.command(name="role")
    @checks.admin_or_permissions(manage_guild=True)
    async def bandcamp_notifications_role(
        self, ctx: commands.Context, role: Optional[discord.Role] = None
    ) -> None:
        """Ping a role with each announcement (`role none` clears it)."""
        if role is None:
            current = await self.config.guild(ctx.guild).watch_role()
            text = f"<@&{current}>" if current else "off"
            await ctx.send(
                f"Current ping: {text}. Give me a role, or `role none` to clear it."
            )
            return
        await self.config.guild(ctx.guild).watch_role.set(role.id)
        if role.id == ctx.guild.id:
            await ctx.send("Announcements will ping **@everyone**.")
        else:
            await ctx.send(f"Announcements will ping {role.mention}.")

    @bandcamp_notifications.command(name="interval")
    @checks.admin_or_permissions(manage_guild=True)
    async def bandcamp_notifications_interval(
        self, ctx: commands.Context, minutes: int
    ) -> None:
        """How often the watchlist is polled, in minutes (15-360)."""
        if not WATCH_MIN_INTERVAL <= minutes <= WATCH_MAX_INTERVAL:
            await ctx.send(
                f"Give me a number between {WATCH_MIN_INTERVAL} and {WATCH_MAX_INTERVAL}."
            )
            return
        await self.config.guild(ctx.guild).watch_interval.set(minutes)
        await ctx.send(f"The watchlist is polled every **{minutes} minutes**.")

    @bandcamp_notifications.command(name="check", aliases=["poll"])
    @checks.admin_or_permissions(manage_guild=True)
    @commands.cooldown(1, 120, commands.BucketType.guild)
    async def bandcamp_notifications_check(self, ctx: commands.Context) -> None:
        """Poll the watchlist right now and report what is new."""
        settings = await self.config.guild(ctx.guild).all()
        if not settings.get("watch_channel"):
            await ctx.send(
                "Set a channel first — "
                f"`{ctx.clean_prefix}bandcamp notifications channel #channel`."
            )
            return
        if not settings.get("watch"):
            await ctx.send(
                f"This server follows nothing yet — `{ctx.clean_prefix}bandcamp follow <artist>`."
            )
            return
        await self._defer(ctx)
        count = await self._poll_guild(ctx.guild, force=True)
        if count:
            await ctx.send(f"Posted **{plural(count, 'new release')}**.")
        else:
            await ctx.send("Nothing new since the last check.")

    # -- autoembed --------------------------------------------------------
    @bandcamp.group(name="autoembed", aliases=["auto"], invoke_without_command=True)
    @commands.guild_only()
    async def bandcamp_autoembed(self, ctx: commands.Context) -> None:
        """Answer pasted Bandcamp links with an embed (off by default)."""
        settings = await self.config.guild(ctx.guild).all()
        channels = settings.get("autoembed_channels") or []
        state = "on" if settings.get("autoembed") else "off"
        scope = ", ".join(f"<#{channel}>" for channel in channels) or "every channel"
        embed = discord.Embed(
            title="Bandcamp link previews",
            description=(
                f"**{state}** — scope: {scope}\n\n"
                f"`{ctx.clean_prefix}bandcamp autoembed on|off` toggles it.\n"
                f"`{ctx.clean_prefix}bandcamp autoembed channel <#channel>` limits it to "
                "one channel, `… channel remove <#channel>` drops it again."
            ),
            colour=await self._colour(ctx),
        )
        await ctx.send(embed=embed)

    @bandcamp_autoembed.command(name="on", aliases=["enable"])
    @checks.admin_or_permissions(manage_guild=True)
    async def autoembed_on(self, ctx: commands.Context) -> None:
        """Turn link previews on for this server."""
        await self.config.guild(ctx.guild).autoembed.set(True)
        await ctx.send(
            "Bandcamp link previews are **on** — links pasted in this server get an embed."
        )

    @bandcamp_autoembed.command(name="off", aliases=["disable"])
    @checks.admin_or_permissions(manage_guild=True)
    async def autoembed_off(self, ctx: commands.Context) -> None:
        """Turn link previews off for this server."""
        await self.config.guild(ctx.guild).autoembed.set(False)
        await ctx.send("Bandcamp link previews are **off**.")

    @bandcamp_autoembed.command(name="channel")
    @checks.admin_or_permissions(manage_guild=True)
    async def autoembed_channel(
        self,
        ctx: commands.Context,
        action: str,
        channel: Optional[discord.TextChannel] = None,
    ) -> None:
        """Limit previews to a channel: `channel #channel` or `channel remove #channel`."""
        removing = action.casefold() in {"remove", "del", "delete", "off"}
        if not removing and action.casefold() not in {"add", "set"}:
            # `[p]bandcamp autoembed channel #general` means "add".
            target = discord.utils.get(
                ctx.guild.text_channels, mention=action
            ) or discord.utils.get(ctx.guild.text_channels, name=action.lstrip("#"))
            channel = target or channel
            removing = False
        if channel is None:
            await ctx.send("Name a channel, like `autoembed channel #music`.")
            return
        key = str(channel.id)
        async with self.config.guild(ctx.guild).autoembed_channels() as channels:
            channels = [str(entry) for entry in channels or []]
            if removing:
                channels = [entry for entry in channels if entry != key]
            elif key not in channels:
                channels.append(key)
        if removing:
            await ctx.send(
                f"Bandcamp previews will no longer be limited to {channel.mention}."
            )
        else:
            await ctx.send(f"Bandcamp previews now only post in {channel.mention}.")

    @commands.Cog.listener()
    async def on_message_without_command(self, message: discord.Message) -> None:
        """Turn pasted Bandcamp links into an embed when the guild asked for it."""
        if message.author.bot or message.guild is None:
            return
        content = message.content or ""
        if "bandcamp.com" not in content.lower():
            return
        if await self.bot.cog_disabled_in_guild(self, message.guild):
            return
        settings = await self.config.guild(message.guild).all()
        if not settings.get("autoembed"):
            return
        channels = {str(entry) for entry in settings.get("autoembed_channels") or []}
        if channels and str(message.channel.id) not in channels:
            return
        links = collect_bandcamp_urls(content, limit=AUTOEMBED_MAX_LINKS)
        if not links:
            return
        colour = await self._colour(message.channel)
        for parsed in links:
            key = (message.channel.id, parsed.key)
            last = self._recent.get(key)
            now = time.monotonic()
            if last is not None and now - last < AUTOEMBED_WINDOW:
                continue
            self._recent[key] = now
            if len(self._recent) > 200:
                for stale in [
                    k
                    for k, stamp in self._recent.items()
                    if now - stamp > AUTOEMBED_WINDOW
                ]:
                    self._recent.pop(stale, None)
            try:
                resolved = await self.api.resolve(parsed.url)
                detail = await self._embeds_for_resolved(resolved, colour=colour)
                pages = detail.pages
            except BandcampError as error:
                log.debug("Autoembed skipped %s: %s", parsed.url, error)
                continue
            if not pages:
                continue
            view = self._view_for(detail)
            try:
                posted = await message.reply(
                    embed=pages[0],
                    view=view,
                    mention_author=False,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
            except discord.HTTPException:
                log.debug("Could not post the Bandcamp preview for %s", parsed.url)
                continue
            view.message = posted

    # -- settings ---------------------------------------------------------
    @bandcamp.group(name="set", aliases=["settings"], invoke_without_command=True)
    @commands.guild_only()
    @checks.admin_or_permissions(manage_guild=True)
    async def bandcamp_set(self, ctx: commands.Context) -> None:
        """Show this server's Bandcamp settings."""
        settings = await self.config.guild(ctx.guild).all()
        embed = discord.Embed(
            title="Bandcamp settings",
            colour=await self._colour(ctx),
        )
        embed.add_field(
            name="Results per page",
            value=str(settings.get("per_page", DEFAULT_PER_PAGE)),
        )
        embed.add_field(
            name="Auto previews", value="on" if settings.get("autoembed") else "off"
        )
        channels = settings.get("autoembed_channels") or []
        embed.add_field(
            name="Preview channels",
            value=", ".join(f"<#{channel}>" for channel in channels) or "every channel",
        )
        embed.add_field(
            name="Playback",
            value=(
                ("on" if settings.get("play", True) else "off")
                + f" · up to {settings.get('play_mib', DEFAULT_PLAY_MIB)} MiB per track"
            ),
        )
        following = len(settings.get("watch") or {})
        channel_id = settings.get("watch_channel")
        embed.add_field(
            name="Watchlist",
            value=(
                f"{plural(following, 'band')} · "
                + (f"<#{channel_id}>" if channel_id else "no channel")
                + f" · every {_clamp_interval(settings.get('watch_interval'))} min"
            ),
        )
        embed.set_footer(
            text=(
                f"{ctx.clean_prefix}bandcamp set perpage <1-{MAX_PER_PAGE}> · "
                f"{ctx.clean_prefix}bandcamp set play on|off · "
                f"{ctx.clean_prefix}bandcamp set playlimit <1-{MAX_AUDIO_MIB}>"
            )
        )
        await ctx.send(embed=embed)

    @bandcamp_set.command(name="perpage", aliases=["limit"])
    @checks.admin_or_permissions(manage_guild=True)
    async def bandcamp_set_perpage(self, ctx: commands.Context, amount: int) -> None:
        """How many results one page of a list shows (1-25)."""
        if not 1 <= amount <= MAX_PER_PAGE:
            await ctx.send(f"Give me a number between 1 and {MAX_PER_PAGE}.")
            return
        await self.config.guild(ctx.guild).per_page.set(amount)
        await ctx.send(f"Bandcamp lists now show **{amount}** entries per page.")

    @bandcamp_set.command(name="play")
    @checks.admin_or_permissions(manage_guild=True)
    async def bandcamp_set_play(self, ctx: commands.Context, state: str = "") -> None:
        """Allow or refuse posting a track as a playable file."""
        wanted = (state or "").strip().casefold()
        if wanted not in {"on", "off", "true", "false", "yes", "no"}:
            current = await self.config.guild(ctx.guild).play()
            await ctx.send(
                f"Playback is **{'on' if current else 'off'}**. "
                f"Use `{ctx.clean_prefix}bandcamp set play on` or `… off`."
            )
            return
        enabled = wanted in {"on", "true", "yes"}
        await self.config.guild(ctx.guild).play.set(enabled)
        await ctx.send(f"Track playback is **{'on' if enabled else 'off'}**.")

    @bandcamp_set.command(name="playlimit", aliases=["playmib"])
    @checks.admin_or_permissions(manage_guild=True)
    async def bandcamp_set_playlimit(self, ctx: commands.Context, mib: int) -> None:
        """Largest size a posted track may have, in MiB (default 8, max 20)."""
        if not 1 <= mib <= MAX_AUDIO_MIB:
            await ctx.send(f"Give me a number between 1 and {MAX_AUDIO_MIB}.")
            return
        await self.config.guild(ctx.guild).play_mib.set(mib)
        # 128 kbps is ~17 KB/s, so one MiB is roughly a minute of audio.
        await ctx.send(
            f"Posted tracks may now be up to **{mib} MiB** — about {mib} minutes "
            "of Bandcamp's 128 kbps preview."
        )

    @bandcamp.command(name="status")
    @commands.bot_has_permissions(embed_links=True)
    async def bandcamp_status(self, ctx: commands.Context) -> None:
        """Show cache size and what the client is set to."""
        cached = len(self.api.cache)
        embed = discord.Embed(
            title="Bandcamp client",
            description=(
                f"Endpoint: `bandcamp.com/api` (mobile v24, no token needed)\n"
                f"Cached answers: **{cached}**\n"
                f"Library: `bandcamp_async_api`"
            ),
            colour=await self._colour(ctx),
        )
        embed.set_footer(text="Cache entries expire after 10 to 30 minutes.")
        await ctx.send(embed=embed)

    @bandcamp.command(name="clearcache")
    @checks.is_owner()
    async def bandcamp_clearcache(self, ctx: commands.Context) -> None:
        """Drop every cached Bandcamp answer (bot owner)."""
        self.api.cache.clear()
        await ctx.send("Bandcamp cache cleared.")

    # -- errors -----------------------------------------------------------
    async def cog_app_command_error(
        self,
        interaction: discord.Interaction,
        error: discord.app_commands.AppCommandError,
    ) -> None:
        """Slash-command failures get a sentence instead of a silent failure."""
        original = getattr(error, "original", error)
        if isinstance(original, commands.CommandOnCooldown):
            message = f"Slow down — try again in {original.retry_after:.0f}s."
        elif isinstance(original, commands.CheckFailure):
            message = "You are not allowed to do that here."
        elif isinstance(original, BandcampError):
            message = str(original)
        else:
            log.exception("Bandcamp slash command failed", exc_info=original)
            message = "That went wrong — check the bot log."
        with contextlib.suppress(discord.HTTPException):
            if interaction.response.is_done():
                await interaction.followup.send(message, ephemeral=True)
            else:
                await interaction.response.send_message(message, ephemeral=True)


def discography_rows(releases: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """The discography endpoint's rows as pickable list rows."""
    rows: List[Dict[str, Any]] = []
    for row in releases or []:
        item_id = row.get("item_id")
        band_id = row.get("band_id")
        if not item_id or not band_id:
            continue
        item_type = row.get("item_type") or KIND_ALBUM
        kind = KIND_TRACK if item_type == "track" else KIND_ALBUM
        bits = [format_discography_date(row.get("release_date"))]
        if kind == KIND_TRACK:
            bits.append("track")
        if row.get("is_purchasable"):
            bits.append("for sale")
        rows.append(
            {
                "kind": kind,
                "id": int(item_id),
                "artist_id": int(band_id),
                "title": row.get("title") or "Untitled",
                "subtitle": " · ".join(bit for bit in bits if bit),
                "image": track_art_url(row.get("art_id")),
            }
        )
    return rows
