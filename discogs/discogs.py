"""Discogs — browse the Discogs database from Discord.

Search releases, masters, artists and labels, open a record's tracklist,
credits, ratings and marketplace prices, walk an artist's or label's catalogue,
and pull a random record when nobody can decide what to play.

Design notes:

* Everything goes through :mod:`api` next to this file, which has no framework
  imports and can be tested against the live API on its own.
* The API answers without a token, so the cog works out of the box.  A User
  token (``[p]discogs token``) only raises the rate limit from 25 to 60
  requests per minute; the client reads the real limit from the response
  headers and waits instead of collecting ``429``s.
* Lists and detail pages are ``discord.ui.View``s with fixed ``custom_id``s,
  and a data-less placeholder view is registered in ``cog_load`` so a click on
  a button whose message is long gone still gets an answer instead of a
  silent interaction timeout.
* A missing record, an empty search or a flaky API is an embed, never a raised
  exception — Red's error handler would print a traceback summary that reads
  like a broken bot.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import random
from typing import Any, Awaitable, Callable, Dict, List, Optional, Sequence, Tuple

import aiohttp
import discord
from redbot.core import Config, checks, commands
from redbot.core.bot import Red
from redbot.core.utils.views import SetApiView

from .api import (
    API_ROOT,
    CURRENCIES,
    SEARCH_TYPES,
    SORT_KEYS,
    DiscogsAuthError,
    DiscogsClient,
    DiscogsError,
    DiscogsNotFound,
    DiscogsRateLimited,
    describe_entity,
    describe_search_filters,
    format_count,
    format_formats,
    format_price,
    format_tracklist,
    join_artists,
    parse_decade,
    parse_flag_arguments,
    parse_year_filter,
    row_from_release,
    row_from_search,
    row_from_version,
    site_url,
    split_genre_filter,
    strip_markup,
    truncate,
)

log = logging.getLogger("red.freak_cogs.discogs")

SHARED_API_SERVICE = "discogs"
DEFAULT_PER_PAGE = 8
MAX_PER_PAGE = 20
VIEW_TIMEOUT = 600
#: Embed budget for one page, leaving room for the footer inside Discord's 6000.
PAGE_BUDGET = 4800

CID_LIST_PREV = "discogs:list_prev"
CID_LIST_NEXT = "discogs:list_next"
CID_LIST_PICK = "discogs:list_pick"
CID_PAGE_PREV = "discogs:page_prev"
CID_PAGE_NEXT = "discogs:page_next"
CID_ARTIST_RELEASES = "discogs:artist_releases"
CID_LABEL_RELEASES = "discogs:label_releases"
CID_MASTER_VERSIONS = "discogs:master_versions"

#: What a user may type after `--flag` on the search command.
SEARCH_FLAGS = {
    "type": "kind",
    "genre": "genre",
    "style": "style",
    "year": "year",
    "country": "country",
    "format": "fmt",
    "label": "label",
    "artist": "artist",
    "catno": "catno",
    "sort": "sort",
    "limit": "limit",
}
TOP_FLAGS = {"genre": "genre", "style": "style", "year": "year", "country": "country"}
RANDOM_FLAGS = {"genre": "genre", "style": "style", "decade": "decade", "year": "year"}

__red_end_user_data_statement__ = (
    "This cog stores guild settings only (results per page, marketplace currency and "
    "the thumbnail preference) in Red's Config. Search queries and Discogs responses "
    "are cached in memory for up to 30 minutes and dropped when the bot restarts. "
    "Queries are sent to api.discogs.com; no Discord user data leaves the bot. An "
    "optional Discogs API token is kept in Red's shared API token store."
)


# --------------------------------------------------------------------------
# embed helpers
# --------------------------------------------------------------------------


def _primary_image(payload: Dict[str, Any]) -> str:
    for image in payload.get("images") or []:
        if (image.get("type") or "").lower() == "primary" and image.get("uri"):
            return str(image["uri"])
    for image in payload.get("images") or []:
        if image.get("uri"):
            return str(image["uri"])
    return str(payload.get("thumb") or "")


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
            pages.append(current)
            used = _embed_size(current)
            fields = 0
        current.add_field(name=truncate(name, 250), value=value, inline=False)
        used += cost
        fields += 1
    return pages


def _label_field(labels: Sequence[Dict[str, Any]]) -> str:
    lines: List[str] = []
    for label in list(labels)[:3]:
        name = (label.get("name") or "—").strip()
        catno = (label.get("catno") or "").strip()
        lines.append(f"{name} · `{catno}`" if catno else name)
    return truncate("\n".join(lines), 240) or "—"


def _community_field(community: Dict[str, Any]) -> str:
    rating = community.get("rating") or {}
    average = rating.get("average")
    count = rating.get("count") or 0
    if average:
        stars = f"★ {average:.2f}/5 ({format_count(count)} ratings)"
    elif count:
        stars = f"{format_count(count)} ratings"
    else:
        stars = "No ratings yet"
    have = community.get("have") or 0
    want = community.get("want") or 0
    return f"{stars}\n{format_count(have)} have · {format_count(want)} want"


def _market_field(payload: Dict[str, Any], stats: Optional[Dict[str, Any]]) -> str:
    num = None
    price = ""
    if stats:
        num = stats.get("num_for_sale")
        lowest = stats.get("lowest_price") or {}
        if isinstance(lowest, dict):
            price = format_price(lowest.get("value"), str(lowest.get("currency") or ""))
    if num is None:
        num = payload.get("num_for_sale")
    if num is None:
        return ""
    lines = [f"{format_count(num)} for sale"]
    if price and price != "—":
        lines.append(f"from {price}")
    return "\n".join(lines)


def _track_blocks(tracks: Sequence[Dict[str, Any]]) -> List[Tuple[str, str]]:
    chunks = format_tracklist(tracks)
    blocks: List[Tuple[str, str]] = []
    for index, chunk in enumerate(chunks):
        name = "Tracklist" if index == 0 else f"Tracklist · {index + 1}"
        blocks.append((name, chunk))
    return blocks


def build_release_embeds(
    data: Dict[str, Any],
    *,
    colour: discord.Colour,
    stats: Optional[Dict[str, Any]] = None,
    thumbnails: bool = True,
) -> List[discord.Embed]:
    """The release view: facts, community numbers, price, tracklist, credits."""
    heading = (
        f"{join_artists(data.get('artists') or [])} – {data.get('title') or 'Untitled'}"
    )
    embed = discord.Embed(
        title=truncate(heading, 240), url=site_url(data), colour=colour
    )
    image = _primary_image(data) if thumbnails else ""
    if image:
        embed.set_thumbnail(url=image)
    notes = strip_markup(data.get("notes") or "")
    if notes:
        embed.description = truncate(notes, 420)

    embed.add_field(
        name="Label", value=_label_field(data.get("labels") or []), inline=True
    )
    released = (
        data.get("released_formatted")
        or data.get("released")
        or data.get("year")
        or "—"
    )
    country = (data.get("country") or "").strip()
    embed.add_field(
        name="Released",
        value=truncate(f"{released}\n{country}".strip(), 240),
        inline=True,
    )
    embed.add_field(
        name="Format",
        value=truncate(
            format_formats(
                data.get("formats") or [], quantity=data.get("format_quantity")
            ),
            300,
        ),
        inline=True,
    )
    genres = list(data.get("genres") or [])
    styles = list(data.get("styles") or [])
    genre_text = ", ".join([*genres, *styles]) or "—"
    embed.add_field(name="Genre / Style", value=truncate(genre_text, 240), inline=True)
    embed.add_field(
        name="Community",
        value=_community_field(data.get("community") or {}),
        inline=True,
    )
    market = _market_field(data, stats)
    if market:
        embed.add_field(name="Marketplace", value=market, inline=True)

    blocks = _track_blocks(data.get("tracklist") or [])
    credits = data.get("extraartists") or []
    if credits:
        lines = []
        for entry in credits[:20]:
            name = (entry.get("name") or "").strip()
            role = (entry.get("role") or "").strip()
            if name:
                lines.append(f"{name} — {role}" if role else name)
        if lines:
            blocks.append(("Credits", truncate("\n".join(lines), 900)))
    identifiers = data.get("identifiers") or []
    if identifiers:
        lines = [
            f"{entry.get('type') or 'ID'}: {entry.get('value') or ''}".strip()
            for entry in identifiers[:10]
        ]
        blocks.append(("Identifiers", truncate("\n".join(lines), 700)))
    videos = [video for video in (data.get("videos") or []) if video.get("uri")]
    if videos:
        lines = [
            f"[{truncate(video.get('title') or video['uri'], 80)}]({video['uri']})"
            for video in videos[:3]
        ]
        blocks.append(("Videos", "\n".join(lines)))
    if data.get("master_id"):
        master_url = site_url({"uri": f"/master/{data['master_id']}"})
        blocks.append(
            (
                "Master release",
                f"[#{data['master_id']}]({master_url}) — every pressing and version",
            )
        )

    pages = _split_field_pages(embed, blocks)
    footer = (
        f"Discogs · release #{data.get('id')} · data quality: "
        f"{data.get('data_quality') or 'unknown'}"
    )
    for page in pages:
        page.set_footer(text=truncate(footer, 2000))
    return pages


def build_master_embeds(
    data: Dict[str, Any],
    *,
    colour: discord.Colour,
    versions_total: Optional[int] = None,
    main_release: Optional[Dict[str, Any]] = None,
    thumbnails: bool = True,
) -> List[discord.Embed]:
    """The master release view: the canonical version of a record."""
    heading = (
        f"{join_artists(data.get('artists') or [])} – {data.get('title') or 'Untitled'}"
    )
    embed = discord.Embed(
        title=truncate(heading, 240), url=site_url(data), colour=colour
    )
    image = _primary_image(data) if thumbnails else ""
    if image:
        embed.set_thumbnail(url=image)
    year = data.get("year") or "—"
    embed.add_field(name="First released", value=str(year), inline=True)
    genre_text = (
        ", ".join([*(data.get("genres") or []), *(data.get("styles") or [])]) or "—"
    )
    embed.add_field(name="Genre / Style", value=truncate(genre_text, 240), inline=True)
    if versions_total:
        embed.add_field(
            name="Versions", value=format_count(versions_total), inline=True
        )
    market = _market_field(data, None)
    if market:
        embed.add_field(name="Marketplace", value=market, inline=True)
    if data.get("main_release"):
        link = site_url({"uri": f"/release/{data['main_release']}"})
        text = f"[Release #{data['main_release']}]({link})"
        if main_release and main_release.get("title"):
            text += f"\n{join_artists(main_release.get('artists') or [])}"
        embed.add_field(name="Main release", value=text, inline=True)

    blocks = _track_blocks(data.get("tracklist") or [])
    videos = [video for video in (data.get("videos") or []) if video.get("uri")]
    if videos:
        blocks.append(
            (
                "Videos",
                "\n".join(
                    f"[{truncate(video.get('title') or video['uri'], 80)}]({video['uri']})"
                    for video in videos[:3]
                ),
            )
        )
    pages = _split_field_pages(embed, blocks)
    footer = f"Discogs · master #{data.get('id')}"
    for page in pages:
        page.set_footer(text=truncate(footer, 2000))
    return pages


def build_artist_embeds(
    data: Dict[str, Any], *, colour: discord.Colour, thumbnails: bool = True
) -> List[discord.Embed]:
    """The artist view: profile, aliases, groups and links."""
    embed = discord.Embed(
        title=truncate(data.get("name") or "Unknown artist", 240),
        url=site_url(data),
        colour=colour,
    )
    image = _primary_image(data) if thumbnails else ""
    if image:
        embed.set_thumbnail(url=image)
    if data.get("realname"):
        embed.add_field(
            name="Real name", value=truncate(strip_markup(data["realname"]), 240)
        )
    blocks: List[Tuple[str, str]] = []
    profile = strip_markup(data.get("profile") or "")
    if profile:
        chunks = [
            truncate(profile[i : i + 900], 900) for i in range(0, len(profile), 900)
        ]
        for index, chunk in enumerate(chunks[:6]):
            blocks.append(
                ("Profile" if index == 0 else f"Profile · {index + 1}", chunk)
            )
    aliases = [
        alias.get("name") for alias in (data.get("aliases") or []) if alias.get("name")
    ]
    if aliases:
        blocks.append(("Also known as", truncate(", ".join(aliases), 700)))
    groups = [
        group.get("name") for group in (data.get("groups") or []) if group.get("name")
    ]
    if groups:
        blocks.append(("Member of", truncate(", ".join(groups), 700)))
    variations = [name for name in (data.get("namevariations") or []) if name]
    if variations:
        blocks.append(("Name variations", truncate(", ".join(variations), 700)))
    urls = [url for url in (data.get("urls") or []) if url]
    if urls:
        blocks.append(("Links", "\n".join(urls[:8])))
    pages = _split_field_pages(embed, blocks)
    for page in pages:
        page.set_footer(text=f"Discogs · artist #{data.get('id')}")
    return pages


def build_label_embeds(
    data: Dict[str, Any], *, colour: discord.Colour, thumbnails: bool = True
) -> List[discord.Embed]:
    """The label view: profile, parent, sublabels and links."""
    embed = discord.Embed(
        title=truncate(data.get("name") or "Unknown label", 240),
        url=site_url(data),
        colour=colour,
    )
    image = _primary_image(data) if thumbnails else ""
    if image:
        embed.set_thumbnail(url=image)
    parent = data.get("parent_label") or {}
    if parent.get("name"):
        embed.add_field(
            name="Parent label", value=truncate(parent["name"], 240), inline=True
        )
    sublabels = [
        sub.get("name") for sub in (data.get("sublabels") or []) if sub.get("name")
    ]
    if sublabels:
        embed.add_field(
            name=f"Sublabels ({len(sublabels)})",
            value=truncate(", ".join(sublabels), 400),
            inline=True,
        )
    blocks: List[Tuple[str, str]] = []
    profile = strip_markup(data.get("profile") or "")
    if profile:
        chunks = [
            truncate(profile[i : i + 900], 900) for i in range(0, len(profile), 900)
        ]
        for index, chunk in enumerate(chunks[:5]):
            blocks.append(
                ("Profile" if index == 0 else f"Profile · {index + 1}", chunk)
            )
    urls = [url for url in (data.get("urls") or []) if url]
    if urls:
        blocks.append(("Links", "\n".join(urls[:8])))
    contact = strip_markup(data.get("contact_info") or "")
    if contact:
        blocks.append(("Contact", truncate(contact, 700)))
    pages = _split_field_pages(embed, blocks)
    for page in pages:
        page.set_footer(text=f"Discogs · label #{data.get('id')}")
    return pages


def build_list_embed(
    rows: Sequence[Dict[str, Any]],
    *,
    title: str,
    page: int,
    pages: int,
    total: int,
    colour: discord.Colour,
    footer_extra: str = "",
) -> discord.Embed:
    """A page of search hits: one link plus its metadata line per row."""
    embed = discord.Embed(title=truncate(title, 240), colour=colour)
    if not rows:
        embed.description = "Nothing found for that."
    else:
        lines: List[str] = []
        for index, row in enumerate(rows, start=1):
            label = truncate(row.get("title") or "Untitled", 140)
            if row.get("url"):
                lines.append(f"**{index}.** [{label}]({row['url']})")
            else:
                lines.append(f"**{index}.** {label}")
            subtitle = truncate(row.get("subtitle") or "", 200)
            if subtitle:
                lines.append(f"`{subtitle}`")
        embed.description = "\n".join(lines)[:4096]
    footer = f"Page {page}/{max(1, pages)} · {format_count(total)} entries"
    if footer_extra:
        footer += f" · {footer_extra}"
    embed.set_footer(text=truncate(footer, 2000))
    return embed


def error_embed(
    title: str, detail: str, *, colour: Optional[discord.Colour] = None
) -> discord.Embed:
    """An expected failure (empty result, 404, rate limit) — never a raised error."""
    embed = discord.Embed(
        title=truncate(title, 240), description=truncate(detail, 2000), colour=colour
    )
    return embed


# --------------------------------------------------------------------------
# views
# --------------------------------------------------------------------------


class PlaceholderView(discord.ui.View):
    """Answers clicks on buttons whose message is gone (restart, timeout, reload)."""

    MESSAGE = "That list has expired — run the command again."

    def __init__(self, cog: "Discogs") -> None:
        super().__init__(timeout=None)
        self.cog = cog
        buttons = (
            ("Previous", discord.ButtonStyle.secondary, CID_LIST_PREV),
            ("Next", discord.ButtonStyle.secondary, CID_LIST_NEXT),
            ("Releases", discord.ButtonStyle.primary, CID_ARTIST_RELEASES),
            ("Versions", discord.ButtonStyle.primary, CID_MASTER_VERSIONS),
            ("‹ Back", discord.ButtonStyle.secondary, CID_PAGE_PREV),
            ("Next ›", discord.ButtonStyle.secondary, CID_PAGE_NEXT),
            ("Releases", discord.ButtonStyle.primary, CID_LABEL_RELEASES),
        )
        for label, style, custom_id in buttons:
            button = discord.ui.Button(label=label, style=style, custom_id=custom_id)
            button.callback = self._expired
            self.add_item(button)
        select = discord.ui.Select(
            custom_id=CID_LIST_PICK,
            placeholder="Pick a result",
            options=[discord.SelectOption(label="—", value="none")],
            disabled=True,
        )
        select.callback = self._expired
        self.add_item(select)

    async def _expired(self, interaction: discord.Interaction) -> None:
        with contextlib.suppress(discord.HTTPException):
            await interaction.response.send_message(self.MESSAGE, ephemeral=True)


class ListView(discord.ui.View):
    """A paginated list of Discogs entries with a picker that opens details."""

    def __init__(
        self,
        cog: "Discogs",
        *,
        title: str,
        source: Callable[[int], Awaitable[Tuple[List[Dict[str, Any]], Dict[str, Any]]]],
        per_page: int,
        colour: discord.Colour,
        footer_extra: str = "",
        link: Optional[str] = None,
        timeout: int = VIEW_TIMEOUT,
    ) -> None:
        super().__init__(timeout=timeout)
        self.cog = cog
        self.title = title
        self.source = source
        self.per_page = per_page
        self.colour = colour
        self.footer_extra = footer_extra
        self.page = 1
        self.rows: List[Dict[str, Any]] = []
        self.pagination: Dict[str, Any] = {}
        self.message: Optional[discord.Message] = None
        self._loaded: Dict[int, Tuple[List[Dict[str, Any]], Dict[str, Any]]] = {}

        self._prev = discord.ui.Button(
            label="Previous",
            style=discord.ButtonStyle.secondary,
            custom_id=CID_LIST_PREV,
        )
        self._prev.callback = self._on_prev
        self._next = discord.ui.Button(
            label="Next", style=discord.ButtonStyle.secondary, custom_id=CID_LIST_NEXT
        )
        self._next.callback = self._on_next
        self._pick = discord.ui.Select(
            custom_id=CID_LIST_PICK,
            placeholder="Open a result",
            min_values=1,
            max_values=1,
            options=[discord.SelectOption(label="—", value="none")],
        )
        self._pick.callback = self._on_pick
        for item in (self._prev, self._next):
            self.add_item(item)
        if link:
            self.add_item(
                discord.ui.Button(
                    label="Open on Discogs", style=discord.ButtonStyle.link, url=link
                )
            )
        self.add_item(self._pick)

    # -- data -------------------------------------------------------------
    async def load(self, page: int) -> None:
        page = max(1, page)
        cached = self._loaded.get(page)
        if cached is None:
            cached = await self.source(page)
            if len(self._loaded) > 24:
                self._loaded.clear()
            self._loaded[page] = cached
        rows, pagination = cached
        self.rows = list(rows or [])
        self.pagination = dict(pagination or {})
        self.page = int(self.pagination.get("page") or page)

    @property
    def pages(self) -> int:
        return max(1, int(self.pagination.get("pages") or 1))

    @property
    def total(self) -> int:
        return int(self.pagination.get("items") or len(self.rows))

    # -- rendering --------------------------------------------------------
    def embed(self) -> discord.Embed:
        return build_list_embed(
            self.rows,
            title=self.title,
            page=self.page,
            pages=self.pages,
            total=self.total,
            colour=self.colour,
            footer_extra=self.footer_extra,
        )

    def _sync(self) -> None:
        self._prev.disabled = self.page <= 1
        self._next.disabled = self.page >= self.pages
        options = [
            discord.SelectOption(
                label=truncate(row.get("title") or "Untitled", 100),
                description=truncate(
                    f"{describe_entity(row.get('kind') or 'release', {})} · "
                    f"{row.get('subtitle') or ''}",
                    100,
                ),
                value=f"{row.get('kind') or 'release'}:{row.get('id')}",
            )
            for row in self.rows[:25]
            if row.get("id")
        ]
        self._pick.options = options or [discord.SelectOption(label="—", value="none")]
        self._pick.disabled = not options
        if not options:
            self._pick.placeholder = "Nothing to open"

    async def start(self, ctx: commands.Context) -> None:
        self._sync()
        self.message = await ctx.send(embed=self.embed(), view=self)

    # -- callbacks --------------------------------------------------------
    async def _on_prev(self, interaction: discord.Interaction) -> None:
        if self.page <= 1:
            await interaction.response.send_message(
                "Already on the first page.", ephemeral=True
            )
            return
        await interaction.response.defer()
        try:
            await self.load(self.page - 1)
        except DiscogsError as exc:
            await interaction.followup.send(
                embed=error_embed("Discogs did not answer", str(exc)), ephemeral=True
            )
            return
        self._sync()
        await interaction.edit_original_response(embed=self.embed(), view=self)

    async def _on_next(self, interaction: discord.Interaction) -> None:
        if self.page >= self.pages:
            await interaction.response.send_message(
                "That was the last page.", ephemeral=True
            )
            return
        await interaction.response.defer()
        try:
            await self.load(self.page + 1)
        except DiscogsError as exc:
            await interaction.followup.send(
                embed=error_embed("Discogs did not answer", str(exc)), ephemeral=True
            )
            return
        self._sync()
        await interaction.edit_original_response(embed=self.embed(), view=self)

    async def _on_pick(self, interaction: discord.Interaction) -> None:
        value = (self._pick.values or [""])[0]
        await self.cog.open_row(interaction, value)

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
        log.exception("Discogs list view failed", exc_info=error)
        message = "That went wrong — check the bot log."
        with contextlib.suppress(discord.HTTPException):
            if interaction.response.is_done():
                await interaction.followup.send(message, ephemeral=True)
            else:
                await interaction.response.send_message(message, ephemeral=True)


class PageView(discord.ui.View):
    """A paginated detail view (long tracklists, profiles) with action buttons."""

    def __init__(
        self,
        cog: "Discogs",
        pages: Sequence[discord.Embed],
        *,
        extras: Sequence[discord.ui.Item] = (),
        link: Optional[str] = None,
        timeout: int = VIEW_TIMEOUT,
    ) -> None:
        super().__init__(timeout=timeout)
        self.cog = cog
        self.pages = list(pages) or [discord.Embed(title="Nothing to show")]
        self.index = 0
        self.message: Optional[discord.Message] = None
        for item in extras:
            self.add_item(item)
        if len(self.pages) > 1:
            self._prev = discord.ui.Button(
                label="‹ Back",
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
        else:
            self._prev = self._next = None  # type: ignore[assignment]
        if link:
            self.add_item(
                discord.ui.Button(
                    label="Open on Discogs", style=discord.ButtonStyle.link, url=link
                )
            )
        self._sync()

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
        log.exception("Discogs detail view failed", exc_info=error)
        with contextlib.suppress(discord.HTTPException):
            if interaction.response.is_done():
                await interaction.followup.send(
                    "That went wrong — see the bot log.", ephemeral=True
                )
            else:
                await interaction.response.send_message(
                    "That went wrong — see the bot log.", ephemeral=True
                )


class RelatedListButton(discord.ui.Button):
    """Opens the releases of an artist/label, or the versions of a master."""

    def __init__(
        self, cog: "Discogs", *, kind: str, entity_id: int, label: str, custom_id: str
    ):
        super().__init__(
            label=label, style=discord.ButtonStyle.primary, custom_id=custom_id
        )
        self.cog = cog
        self.kind = kind
        self.entity_id = entity_id

    async def callback(self, interaction: discord.Interaction) -> None:
        await self.cog.open_related_list(interaction, self.kind, self.entity_id)


# --------------------------------------------------------------------------
# cog
# --------------------------------------------------------------------------


class Discogs(commands.Cog):
    """Search Discogs, browse artists and labels, and dig through tracklists."""

    def __init__(self, bot: Red) -> None:
        self.bot = bot
        self.config = Config.get_conf(
            self, identifier=987654321, force_registration=True
        )
        self.config.register_guild(
            per_page=DEFAULT_PER_PAGE, currency="EUR", thumbnails=True
        )
        self.session: Optional[aiohttp.ClientSession] = None
        self.client: Optional[DiscogsClient] = None

    # -- lifecycle --------------------------------------------------------
    async def cog_load(self) -> None:
        self.session = aiohttp.ClientSession()
        self.client = DiscogsClient(self.session)
        tokens = await self.bot.get_shared_api_tokens(SHARED_API_SERVICE)
        self.client.set_token((tokens or {}).get("token"))
        with contextlib.suppress(Exception):
            self.bot.add_view(PlaceholderView(self))

    def cog_unload(self) -> None:
        session = self.session
        if session is not None and not session.closed:
            try:
                asyncio.get_running_loop().create_task(session.close())
            except RuntimeError:
                log.debug("No running loop on unload; letting the session be collected")

    @commands.Cog.listener()
    async def on_red_api_tokens_update(
        self, service_name: str, api_tokens: Dict[str, str]
    ) -> None:
        """Pick up `[p]set api discogs token,…` without a reload."""
        if service_name != SHARED_API_SERVICE or self.client is None:
            return
        self.client.set_token((api_tokens or {}).get("token"))

    # -- helpers ----------------------------------------------------------
    @property
    def api(self) -> DiscogsClient:
        if self.client is None:  # pragma: no cover - only before cog_load
            raise RuntimeError("Discogs client is not ready yet.")
        return self.client

    async def _settings(
        self,
        source: Optional[discord.abc.Messageable] = None,
        guild: Optional[discord.Guild] = None,
    ) -> Dict[str, Any]:
        if guild is None:
            guild = getattr(source, "guild", None)
        defaults = {"per_page": DEFAULT_PER_PAGE, "currency": "EUR", "thumbnails": True}
        if guild is None:
            return defaults
        try:
            stored = await self.config.guild(guild).all()
        except Exception:  # pragma: no cover - config trouble must not kill a lookup
            log.debug("Could not read guild settings", exc_info=True)
            return defaults
        return {**defaults, **(stored or {})}

    async def _per_page(self, guild: Optional[discord.Guild]) -> int:
        settings = await self._settings(guild=guild)
        try:
            value = int(settings.get("per_page") or DEFAULT_PER_PAGE)
        except (TypeError, ValueError):
            value = DEFAULT_PER_PAGE
        return max(1, min(MAX_PER_PAGE, value))

    async def _currency(self, guild: Optional[discord.Guild]) -> str:
        settings = await self._settings(guild=guild)
        currency = str(settings.get("currency") or "EUR").upper()
        return currency if currency in CURRENCIES else "EUR"

    async def _entity_view(
        self, kind: str, entity_id: int, *, guild: Optional[discord.Guild]
    ) -> Tuple[List[discord.Embed], List[discord.ui.Item], str]:
        """Fetch one entity and build its pages plus its action buttons."""
        colour = await self._colour(guild)
        settings = await self._settings(guild=guild)
        thumbnails = bool(settings.get("thumbnails", True))
        kind = (kind or "release").lower()
        if kind == "release":
            data = await self.api.release(entity_id)
            stats = None
            with contextlib.suppress(DiscogsError):
                stats = await self.api.marketplace_stats(
                    entity_id, await self._currency(guild)
                )
            pages = build_release_embeds(
                data, colour=colour, stats=stats, thumbnails=thumbnails
            )
            extras: List[discord.ui.Item] = []
            if data.get("master_id"):
                extras.append(
                    RelatedListButton(
                        self,
                        kind="versions",
                        entity_id=int(data["master_id"]),
                        label="Versions",
                        custom_id=CID_MASTER_VERSIONS,
                    )
                )
            return pages, extras, site_url(data)
        if kind == "master":
            data = await self.api.master(entity_id)
            versions_total = None
            main_release = None
            with contextlib.suppress(DiscogsError):
                versions = await self.api.master_versions(entity_id, page=1, per_page=1)
                versions_total = int(
                    (versions.get("pagination") or {}).get("items") or 0
                )
            with contextlib.suppress(DiscogsError, TypeError, ValueError):
                main_release = await self.api.release(int(data["main_release"]))
            pages = build_master_embeds(
                data,
                colour=colour,
                versions_total=versions_total,
                main_release=main_release,
                thumbnails=thumbnails,
            )
            extras = [
                RelatedListButton(
                    self,
                    kind="versions",
                    entity_id=entity_id,
                    label="Versions",
                    custom_id=CID_MASTER_VERSIONS,
                )
            ]
            return pages, extras, site_url(data)
        if kind == "artist":
            data = await self.api.artist(entity_id)
            pages = build_artist_embeds(data, colour=colour, thumbnails=thumbnails)
            extras = [
                RelatedListButton(
                    self,
                    kind="artist",
                    entity_id=entity_id,
                    label="Releases",
                    custom_id=CID_ARTIST_RELEASES,
                )
            ]
            return pages, extras, site_url(data)
        if kind == "label":
            data = await self.api.label(entity_id)
            pages = build_label_embeds(data, colour=colour, thumbnails=thumbnails)
            extras = [
                RelatedListButton(
                    self,
                    kind="label",
                    entity_id=entity_id,
                    label="Releases",
                    custom_id=CID_LABEL_RELEASES,
                )
            ]
            return pages, extras, site_url(data)
        raise DiscogsError(f"Unknown entry type `{kind}`.")

    async def _colour(self, guild: Optional[discord.Guild]) -> discord.Colour:
        try:
            return await self.bot.get_embed_color(guild)
        except Exception:  # pragma: no cover - colour is cosmetic
            return discord.Colour.blurple()

    async def _resolve_id(self, kind: str, value: str) -> int:
        """Accept a numeric ID or a name (resolved through search)."""
        text = (value or "").strip()
        if text.isdigit():
            return int(text)
        data = await self.api.search(text, kind=kind, per_page=5)
        results = data.get("results") or []
        if not results:
            raise DiscogsNotFound(f"No {kind} found for `{text}`.")
        best = results[0]
        entity_id = best.get("id") or best.get("master_id")
        if not entity_id:
            raise DiscogsNotFound(f"No {kind} found for `{text}`.")
        return int(entity_id)

    async def open_row(self, interaction: discord.Interaction, value: str) -> None:
        """Open the detail view for a picked list row."""
        kind, _, raw_id = (value or "").partition(":")
        if not raw_id.isdigit():
            await interaction.response.send_message(
                "That entry is not available anymore.", ephemeral=True
            )
            return
        await interaction.response.defer()
        try:
            pages, extras, link = await self._entity_view(
                kind, int(raw_id), guild=interaction.guild
            )
        except DiscogsNotFound as exc:
            await interaction.followup.send(
                embed=error_embed("Not on Discogs", str(exc)), ephemeral=True
            )
            return
        except DiscogsError as exc:
            await interaction.followup.send(
                embed=error_embed("Discogs did not answer", str(exc)), ephemeral=True
            )
            return
        view = PageView(self, pages, extras=extras, link=link)
        view.message = await interaction.followup.send(embeds=[pages[0]], view=view)

    async def open_related_list(
        self, interaction: discord.Interaction, kind: str, entity_id: int
    ) -> None:
        """Open the release list of an artist/label or the versions of a master."""
        await interaction.response.defer()
        guild = interaction.guild
        per_page = await self._per_page(guild)
        colour = await self._colour(guild)
        try:
            if kind == "artist":
                artist = await self.api.artist(entity_id)
                name = artist.get("name") or f"Artist #{entity_id}"

                async def source(page: int):
                    data = await self.api.artist_releases(
                        entity_id, page=page, per_page=per_page
                    )
                    return (
                        [row_from_release(item) for item in data.get("releases") or []],
                        data.get("pagination") or {},
                    )

                title = f"{name} · releases"
                link = site_url(artist)
            elif kind == "label":
                label = await self.api.label(entity_id)
                name = label.get("name") or f"Label #{entity_id}"

                async def source(page: int):
                    data = await self.api.label_releases(
                        entity_id, page=page, per_page=per_page
                    )
                    return (
                        [row_from_release(item) for item in data.get("releases") or []],
                        data.get("pagination") or {},
                    )

                title = f"{name} · releases"
                link = site_url(label)
            elif kind == "versions":
                master = await self.api.master(entity_id)
                name = f"{join_artists(master.get('artists') or [])} – {master.get('title')}"

                async def source(page: int):
                    data = await self.api.master_versions(
                        entity_id, page=page, per_page=per_page
                    )
                    return (
                        [row_from_version(item) for item in data.get("versions") or []],
                        data.get("pagination") or {},
                    )

                title = f"{name} · versions"
                link = site_url(master)
            else:
                raise DiscogsError(f"Unknown list type `{kind}`.")
            view = ListView(
                self,
                title=title,
                source=source,
                per_page=per_page,
                colour=colour,
                link=link,
            )
            await view.load(1)
            view._sync()
            view.message = await interaction.followup.send(
                embed=view.embed(), view=view
            )
        except DiscogsNotFound as exc:
            await interaction.followup.send(
                embed=error_embed("Not on Discogs", str(exc)), ephemeral=True
            )
        except DiscogsError as exc:
            await interaction.followup.send(
                embed=error_embed("Discogs did not answer", str(exc)), ephemeral=True
            )

    async def _send_list(
        self,
        ctx: commands.Context,
        *,
        title: str,
        source: Callable[[int], Awaitable[Tuple[List[Dict[str, Any]], Dict[str, Any]]]],
        footer_extra: str = "",
        link: Optional[str] = None,
    ) -> None:
        guild = getattr(ctx, "guild", None)
        per_page = await self._per_page(guild)
        colour = await ctx.embed_color()
        view = ListView(
            self,
            title=title,
            source=source,
            per_page=per_page,
            colour=colour,
            footer_extra=footer_extra,
            link=link,
        )
        try:
            await view.load(1)
        except DiscogsNotFound as exc:
            await ctx.send(embed=error_embed("Not on Discogs", str(exc), colour=colour))
            return
        except DiscogsRateLimited as exc:
            await ctx.send(
                embed=error_embed("Slow down a moment", str(exc), colour=colour)
            )
            return
        except DiscogsError as exc:
            await ctx.send(
                embed=error_embed("Discogs did not answer", str(exc), colour=colour)
            )
            return
        if not view.rows:
            await ctx.send(
                embed=error_embed(
                    "Nothing found",
                    "No entry matches those filters. Try a broader search.",
                    colour=colour,
                )
            )
            return
        view._sync()
        view.message = await ctx.send(embed=view.embed(), view=view)

    # -- commands ---------------------------------------------------------
    @commands.group(name="discogs", invoke_without_command=True)
    @commands.bot_has_permissions(embed_links=True)
    @commands.cooldown(4, 60, commands.BucketType.user)
    async def discogs(
        self, ctx: commands.Context, *, query: Optional[str] = None
    ) -> None:
        """Search Discogs (or run `[p]discogs` for the command list).

        `[p]discogs daft punk` searches releases; every subcommand has its own
        filters, run `[p]discogs search` for the full list.
        """
        if not query:
            await ctx.send_help()
            return
        await self._search_command(ctx, query=query, kind="release")

    @discogs.command(name="search", aliases=["find"])
    @commands.bot_has_permissions(embed_links=True)
    @commands.cooldown(4, 60, commands.BucketType.user)
    async def discogs_search(self, ctx: commands.Context, *, args: str) -> None:
        """Search the Discogs database.

        Flags (all optional): `--type release|master|artist|label`, `--genre`,
        `--style`, `--year`, `--country`, `--format`, `--label`, `--artist`,
        `--catno`, `--sort year|artist|title|label|catno|format|rating|added|want`,
        `--limit 1-25`.

        **Examples**
        `[p]discogs search daft punk --year 2001`
        `[p]discogs search aphex --type artist`
        `[p]discogs search --genre Techno --style Detroit --sort want`
        `[p]discogs search blue monday --format Vinyl --country UK`
        """
        values, query, unknown = parse_flag_arguments(args, SEARCH_FLAGS)
        if unknown:
            await ctx.send(
                embed=error_embed(
                    "Unknown flag",
                    f"`--{unknown[0]}` is not a search flag.\nAvailable: "
                    + ", ".join(f"`--{name}`" for name in SEARCH_FLAGS),
                    colour=await ctx.embed_color(),
                )
            )
            return
        limit = None
        if values.get("limit"):
            try:
                limit = int(values["limit"])
            except ValueError:
                await ctx.send(
                    embed=error_embed(
                        "`--limit` needs a number",
                        "For example `--limit 10` (1–25 per page).",
                        colour=await ctx.embed_color(),
                    )
                )
                return
        await self._search_command(
            ctx,
            query=query,
            kind=values.get("kind", "release"),
            limit=limit,
            genre=values.get("genre"),
            style=values.get("style"),
            country=values.get("country"),
            year=values.get("year"),
            fmt=values.get("fmt"),
            label=values.get("label"),
            artist=values.get("artist"),
            catno=values.get("catno"),
            sort=values.get("sort"),
        )

    async def _search_command(
        self,
        ctx: commands.Context,
        *,
        query: Optional[str] = None,
        kind: str = "release",
        limit: Optional[int] = None,
        **filters: Any,
    ) -> None:
        kind = (kind or "release").strip().lower()
        if kind not in SEARCH_TYPES:
            await ctx.send(
                embed=error_embed(
                    "Unknown search type",
                    f"`{kind}` is not one of: "
                    + ", ".join(f"`{name}`" for name in SEARCH_TYPES),
                    colour=await ctx.embed_color(),
                )
            )
            return
        if not (query and query.strip()) and not any(
            value for key, value in filters.items() if key != "sort"
        ):
            await ctx.send(
                embed=error_embed(
                    "Nothing to search for",
                    "Give me a query, a filter (for example `--genre Techno`), or both.",
                    colour=await ctx.embed_color(),
                )
            )
            return
        genre_field, genre_value, genre_note = split_genre_filter(filters.get("genre"))
        filters["genre"] = None
        if genre_value:
            if genre_field == "style" and filters.get("style"):
                genre_note = (
                    f"“{filters['style']}” is the style already — ignored "
                    f"the genre “{genre_value}”"
                )
            else:
                filters[genre_field] = genre_value
        sort_key = (filters.get("sort") or "").strip().lower() or None
        if sort_key and sort_key not in SORT_KEYS:
            await ctx.send(
                embed=error_embed(
                    "Unknown sort key",
                    f"Pick one of: {', '.join(f'`{key}`' for key in SORT_KEYS)}.",
                    colour=await ctx.embed_color(),
                )
            )
            return
        filters["sort"] = sort_key
        if sort_key:
            filters["sort_order"] = (
                "desc" if sort_key in ("year", "rating", "added", "want") else "asc"
            )
        params = {
            key: value for key, value in filters.items() if value not in (None, "")
        }
        per_page = await self._limit_per_page(ctx.guild, limit, kind)
        query_text = (query or "").strip()

        async def source(page: int):
            data = await self.api.search(
                query_text or None, kind=kind, page=page, per_page=per_page, **params
            )
            return (
                [row_from_search(result) for result in data.get("results") or []],
                data.get("pagination") or {},
            )

        title_bits = [f"Discogs · {query_text}" if query_text else "Discogs"]
        title_bits.append(describe_entity(kind, {}))
        extra = describe_search_filters(params)
        if genre_note:
            extra = f"{extra} · {genre_note}" if extra else genre_note
        await self._send_list(
            ctx,
            title=" · ".join(part for part in title_bits if part),
            source=source,
            footer_extra=extra,
            link=f"{API_ROOT}/database/search",
        )

    async def _limit_per_page(
        self, guild: Optional[discord.Guild], limit: Optional[int], kind: str
    ) -> int:
        per_page = await self._per_page(guild)
        if limit:
            per_page = max(1, min(25, int(limit)))
        # A picker takes at most 25 options, so cap the page accordingly.
        return min(per_page, 25)

    @discogs.command(name="release", aliases=["rel"])
    @commands.bot_has_permissions(embed_links=True)
    @commands.cooldown(6, 60, commands.BucketType.user)
    async def discogs_release(self, ctx: commands.Context, *, release: str) -> None:
        """Show one release by ID (or by title, which is searched first).

        `[p]discogs release 249504`
        `[p]discogs release never gonna give you up`
        """
        colour = await ctx.embed_color()
        async with ctx.typing():
            try:
                release_id = await self._resolve_id("release", release)
                pages, extras, link = await self._entity_view(
                    "release", release_id, guild=ctx.guild
                )
            except DiscogsNotFound as exc:
                await ctx.send(
                    embed=error_embed("Not on Discogs", str(exc), colour=colour)
                )
                return
            except DiscogsAuthError as exc:
                await ctx.send(
                    embed=error_embed("Discogs needs a token", str(exc), colour=colour)
                )
                return
            except DiscogsError as exc:
                await ctx.send(
                    embed=error_embed("Discogs did not answer", str(exc), colour=colour)
                )
                return
        view = PageView(self, pages, extras=extras, link=link)
        view.message = await ctx.send(embeds=[pages[0]], view=view)

    @discogs.command(name="master")
    @commands.bot_has_permissions(embed_links=True)
    @commands.cooldown(6, 60, commands.BucketType.user)
    async def discogs_master(self, ctx: commands.Context, *, master: str) -> None:
        """Show a master release — the record behind all its pressings.

        `[p]discogs master 96559`
        """
        colour = await ctx.embed_color()
        async with ctx.typing():
            try:
                master_id = await self._resolve_id("master", master)
                pages, extras, link = await self._entity_view(
                    "master", master_id, guild=ctx.guild
                )
            except DiscogsNotFound as exc:
                await ctx.send(
                    embed=error_embed("Not on Discogs", str(exc), colour=colour)
                )
                return
            except DiscogsError as exc:
                await ctx.send(
                    embed=error_embed("Discogs did not answer", str(exc), colour=colour)
                )
                return
        view = PageView(self, pages, extras=extras, link=link)
        view.message = await ctx.send(embeds=[pages[0]], view=view)

    @discogs.command(name="artist")
    @commands.bot_has_permissions(embed_links=True)
    @commands.cooldown(6, 60, commands.BucketType.user)
    async def discogs_artist(self, ctx: commands.Context, *, artist: str) -> None:
        """Show an artist profile, with a button for their releases.

        `[p]discogs artist aphex twin`
        """
        colour = await ctx.embed_color()
        async with ctx.typing():
            try:
                artist_id = await self._resolve_id("artist", artist)
                pages, extras, link = await self._entity_view(
                    "artist", artist_id, guild=ctx.guild
                )
            except DiscogsNotFound as exc:
                await ctx.send(
                    embed=error_embed("Not on Discogs", str(exc), colour=colour)
                )
                return
            except DiscogsError as exc:
                await ctx.send(
                    embed=error_embed("Discogs did not answer", str(exc), colour=colour)
                )
                return
        view = PageView(self, pages, extras=extras, link=link)
        view.message = await ctx.send(embeds=[pages[0]], view=view)

    @discogs.command(name="label")
    @commands.bot_has_permissions(embed_links=True)
    @commands.cooldown(6, 60, commands.BucketType.user)
    async def discogs_label(self, ctx: commands.Context, *, label: str) -> None:
        """Show a label profile, with a button for its releases.

        `[p]discogs label planet e`
        """
        colour = await ctx.embed_color()
        async with ctx.typing():
            try:
                label_id = await self._resolve_id("label", label)
                pages, extras, link = await self._entity_view(
                    "label", label_id, guild=ctx.guild
                )
            except DiscogsNotFound as exc:
                await ctx.send(
                    embed=error_embed("Not on Discogs", str(exc), colour=colour)
                )
                return
            except DiscogsError as exc:
                await ctx.send(
                    embed=error_embed("Discogs did not answer", str(exc), colour=colour)
                )
                return
        view = PageView(self, pages, extras=extras, link=link)
        view.message = await ctx.send(embeds=[pages[0]], view=view)

    @discogs.command(name="versions")
    @commands.bot_has_permissions(embed_links=True)
    @commands.cooldown(4, 60, commands.BucketType.user)
    async def discogs_versions(self, ctx: commands.Context, *, master: str) -> None:
        """List every pressing and version of a master release.

        `[p]discogs versions 96559`
        """
        colour = await ctx.embed_color()
        async with ctx.typing():
            try:
                master_id = await self._resolve_id("master", master)
                master_data = await self.api.master(master_id)
            except DiscogsNotFound as exc:
                await ctx.send(
                    embed=error_embed("Not on Discogs", str(exc), colour=colour)
                )
                return
            except DiscogsError as exc:
                await ctx.send(
                    embed=error_embed("Discogs did not answer", str(exc), colour=colour)
                )
                return
        per_page = await self._per_page(ctx.guild)

        async def source(page: int):
            data = await self.api.master_versions(
                master_id, page=page, per_page=per_page
            )
            return (
                [row_from_version(version) for version in data.get("versions") or []],
                data.get("pagination") or {},
            )

        name = f"{join_artists(master_data.get('artists') or [])} – {master_data.get('title')}"
        await self._send_list(
            ctx,
            title=f"{name} · versions",
            source=source,
            link=site_url(master_data),
        )

    @discogs.command(name="top", aliases=["popular"])
    @commands.bot_has_permissions(embed_links=True)
    @commands.cooldown(4, 60, commands.BucketType.user)
    async def discogs_top(self, ctx: commands.Context, *, args: str = "") -> None:
        """The most wanted records, optionally narrowed by genre/style/year.

        `[p]discogs top`
        `[p]discogs top --genre Techno --year 1995`
        `[p]discogs top --style "Detroit Techno"`

        Flags: `--genre`, `--style`, `--year`, `--country`. Quote values that
        contain a space.
        """
        values, _, unknown = parse_flag_arguments(args, TOP_FLAGS)
        if unknown:
            await ctx.send(
                embed=error_embed(
                    "Unknown flag",
                    f"`--{unknown[0]}` is not a top flag.\nAvailable: "
                    + ", ".join(f"`--{name}`" for name in TOP_FLAGS),
                    colour=await ctx.embed_color(),
                )
            )
            return
        genre_field, genre_value, genre_note = split_genre_filter(values.get("genre"))
        style = values.get("style")
        if genre_value and genre_field == "style" and not style:
            style = genre_value
            genre_value = None
        year = values.get("year")
        country = values.get("country")
        filters = {
            "genre": genre_value,
            "style": style,
            "year": year,
            "country": country,
            "sort": "want",
            "sort_order": "desc",
        }
        params = {key: value for key, value in filters.items() if value}
        if not any(params.get(key) for key in ("genre", "style", "year", "country")):
            params = {"sort": "want", "sort_order": "desc"}
        per_page = await self._per_page(ctx.guild)

        async def source(page: int):
            data = await self.api.search(
                None, kind="release", page=page, per_page=per_page, **params
            )
            return (
                [row_from_search(result) for result in data.get("results") or []],
                data.get("pagination") or {},
            )

        title = "Discogs · most wanted"
        extra = describe_search_filters(params)
        if genre_note:
            extra = f"{extra} · {genre_note}" if extra else genre_note
        await self._send_list(ctx, title=title, source=source, footer_extra=extra)

    @discogs.command(name="random", aliases=["roulette"])
    @commands.bot_has_permissions(embed_links=True)
    @commands.cooldown(3, 60, commands.BucketType.user)
    async def discogs_random(self, ctx: commands.Context, *, args: str = "") -> None:
        """Pull a random record from the database — a genre or decade helps.

        `[p]discogs random --genre Jazz --decade 60s`
        `[p]discogs random --style "Detroit Techno"`
        `[p]discogs random --year 1994`

        Flags: `--genre`, `--style`, `--decade` (`60s`, `1990s`), `--year`.
        """
        colour = await ctx.embed_color()
        values, _, unknown = parse_flag_arguments(args, RANDOM_FLAGS)
        if unknown:
            await ctx.send(
                embed=error_embed(
                    "Unknown flag",
                    f"`--{unknown[0]}` is not a random flag.\nAvailable: "
                    + ", ".join(f"`--{name}`" for name in RANDOM_FLAGS),
                    colour=colour,
                )
            )
            return
        genre = values.get("genre")
        style = values.get("style")
        decade = values.get("decade")
        year = values.get("year")
        params: Dict[str, Any] = {"type": "release"}
        if genre:
            params["genre"] = genre
        if style:
            params["style"] = style
        span = parse_decade(decade) if decade else None
        if year:
            parsed = parse_year_filter(year)
            params["year"] = parsed if parsed else year
        elif span:
            params["year"] = random.randint(span[0], span[1])
        elif not (genre or style):
            params["year"] = random.randint(1960, 2026)

        async with ctx.typing():
            try:
                # One cheap request for the result count, one for the page we pick.
                probe = await self.api.search(
                    None,
                    page=1,
                    per_page=1,
                    **{k: v for k, v in params.items() if k != "type"},
                )
                pagination = probe.get("pagination") or {}
                total_pages = int(pagination.get("pages") or 0)
                if not total_pages:
                    await ctx.send(
                        embed=error_embed(
                            "Nothing found",
                            "No release matches those filters — try a broader random.",
                            colour=colour,
                        )
                    )
                    return
                page = random.randint(1, min(total_pages, 40))
                data = await self.api.search(
                    None,
                    page=page,
                    per_page=25,
                    **{k: v for k, v in params.items() if k != "type"},
                )
                results = data.get("results") or []
                if not results:
                    await ctx.send(
                        embed=error_embed(
                            "Nothing found",
                            "Discogs returned no results for that draw.",
                            colour=colour,
                        )
                    )
                    return
                pick = random.choice(results)
                release_id = int(pick.get("id") or 0)
                pages, extras, link = await self._entity_view(
                    "release", release_id, guild=ctx.guild
                )
            except DiscogsRateLimited as exc:
                await ctx.send(
                    embed=error_embed("Slow down a moment", str(exc), colour=colour)
                )
                return
            except DiscogsNotFound as exc:
                await ctx.send(
                    embed=error_embed("Not on Discogs", str(exc), colour=colour)
                )
                return
            except DiscogsError as exc:
                await ctx.send(
                    embed=error_embed("Discogs did not answer", str(exc), colour=colour)
                )
                return
        filters = describe_search_filters(params)
        for page_embed in pages:
            base = page_embed.footer.text or "Discogs"
            page_embed.set_footer(
                text=truncate(f"{base} · random pick: {filters or 'anything'}", 2000)
            )
        view = PageView(self, pages, extras=extras, link=link)
        view.message = await ctx.send(embeds=[pages[0]], view=view)

    @discogs.command(name="token")
    @checks.is_owner()
    async def discogs_token(self, ctx: commands.Context) -> None:
        """Store a Discogs user token (bot owner).

        A token is optional — it raises the rate limit from 25 to 60 requests
        per minute. Generate one at
        <https://www.discogs.com/settings/developers> and press the button, or
        run `[p]set api discogs token,<token>`.
        """
        current = self.api.has_token
        text = (
            "A Discogs token is stored. You can replace it with the button below.\n"
            if current
            else "No Discogs token set — searches work without one, but only at "
            "25 requests per minute. A free token raises that to 60.\n"
        )
        await ctx.send(
            text + "Create one at <https://www.discogs.com/settings/developers> "
            "(“Generate new token”), or use "
            f"`{ctx.clean_prefix}set api {SHARED_API_SERVICE} token,<token>`.",
            view=SetApiView(
                default_service=SHARED_API_SERVICE, default_keys={"token": ""}
            ),
        )

    @discogs.command(name="status")
    async def discogs_status(self, ctx: commands.Context) -> None:
        """Show the API state, the cache and this server's settings."""
        colour = await ctx.embed_color()
        stats = self.api.stats()
        settings = await self._settings(guild=ctx.guild)
        embed = discord.Embed(title="Discogs status", colour=colour)
        mode = "user token" if stats["token"] else "anonymous"
        embed.add_field(
            name="API",
            value=truncate(
                f"{mode}\n{format_count(stats['requests'])} requests this session\n"
                f"limit {stats['limit']}/min · {stats['window']} in the last minute",
                600,
            ),
            inline=True,
        )
        cache_line = f"{stats['cached']} entries"
        if stats["blocked_for"]:
            cache_line += f"\nbacking off for {stats['blocked_for']} s"
        if stats["failures"]:
            cache_line += f"\n{stats['failures']} failed requests"
        embed.add_field(name="Cache", value=cache_line, inline=True)
        embed.add_field(
            name="This server",
            value=truncate(
                f"{settings.get('per_page')} results per page\n"
                f"marketplace currency: {settings.get('currency')}\n"
                f"thumbnails: {'on' if settings.get('thumbnails', True) else 'off'}",
                600,
            ),
            inline=True,
        )
        if not stats["token"]:
            embed.set_footer(
                text="No token set — [p]discogs token shows how (bot owner)."
            )
        await ctx.send(embed=embed)

    @discogs.group(name="set", aliases=["settings"])
    @checks.admin_or_permissions(manage_guild=True)
    async def discogs_set(self, ctx: commands.Context) -> None:
        """Configure this server's Discogs settings."""

    @discogs_set.command(name="perpage")
    @checks.admin_or_permissions(manage_guild=True)
    async def discogs_set_perpage(self, ctx: commands.Context, amount: int) -> None:
        """How many entries a result page shows (1-20)."""
        amount = max(1, min(MAX_PER_PAGE, amount))
        await self.config.guild(ctx.guild).per_page.set(amount)
        await ctx.tick()

    @discogs_set.command(name="currency")
    @checks.admin_or_permissions(manage_guild=True)
    async def discogs_set_currency(self, ctx: commands.Context, currency: str) -> None:
        """Currency for marketplace prices, e.g. `EUR`, `USD`, `CHF`."""
        code = (currency or "").strip().upper()
        if code not in CURRENCIES:
            await ctx.send(
                embed=error_embed(
                    "Unknown currency",
                    "Pick one of: " + ", ".join(f"`{name}`" for name in CURRENCIES),
                    colour=await ctx.embed_color(),
                )
            )
            return
        await self.config.guild(ctx.guild).currency.set(code)
        await ctx.tick()

    @discogs_set.command(name="thumbnails")
    @checks.admin_or_permissions(manage_guild=True)
    async def discogs_set_thumbnails(
        self, ctx: commands.Context, enabled: bool
    ) -> None:
        """Whether detail views show cover images."""
        await self.config.guild(ctx.guild).thumbnails.set(bool(enabled))
        await ctx.tick()

    @discogs_set.command(name="show")
    @checks.admin_or_permissions(manage_guild=True)
    async def discogs_set_show(self, ctx: commands.Context) -> None:
        """Show this server's Discogs settings."""
        settings = await self._settings(guild=ctx.guild)
        embed = discord.Embed(
            title="Discogs settings",
            colour=await ctx.embed_color(),
            description=(
                f"**Results per page:** {settings.get('per_page')}\n"
                f"**Currency:** {settings.get('currency')}\n"
                f"**Thumbnails:** {'on' if settings.get('thumbnails', True) else 'off'}"
            ),
        )
        await ctx.send(embed=embed)

    @discogs.command(name="clearcache")
    @checks.is_owner()
    async def discogs_clearcache(self, ctx: commands.Context) -> None:
        """Drop the in-memory API cache (bot owner)."""
        dropped = self.api.clear_cache()
        await ctx.send(f"Dropped {format_count(dropped)} cached Discogs responses.")
