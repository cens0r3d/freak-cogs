"""Bandcamp data access and formatting.

This module holds everything that talks to Bandcamp and everything that turns
an answer into text, with no ``discord`` or ``redbot`` imports on purpose: the
live service is testable on its own and the cog next to it only renders.

Why a hand-written client instead of ``bandcamp-async-api``: every release of
that library declares ``Requires-Python >=3.12``, and a Red bot on Python 3.11
refuses to install it at all (``pip`` reports "no versions").  The endpoints it
wraps are Bandcamp's own public JSON API and need nothing but ``aiohttp``,
which Red ships, so the four calls this cog uses are implemented here:

* ``GET  /api/fuzzysearch/1/app_autocomplete`` - search (artists, albums, tracks)
* ``GET  /api/mobile/24/tralbum_details``      - an album or a track, with tracklist
* ``POST /api/mobile/24/band_details``         - an artist/label profile and its discography
* ``GET  /api/mobile/24/tralbum_lyrics``       - lyrics, one map per request

Design notes:

* A Bandcamp URL carries no ids, and the site itself answers a datacenter IP
  with a JavaScript challenge instead of the page, so a URL is resolved through
  the search endpoint: subdomain and slug become the query, and the candidate
  whose URL matches wins.  That keeps ``[p]bandcamp album <link>`` working
  without scraping.
* Every failure the API can produce is folded into :class:`BandcampError` with
  a sentence a Discord user can act on.  Callers render that sentence instead
  of letting Red print a traceback.
* Answers are cached in memory for a few minutes; Bandcamp rate limits, and a
  channel where five people paste the same album should cost one request.
"""

from __future__ import annotations

import html as html_module
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Sequence, Tuple
from urllib.parse import quote, unquote

import aiohttp

KIND_ARTIST = "artist"
KIND_ALBUM = "album"
KIND_TRACK = "track"

#: Search answers change slowly, entity pages even slower.
SEARCH_TTL = 600
ENTITY_TTL = 1800
#: Polling a watchlist must see a new release quickly; a minute still absorbs a
#: double tick and two guilds watching the same band.
FRESH_TTL = 60
CACHE_SIZE = 256

IMAGE_BASE = "https://f4.bcbits.com/img/"
BASE_URL = "https://bandcamp.com/api"

#: One page of a tracklist field, Discord allows 1024 characters per field.
TRACKLIST_BUDGET = 1000

#: Discord lets a bot upload 20 MiB per file; that is the hard ceiling for a
#: track posted as an attachment (a 128 kbps stream is ~17 KB/s ≈ 1 MiB/min).
MAX_AUDIO_MIB = 20
DEFAULT_PLAY_MIB = 8
PLAY_CEILING_BYTES = MAX_AUDIO_MIB * 1024 * 1024


class BandcampError(Exception):
    """An expected failure, carrying a message that can be shown to a user."""


class NotFoundError(Exception):
    """Bandcamp does not know that id."""


class RateLimitError(Exception):
    """Bandcamp answered 429."""

    def __init__(self, message: str, retry_after: Optional[int] = None) -> None:
        super().__init__(message)
        self.retry_after = retry_after


class UpstreamError(Exception):
    """Bandcamp answered with an error status or something that is not JSON."""

    def __init__(self, message: str, status: Optional[int] = None) -> None:
        super().__init__(message)
        self.status = status


class AudioTooLarge(Exception):
    """A track does not fit into the upload budget."""

    def __init__(self, size: int, cap: int) -> None:
        super().__init__(f"audio is {size} bytes, cap is {cap}")
        self.size = size
        self.cap = cap


# --------------------------------------------------------------------------
# models
# --------------------------------------------------------------------------


@dataclass
class Artist:
    """A band or label page."""

    id: int
    name: str
    url: Optional[str] = None
    image_url: Optional[str] = None
    location: Optional[str] = None
    bio: Optional[str] = None
    is_label: bool = False
    tags: List[str] = field(default_factory=list)
    genre: Optional[str] = None


@dataclass
class Track:
    """One track, either standalone or a row of an album's tracklist."""

    id: int
    title: str
    artist: Artist
    url: Optional[str] = None
    art_url: Optional[str] = None
    duration: Optional[float] = None
    streaming_url: Optional[Dict[str, str]] = None
    track_number: int = 0
    has_lyrics: bool = False
    lyrics: Optional[str] = None
    about: Optional[str] = None
    credits: Optional[str] = None
    album_id: Optional[int] = None
    album_title: Optional[str] = None
    tralbum_artist: Optional[str] = None
    label: Optional[str] = None
    type: str = KIND_TRACK


@dataclass
class Album:
    """One release with its tracklist."""

    id: int
    title: str
    artist: Artist
    url: Optional[str] = None
    art_url: Optional[str] = None
    release_date: Optional[int] = None
    price: Optional[Dict[str, Any]] = None
    is_free: bool = False
    is_set_price: bool = False
    is_purchasable: bool = False
    is_preorder: bool = False
    about: Optional[str] = None
    credits: Optional[str] = None
    tags: List[str] = field(default_factory=list)
    total_tracks: int = 0
    tracks: List[Track] = field(default_factory=list)
    type: str = KIND_ALBUM
    tralbum_artist: Optional[str] = None
    label: Optional[str] = None
    label_id: Optional[int] = None
    copyright: Optional[str] = None


@dataclass
class SearchHit:
    """One row of a search answer."""

    type: str
    id: int
    name: str
    url: Optional[str] = None
    artist_id: Optional[int] = None
    artist_name: Optional[str] = None
    artist_url: Optional[str] = None
    album_id: Optional[int] = None
    album_name: Optional[str] = None
    image_url: Optional[str] = None
    location: Optional[str] = None
    is_label: bool = False
    genre: Optional[str] = None
    tags: List[str] = field(default_factory=list)
    release_date: Optional[int] = None


# --------------------------------------------------------------------------
# URLs
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class ParsedURL:
    """A Bandcamp link split into what the search endpoint needs."""

    url: str
    kind: str
    subdomain: str
    slug: str

    @property
    def key(self) -> str:
        """A comparable identity: ``subdomain/kind/slug``."""
        if self.kind == KIND_ARTIST:
            return f"{self.subdomain}/{KIND_ARTIST}".lower()
        return f"{self.subdomain}/{self.kind}/{self.slug}".lower()

    @property
    def search_query(self) -> str:
        """The words to hand the search endpoint for this link."""
        words = self.slug.replace("-", " ").replace("_", " ").strip()
        return f"{self.subdomain} {words}".strip()

    @property
    def artist_url(self) -> str:
        """The band page that owns this link."""
        return subdomain_url(self.subdomain)


_URL_RE = re.compile(
    r"(?:https?://)?(?P<sub>[a-z0-9][a-z0-9-]*)\.bandcamp\.com"
    r"(?:/(?P<kind>album|track)/(?P<slug>[^/?#\s]+))?",
    re.IGNORECASE,
)


def subdomain_url(subdomain: str) -> str:
    """The band page URL for a subdomain."""
    return f"https://{subdomain}.bandcamp.com"


def band_url_from(url: Optional[str]) -> Optional[str]:
    """The band page for a release URL: ``https://sub.bandcamp.com``."""
    parsed = parse_bandcamp_url(url or "")
    if parsed is not None:
        return parsed.artist_url
    match = _URL_RE.match((url or "").strip())
    return subdomain_url(match.group("sub").lower()) if match else None


def parse_bandcamp_url(text: str) -> Optional[ParsedURL]:
    """Split a Bandcamp link into its parts, or return ``None``.

    Only item links and band pages carry an identity.  The front page,
    ``/discover`` and ``bandcamp.com/<anything>`` have none, so they are
    rejected instead of being sent to the search endpoint.
    """
    match = _URL_RE.search(text or "")
    if not match:
        return None
    subdomain = match.group("sub").lower()
    if subdomain in {"bandcamp", "www", "daily"}:
        return None
    kind = (match.group("kind") or KIND_ARTIST).lower()
    slug = unquote(match.group("slug") or "")
    if kind != KIND_ARTIST and not slug:
        return None
    if kind == KIND_ARTIST:
        url = subdomain_url(subdomain)
    else:
        url = f"{subdomain_url(subdomain)}/{kind}/{quote(slug)}"
    return ParsedURL(url=url, kind=kind, subdomain=subdomain, slug=slug)


def collect_bandcamp_urls(text: str, limit: int = 3) -> List[ParsedURL]:
    """Every distinct Bandcamp link in a message, in the order they appear."""
    found: List[ParsedURL] = []
    seen: set[str] = set()
    for match in _URL_RE.finditer(text or ""):
        parsed = parse_bandcamp_url(match.group(0))
        if parsed is None or parsed.key in seen:
            continue
        seen.add(parsed.key)
        found.append(parsed)
        if len(found) >= limit:
            break
    return found


def clean_url(raw: Optional[str]) -> Optional[str]:
    """Repair the search endpoint's glued URLs.

    Search rows sometimes answer
    ``https://a.bandcamp.comhttps://a.bandcamp.com/track/x``; the last complete
    URL in the string is the real one.
    """
    text = (raw or "").strip()
    if not text:
        return None
    last = max(text.rfind("https://"), text.rfind("http://"))
    if last > 0:
        text = text[last:]
    return text or None


def cover_url(art_id: Any) -> Optional[str]:
    """Cover art URL for an ``art_id``."""
    try:
        number = int(art_id)
    except (TypeError, ValueError):
        return None
    if number <= 0:
        return None
    return f"{IMAGE_BASE}a{number}_0.jpg"


def band_image_url(image_id: Any) -> Optional[str]:
    """Profile image URL for a band's ``image_id``."""
    try:
        number = int(image_id)
    except (TypeError, ValueError):
        return None
    if number <= 0:
        return None
    return f"{IMAGE_BASE}000{number}_0.png"


def track_art_url(art_id: Any) -> Optional[str]:
    """Cover URL for a release id from the discography endpoint."""
    return cover_url(art_id)


def audio_filename(artist: Any, title: Any, limit: int = 110) -> str:
    """``Artist - Title.mp3``, with the characters Discord rejects replaced.

    Windows and Discord both choke on ``\\ / : * ? " < > |``; the name is what
    the client shows above the player, so it keeps the artist and the title.
    """
    base = f"{artist or 'Unknown artist'} - {title or 'Untitled'}"
    cleaned = re.sub(r'[\\/:*?"<>|\x00-\x1f]', "_", base).strip(" .")
    cleaned = " ".join(cleaned.split())
    if len(cleaned) > limit:
        cleaned = cleaned[:limit].rstrip(" .")
    return f"{cleaned or 'track'}.mp3"


# --------------------------------------------------------------------------
# text helpers
# --------------------------------------------------------------------------

_TAG_RE = re.compile(r"<[^>]+>")
_SPACE_RE = re.compile(r"[ \t\u00a0]+")
_BLANK_LINES_RE = re.compile(r"\n{3,}")


def strip_markup(text: Optional[str]) -> str:
    """Turn Bandcamp's HTML-ish descriptions into plain text.

    Bandcamp's payload mixes plain text with a little HTML, so both a tag strip
    and an entity unescape are needed; ``<br>`` and paragraph ends turn into
    newlines first so the text does not run together.
    """
    if not text:
        return ""
    cleaned = re.sub(r"(?i)<br\s*/?>", "\n", text)
    cleaned = re.sub(r"(?i)</p\s*>", "\n\n", cleaned)
    cleaned = _TAG_RE.sub("", cleaned)
    cleaned = html_module.unescape(cleaned)
    cleaned = _SPACE_RE.sub(" ", cleaned)
    cleaned = _BLANK_LINES_RE.sub("\n\n", cleaned)
    return cleaned.strip()


def truncate(text: str, limit: int) -> str:
    """Cut text to ``limit`` characters, ending on a word when possible."""
    text = text or ""
    if len(text) <= limit:
        return text
    if limit <= 1:
        return text[:limit]
    cut = text[: limit - 1]
    space = cut.rfind(" ")
    if space > limit * 0.6:
        cut = cut[:space]
    return cut.rstrip() + "…"


def format_duration(seconds: Optional[float]) -> str:
    """``238.4`` -> ``3:58``, ``3725`` -> ``1:02:05``."""
    if seconds is None:
        return "—"
    try:
        total = int(round(float(seconds)))
    except (TypeError, ValueError):
        return "—"
    if total < 0:
        return "—"
    hours, rest = divmod(total, 3600)
    minutes, secs = divmod(rest, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"


def format_date(value: Any) -> str:
    """A Bandcamp release timestamp (UTC seconds) as ``03 Mar 2016``."""
    if value is None:
        return "—"
    if isinstance(value, str):
        text = value.strip()
        # The discography list answers "01 Aug 2025 00:00:00 GMT".
        for pattern in ("%d %b %Y %H:%M:%S %Z", "%d %b %Y %H:%M:%S", "%Y-%m-%d"):
            try:
                parsed = datetime.strptime(text, pattern)
            except ValueError:
                continue
            return parsed.strftime("%d %b %Y")
        return text
    try:
        stamp = int(value)
    except (TypeError, ValueError):
        return "—"
    if stamp <= 0:
        return "—"
    return datetime.fromtimestamp(stamp, tz=timezone.utc).strftime("%d %b %Y")


def format_price(
    price: Optional[Dict[str, Any]],
    *,
    is_free: bool = False,
    is_set_price: bool = False,
    is_purchasable: bool = False,
    is_preorder: bool = False,
) -> str:
    """What the release page's price line says, in one short phrase."""
    if is_preorder:
        return "Preorder"
    amount = None
    currency = ""
    if isinstance(price, dict):
        amount = price.get("amount")
        currency = str(price.get("currency") or "")
    if is_free or amount in (0, 0.0):
        if is_set_price or is_purchasable:
            return "Name your price"
        return "Free"
    if amount is None:
        return "—"
    try:
        return f"{float(amount):.2f} {currency}".strip()
    except (TypeError, ValueError):
        return "—"


def format_tags(tags: Optional[Sequence[str]], limit: int = 6) -> str:
    """Bandcamp tags as one line, capped at ``limit`` entries."""
    cleaned = [tag.strip() for tag in (tags or []) if tag and tag.strip()]
    if not cleaned:
        return ""
    return " · ".join(cleaned[:limit])


def format_discography_date(value: Any) -> str:
    """The discography endpoint's own date format, kept as a year when it can."""
    text = format_date(value)
    if text.count(" ") == 2:
        return text.split(" ")[2]
    return text


def plural(count: int, singular: str, plural_form: Optional[str] = None) -> str:
    """``3 tracks`` / ``1 track``."""
    if count == 1:
        return f"{count} {singular}"
    return f"{count} {plural_form or singular + 's'}"


# --------------------------------------------------------------------------
# search ranking
# --------------------------------------------------------------------------


def normalize(text: str) -> str:
    """Fold a name down to its comparable words."""
    value = (text or "").casefold().replace("&", " and ")
    value = re.sub(r"[’']s\b", "", value)
    value = re.sub(r"[^a-z0-9]+", " ", value)
    return " ".join(value.split())


def _url_key(url: Optional[str]) -> str:
    parsed = parse_bandcamp_url(url or "")
    return parsed.key if parsed else ""


def score_result(item: Any, query: str, parsed: Optional[ParsedURL]) -> int:
    """How well a search hit matches what the caller asked for."""
    score = 0
    item_type = getattr(item, "type", "")
    name = normalize(getattr(item, "name", "") or "")
    if parsed is not None:
        if item_type == parsed.kind:
            score += 40
        if _url_key(getattr(item, "url", "")) == parsed.key:
            score += 100
        artist_url = getattr(item, "artist_url", "") or ""
        if parsed.kind == KIND_ARTIST and _url_key(artist_url) == parsed.key:
            score += 60
        words = normalize(parsed.search_query)
        if words and words in name:
            score += 20
        return score
    wanted = normalize(query)
    if not wanted:
        return score
    if name == wanted:
        score += 40
    elif name.startswith(wanted):
        score += 25
    elif wanted in name:
        score += 15
    tokens = wanted.split()
    if tokens and all(token in name.split() for token in tokens):
        score += 10
    # A release by the searched artist matters as much as its own title: a
    # query for a band has to surface that band's albums above a tribute act's.
    artist_name = normalize(getattr(item, "artist_name", "") or "")
    if artist_name and artist_name == wanted:
        score += 30
    elif artist_name and wanted in artist_name:
        score += 12
    return score


def rank_results(
    results: Sequence[Any],
    query: str,
    parsed: Optional[ParsedURL] = None,
    kind: Optional[str] = None,
) -> List[Any]:
    """Search hits ordered by how well they match, best first."""
    pool = [item for item in results if kind is None or item.type == kind]
    scored = [
        (score_result(item, query, parsed), index, item)
        for index, item in enumerate(pool)
    ]
    scored.sort(key=lambda entry: (-entry[0], entry[1]))
    return [entry[2] for entry in scored]


def describe_result(item: Any) -> str:
    """A one-line subtitle for a search hit: what it is and who made it."""
    kind = getattr(item, "type", "item")
    if kind == KIND_ARTIST:
        bits = [getattr(item, "location", "") or ""]
        if getattr(item, "is_label", False):
            bits.append("label")
        genre = getattr(item, "genre", "") or ""
        if genre:
            bits.append(genre)
        return " · ".join(bit for bit in bits if bit) or "Artist"
    if kind == KIND_ALBUM:
        parts = [getattr(item, "artist_name", "") or ""]
        released = getattr(item, "release_date", None)
        if released:
            parts.append(format_date(released))
        return " · ".join(part for part in parts if part) or "Album"
    artist = getattr(item, "artist_name", "") or ""
    album = getattr(item, "album_name", "") or ""
    if artist and album:
        return f"{artist} · {album}"
    return artist or album or "Track"


def result_row(item: Any) -> Dict[str, Any]:
    """A search hit as the plain dict the views and embeds work with."""
    kind = getattr(item, "type", "item")
    row = {
        "kind": kind,
        "id": int(getattr(item, "id", 0) or 0),
        "title": getattr(item, "name", "") or "Untitled",
        "url": getattr(item, "url", "") or "",
        "subtitle": describe_result(item),
        "image": getattr(item, "image_url", None),
    }
    artist_id = getattr(item, "artist_id", None)
    if artist_id:
        row["artist_id"] = int(artist_id)
    if kind == KIND_TRACK:
        album_id = getattr(item, "album_id", None)
        if album_id:
            row["album_id"] = int(album_id)
    return row


# --------------------------------------------------------------------------
# watchlist diffs
# --------------------------------------------------------------------------


def release_key(row: Dict[str, Any]) -> str:
    """A stable identity for a discography row."""
    item_id = row.get("item_id")
    return str(item_id) if item_id else ""


def sort_releases(rows: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Rows newest first, sorted on the parsed date.

    The endpoint's own order is not chronological, so a comparison has to parse
    the date it prints (``01 Aug 2025 00:00:00 GMT``).
    """

    def stamp(row: Dict[str, Any]) -> float:
        text = str(row.get("release_date") or "")
        for pattern in ("%d %b %Y %H:%M:%S %Z", "%d %b %Y %H:%M:%S", "%Y-%m-%d"):
            try:
                return datetime.strptime(text, pattern).timestamp()
            except ValueError:
                continue
        return 0.0

    return sorted(rows, key=stamp, reverse=True)


def new_releases(
    rows: Sequence[Dict[str, Any]], known: Sequence[str], limit: int = 5
) -> List[Dict[str, Any]]:
    """Rows whose id is not in ``known``, newest first, at most ``limit``."""
    seen = {str(entry) for entry in known}
    fresh = [row for row in rows if release_key(row) and release_key(row) not in seen]
    return sort_releases(fresh)[:limit]


def known_keys(rows: Sequence[Dict[str, Any]], cap: int = 400) -> List[str]:
    """The ids worth remembering for a followed band, newest first.

    A label can list hundreds of releases; the cap keeps the stored list small.
    Only the oldest entries fall off, so a release that is still current never
    comes back as "new".
    """
    keys = [release_key(row) for row in sort_releases(rows) if release_key(row)]
    return keys[:cap]


# --------------------------------------------------------------------------
# tracklist chunks
# --------------------------------------------------------------------------


def format_tracklist(
    tracks: Sequence[Any], budget: int = TRACKLIST_BUDGET
) -> List[str]:
    """Numbered tracklist chunks, each small enough for one embed field."""
    lines: List[str] = []
    for position, track in enumerate(tracks, start=1):
        number = getattr(track, "track_number", 0) or position
        title = getattr(track, "title", "") or "Untitled"
        duration = format_duration(getattr(track, "duration", None))
        url = getattr(track, "url", None)
        label = f"[{title}]({url})" if url else title
        lines.append(f"`{int(number):02d}` {label} · {duration}")
    return chunk_lines(lines, budget)


def chunk_lines(lines: Sequence[str], budget: int) -> List[str]:
    """Greedily pack lines into chunks that stay under ``budget`` characters."""
    chunks: List[str] = []
    current: List[str] = []
    size = 0
    for line in lines:
        cost = len(line) + 1
        if current and size + cost > budget:
            chunks.append("\n".join(current))
            current = []
            size = 0
        current.append(truncate(line, budget))
        size += cost
    if current:
        chunks.append("\n".join(current))
    return chunks


def split_text(text: str, budget: int) -> List[str]:
    """Split prose into chunks on paragraph, then line, then word boundaries."""
    text = (text or "").strip()
    if not text:
        return []
    if len(text) <= budget:
        return [text]
    chunks: List[str] = []
    current = ""
    for paragraph in text.split("\n\n"):
        piece = paragraph if not current else "\n\n" + paragraph
        if len(current) + len(piece) <= budget:
            current += piece
            continue
        if current:
            chunks.append(current)
            current = ""
        while len(paragraph) > budget:
            cut = paragraph.rfind(" ", 0, budget)
            cut = cut if cut > 0 else budget
            chunks.append(paragraph[:cut].rstrip())
            paragraph = paragraph[cut:].lstrip()
        current = paragraph
    if current:
        chunks.append(current)
    return chunks


# --------------------------------------------------------------------------
# cache
# --------------------------------------------------------------------------


class TTLCache:
    """A tiny mapping with a per-entry lifetime and a capacity cap."""

    def __init__(self, size: int = CACHE_SIZE) -> None:
        self.size = size
        self._data: Dict[Any, Tuple[float, Any]] = {}

    def get(self, key: Any) -> Any:
        entry = self._data.get(key)
        if entry is None:
            return None
        expires, value = entry
        if expires < time.monotonic():
            self._data.pop(key, None)
            return None
        return value

    def set(self, key: Any, value: Any, ttl: int) -> None:
        if len(self._data) >= self.size:
            self._drop_expired()
        if len(self._data) >= self.size:
            oldest = min(self._data, key=lambda item: self._data[item][0])
            self._data.pop(oldest, None)
        self._data[key] = (time.monotonic() + ttl, value)

    def _drop_expired(self) -> None:
        now = time.monotonic()
        for key in [key for key, (expires, _) in self._data.items() if expires < now]:
            self._data.pop(key, None)

    def clear(self) -> None:
        self._data.clear()

    def __len__(self) -> int:
        return len(self._data)


# --------------------------------------------------------------------------
# the client
# --------------------------------------------------------------------------


def _tag_names(raw_tags: Any) -> List[str]:
    """Bandcamp's tag objects as plain names, skipping anything odd."""
    names: List[str] = []
    for tag in raw_tags or []:
        if isinstance(tag, dict):
            name = tag.get("name")
            if isinstance(name, str) and name.strip():
                names.append(name.strip())
        elif isinstance(tag, str) and tag.strip():
            names.append(tag.strip())
    return names


def _int_or_zero(raw: Optional[str]) -> int:
    """A header value as an int, or ``0`` when it is missing or odd."""
    try:
        return int(raw or "")
    except (TypeError, ValueError):
        return 0


def _retry_after(raw: Optional[str]) -> Optional[int]:
    """``Retry-After`` as seconds, or ``None`` when it is missing or odd."""
    try:
        return int(raw or "")
    except (TypeError, ValueError):
        return None


class BandcampClient:
    """The four Bandcamp endpoints this cog needs, with no token involved."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        *,
        timeout: float = 25.0,
        user_agent: str = "freak-cogs-bandcamp/1.0 (+Discord bot)",
    ) -> None:
        # The session belongs to the cog, which opens it in `cog_load` and
        # closes it in `cog_unload`; this client only borrows it.
        self._session = session
        self._user_agent = user_agent
        self._timeout = aiohttp.ClientTimeout(total=timeout, sock_connect=10)

    async def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        """One call, with Bandcamp's failure answers turned into exceptions."""
        session = self._session
        headers = {"User-Agent": self._user_agent, "Accept": "application/json"}
        url = path if path.startswith("http") else f"{BASE_URL}{path}"
        async with session.request(
            method, url, headers=headers, timeout=self._timeout, **kwargs
        ) as response:
            if response.status == 429:
                raise RateLimitError(
                    "Bandcamp rate limit",
                    retry_after=_retry_after(response.headers.get("Retry-After")),
                )
            if response.status == 404:
                raise NotFoundError("Bandcamp has no such id")
            if response.status >= 400:
                raise UpstreamError(
                    f"Bandcamp answered HTTP {response.status}", status=response.status
                )
            try:
                # Bandcamp serves JSON as text/plain on some paths.
                data = await response.json(content_type=None)
            except (aiohttp.ClientError, ValueError) as error:
                raise UpstreamError(
                    "Bandcamp answered something that is not JSON "
                    f"(HTTP {response.status})",
                    status=response.status,
                ) from error
        if isinstance(data, dict) and data.get("error"):
            message = str(data.get("error_message") or "Bandcamp refused the request")
            if "No such" in message:
                raise NotFoundError(message)
            raise UpstreamError(message)
        return data

    # -- search -----------------------------------------------------------
    async def search(self, query: str) -> List[SearchHit]:
        """Search artists, albums and tracks."""
        data = await self._request(
            "GET",
            "/fuzzysearch/1/app_autocomplete",
            params={
                "q": (query or "").replace(",", " "),
                "param_with_locations": "true",
            },
        )
        hits: List[SearchHit] = []
        for raw in data.get("results") or []:
            hit = self._parse_search_hit(raw)
            if hit is not None:
                hits.append(hit)
        return hits

    def _parse_search_hit(self, raw: Dict[str, Any]) -> Optional[SearchHit]:
        kind = {"b": KIND_ARTIST, "a": KIND_ALBUM, "t": KIND_TRACK}.get(raw.get("type"))
        if kind is None or not raw.get("id"):
            return None
        url = clean_url(raw.get("url"))
        if kind == KIND_ARTIST:
            artist_id = int(raw["id"])
            artist_name = raw.get("name") or ""
            artist_url = url
            image = band_image_url(raw.get("img_id"))
            album_id = None
            album_name = None
        elif kind == KIND_ALBUM:
            artist_id = int(raw.get("band_id") or 0)
            artist_name = raw.get("band_name") or ""
            artist_url = band_url_from(url)
            image = cover_url(raw.get("art_id"))
            album_id = int(raw["id"])
            album_name = raw.get("name") or ""
        else:
            artist_id = int(raw.get("band_id") or 0)
            artist_name = raw.get("band_name") or ""
            artist_url = band_url_from(url)
            image = cover_url(raw.get("art_id"))
            album_id = int(raw["album_id"]) if raw.get("album_id") else None
            album_name = raw.get("album_name") or ""
        return SearchHit(
            type=kind,
            id=int(raw["id"]),
            name=raw.get("name") or "Untitled",
            url=url,
            artist_id=artist_id or None,
            artist_name=artist_name or None,
            artist_url=artist_url,
            album_id=album_id,
            album_name=album_name or None,
            image_url=image,
            location=raw.get("location") or None,
            is_label=bool(raw.get("is_label")),
            genre=raw.get("genre_name") or None,
            tags=_tag_names(raw.get("tag_names")),
        )

    # -- releases ---------------------------------------------------------
    async def get_album(
        self, band_id: Any, album_id: Any, *, with_lyrics: bool = False
    ) -> Album:
        """One release with its tracklist."""
        data = await self._tralbum(band_id, album_id, KIND_ALBUM)
        album = self._parse_album(data)
        if with_lyrics and album.tracks:
            await self._attach_lyrics(album.tracks, album_id, "a")
        return album

    async def get_track(
        self, band_id: Any, track_id: Any, *, with_lyrics: bool = False
    ) -> Track:
        """One track."""
        data = await self._tralbum(band_id, track_id, KIND_TRACK)
        track = self._parse_track(data)
        if with_lyrics:
            await self._attach_lyrics([track], track.id, "t")
        return track

    async def _tralbum(
        self, band_id: Any, tralbum_id: Any, kind: str
    ) -> Dict[str, Any]:
        return await self._request(
            "GET",
            "/mobile/24/tralbum_details",
            params={
                "band_id": band_id,
                "tralbum_id": tralbum_id,
                "tralbum_type": "a" if kind == KIND_ALBUM else "t",
            },
        )

    def _artist_from_payload(self, data: Dict[str, Any]) -> Artist:
        band = data.get("band") or {}
        band_id = band.get("band_id") or data.get("band_id") or 0
        return Artist(
            id=int(band_id or 0),
            name=band.get("name") or data.get("tralbum_artist") or "Unknown artist",
            url=band_url_from(data.get("bandcamp_url")),
            image_url=band_image_url(band.get("image_id")),
            location=band.get("location") or None,
            bio=band.get("bio") or None,
        )

    def _parse_album(self, data: Dict[str, Any]) -> Album:
        artist = self._artist_from_payload(data)
        raw_tracks = data.get("tracks") or []
        tracks = [self._parse_album_track(raw, artist, data) for raw in raw_tracks]
        price = data.get("price")
        currency = data.get("currency")
        price_info = (
            {"amount": price, "currency": currency}
            if price is not None and currency
            else None
        )
        single = len(raw_tracks) == 1 and raw_tracks[0].get("track_id") == data.get(
            "id"
        )
        return Album(
            id=int(data.get("id") or 0),
            title=data.get("title") or data.get("album_title") or "Untitled",
            artist=artist,
            url=clean_url(data.get("bandcamp_url")),
            art_url=cover_url(data.get("art_id")),
            release_date=data.get("release_date"),
            price=price_info,
            is_free=price in (0, 0.0),
            is_set_price=bool(data.get("is_set_price")),
            is_purchasable=bool(data.get("is_purchasable")),
            is_preorder=bool(data.get("is_preorder")),
            about=data.get("about"),
            credits=data.get("credits"),
            tags=_tag_names(data.get("tags")),
            total_tracks=int(data.get("num_downloadable_tracks") or len(tracks)),
            tracks=tracks,
            type=KIND_TRACK if single else KIND_ALBUM,
            tralbum_artist=data.get("tralbum_artist"),
            label=data.get("label") or None,
            label_id=int(data.get("label_id") or 0) or None,
        )

    def _parse_album_track(
        self, raw: Dict[str, Any], artist: Artist, album: Dict[str, Any]
    ) -> Track:
        return Track(
            id=int(raw.get("track_id") or 0),
            title=raw.get("title") or "Untitled",
            artist=artist,
            url=clean_url(raw.get("track_url")),
            art_url=cover_url(raw.get("art_id")) or cover_url(album.get("art_id")),
            duration=raw.get("duration"),
            streaming_url=raw.get("streaming_url"),
            track_number=int(raw.get("track_num") or 0),
            has_lyrics=bool(raw.get("has_lyrics")),
            about=raw.get("about"),
            credits=raw.get("credits"),
            album_id=int(raw.get("album_id") or album.get("id") or 0) or None,
            album_title=raw.get("album_title") or album.get("title"),
            tralbum_artist=album.get("tralbum_artist"),
        )

    def _parse_track(self, data: Dict[str, Any]) -> Track:
        artist = self._artist_from_payload(data)
        raw = (data.get("tracks") or [{}])[0] or {}
        return Track(
            id=int(data.get("id") or raw.get("track_id") or 0),
            title=data.get("title") or raw.get("title") or "Untitled",
            artist=artist,
            url=clean_url(data.get("bandcamp_url")) or clean_url(raw.get("track_url")),
            art_url=cover_url(data.get("art_id")) or cover_url(raw.get("art_id")),
            duration=raw.get("duration"),
            streaming_url=raw.get("streaming_url"),
            track_number=int(raw.get("track_num") or 0),
            has_lyrics=bool(raw.get("has_lyrics")),
            about=data.get("about"),
            credits=data.get("credits"),
            album_id=int(data.get("album_id") or 0) or None,
            album_title=data.get("album_title") or raw.get("album_title"),
            tralbum_artist=data.get("tralbum_artist"),
            label=data.get("label") or None,
        )

    # -- audio ------------------------------------------------------------
    async def fetch_audio(self, url: str, *, max_bytes: int) -> Tuple[bytes, str]:
        """Stream one preview MP3 into memory, refusing anything over the cap.

        The signed ``stream_redirect`` link answers a 302 to a CDN host, so the
        declared ``Content-Length`` is checked before the body and the running
        length during it: the header is a claim, not a guarantee.
        """
        session = self._session
        headers = {"User-Agent": self._user_agent, "Accept": "audio/mpeg,*/*"}
        async with session.get(url, headers=headers, timeout=self._timeout) as response:
            if response.status == 429:
                raise RateLimitError(
                    "Bandcamp rate limit",
                    retry_after=_retry_after(response.headers.get("Retry-After")),
                )
            if response.status == 404:
                raise NotFoundError("That stream is gone")
            if response.status >= 400:
                raise UpstreamError(
                    f"Bandcamp answered HTTP {response.status}", status=response.status
                )
            declared = _int_or_zero(response.headers.get("Content-Length"))
            if declared > max_bytes:
                raise AudioTooLarge(declared, max_bytes)
            buffer = bytearray()
            async for chunk in response.content.iter_chunked(64 * 1024):
                buffer.extend(chunk)
                if len(buffer) > max_bytes:
                    raise AudioTooLarge(len(buffer), max_bytes)
            content_type = response.headers.get("Content-Type") or "audio/mpeg"
        if not buffer:
            raise UpstreamError("Bandcamp sent an empty file")
        return bytes(buffer), content_type

    # -- artists ----------------------------------------------------------
    async def get_artist(self, band_id: Any) -> Artist:
        """An artist or label profile."""
        data = await self._request(
            "POST", "/mobile/24/band_details", json={"band_id": band_id}
        )
        return self._parse_artist(data)

    async def get_artist_discography(self, band_id: Any) -> List[Dict[str, Any]]:
        """Every release of an artist: the profile payload's discography rows."""
        data = await self._request(
            "POST", "/mobile/24/band_details", json={"band_id": band_id}
        )
        return list(data.get("discography") or [])

    def _parse_artist(self, data: Dict[str, Any]) -> Artist:
        return Artist(
            id=int(data.get("id") or 0),
            name=data.get("name") or "Unknown artist",
            url=clean_url(data.get("bandcamp_url")),
            image_url=band_image_url(data.get("bio_image_id")),
            location=data.get("location") or None,
            bio=data.get("bio") or None,
            is_label=bool(data.get("is_label")),
            tags=_tag_names(data.get("tags")),
            genre=data.get("genre_name") or None,
        )

    # -- lyrics -----------------------------------------------------------
    async def get_lyrics(
        self, tralbum_id: Any, tralbum_type: str = "t"
    ) -> Dict[int, Optional[str]]:
        """Track id to lyrics text, for one track or for a whole release."""
        data = await self._request(
            "GET",
            "/mobile/24/tralbum_lyrics",
            params={"tralbum_id": tralbum_id, "tralbum_type": tralbum_type},
        )
        lyrics: Dict[int, Optional[str]] = {}
        for track_id, text in (data.get("lyrics") or {}).items():
            try:
                lyrics[int(track_id)] = text
            except (TypeError, ValueError):
                continue
        return lyrics

    async def _attach_lyrics(
        self, tracks: Sequence[Track], tralbum_id: Any, tralbum_type: str
    ) -> None:
        """Fill in the lyrics of ``tracks`` with one extra request.

        A release without a single ``has_lyrics`` flag costs nothing, and a
        failed lyrics call never breaks the caller: the text stays ``None``.
        """
        if not any(track.has_lyrics for track in tracks):
            return
        try:
            lyrics = await self.get_lyrics(tralbum_id, tralbum_type)
        except (
            NotFoundError,
            RateLimitError,
            UpstreamError,
            aiohttp.ClientError,
            TimeoutError,
        ):
            return
        for track in tracks:
            track.lyrics = lyrics.get(track.id)


# --------------------------------------------------------------------------
# service
# --------------------------------------------------------------------------


@dataclass
class Resolved:
    """What a user's text or link turned out to be."""

    kind: str
    item: Any
    query: str
    from_url: bool = False


def _size(num_bytes: int) -> str:
    """Bytes as ``783 KiB`` or ``8.0 MiB``, whichever reads better."""
    if num_bytes < 1024 * 1024:
        return f"{num_bytes / 1024:.0f} KiB"
    return f"{num_bytes / (1024 * 1024):.1f} MiB"


def _friendly(error: BaseException) -> str:
    """A sentence for a failed Bandcamp call."""
    if isinstance(error, NotFoundError):
        return "Bandcamp has no such release, track or artist."
    if isinstance(error, RateLimitError):
        wait = getattr(error, "retry_after", None)
        if wait:
            return f"Bandcamp is rate limiting the bot. Try again in about {int(wait)} seconds."
        return "Bandcamp is rate limiting the bot. Try again in a minute."
    if isinstance(error, AudioTooLarge):
        return (
            f"That track is about {_size(error.size)} and the upload cap here is "
            f"{_size(error.cap)} — too long to post as a file. "
            "Open it on Bandcamp instead."
        )
    if isinstance(error, UpstreamError):
        return f"{error}. Try again later."
    if isinstance(error, TimeoutError):
        return "Bandcamp did not answer in time. Try again."
    if isinstance(error, aiohttp.ClientError):
        return "Could not reach Bandcamp. Check the bot's connection and try again."
    return f"Bandcamp lookup failed: {error}"


class BandcampService:
    """Search, releases, artists and lyrics, cached and error-friendly."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        *,
        timeout: float = 25.0,
        user_agent: str = "freak-cogs-bandcamp/1.0 (+Discord bot)",
    ) -> None:
        self.client = BandcampClient(
            session=session, timeout=timeout, user_agent=user_agent
        )
        self.cache = TTLCache()

    # -- internals --------------------------------------------------------
    async def _call(self, coro: Any) -> Any:
        try:
            return await coro
        except (
            NotFoundError,
            RateLimitError,
            UpstreamError,
            AudioTooLarge,
            aiohttp.ClientError,
            TimeoutError,
        ) as error:
            raise BandcampError(_friendly(error)) from error

    async def _cached(self, key: Any, ttl: int, factory: Any) -> Any:
        cached = self.cache.get(key)
        if cached is not None:
            return cached
        value = await self._call(factory())
        self.cache.set(key, value, ttl)
        return value

    # -- lookups ----------------------------------------------------------
    async def search(self, query: str) -> List[SearchHit]:
        """Search artists, albums and tracks."""
        cleaned = (query or "").strip()
        if not cleaned:
            raise BandcampError("Give me something to search for.")
        return await self._cached(
            ("search", cleaned.casefold()),
            SEARCH_TTL,
            lambda: self.client.search(cleaned),
        )

    async def album(
        self, artist_id: Any, album_id: Any, *, with_lyrics: bool = False
    ) -> Album:
        """One release with its tracklist."""
        key = ("album", int(artist_id), int(album_id), bool(with_lyrics))
        return await self._cached(
            key,
            ENTITY_TTL,
            lambda: self.client.get_album(artist_id, album_id, with_lyrics=with_lyrics),
        )

    async def track(
        self, artist_id: Any, track_id: Any, *, with_lyrics: bool = False
    ) -> Track:
        """One track."""
        key = ("track", int(artist_id), int(track_id), bool(with_lyrics))
        return await self._cached(
            key,
            ENTITY_TTL,
            lambda: self.client.get_track(artist_id, track_id, with_lyrics=with_lyrics),
        )

    async def artist(self, artist_id: Any) -> Artist:
        """One artist or label profile."""
        return await self._cached(
            ("artist", int(artist_id)),
            ENTITY_TTL,
            lambda: self.client.get_artist(artist_id),
        )

    async def discography(
        self, artist_id: Any, *, fresh: bool = False
    ) -> List[Dict[str, Any]]:
        """Every release of an artist as the raw API rows.

        The rows are **not** in date order (Burial answers 2025, 2011, 2024), so
        callers must diff on ``item_id`` — see :func:`new_releases`. ``fresh``
        shortens the cache to a minute, which is what a watch poll needs.
        """
        if not fresh:
            return await self._cached(
                ("discography", int(artist_id)),
                ENTITY_TTL,
                lambda: self.client.get_artist_discography(artist_id),
            )
        return await self._cached(
            ("discography-fresh", int(artist_id)),
            FRESH_TTL,
            lambda: self.client.get_artist_discography(artist_id),
        )

    async def track_lyrics(
        self, artist_id: Any, track_id: Any
    ) -> Tuple[Any, Optional[str]]:
        """A track plus its lyrics text, at the cost of one extra request."""
        key = ("lyrics-track", int(artist_id), int(track_id))
        return await self._cached(
            key, ENTITY_TTL, lambda: self._track_lyrics(artist_id, track_id)
        )

    async def _track_lyrics(
        self, artist_id: Any, track_id: Any
    ) -> Tuple[Any, Optional[str]]:
        track = await self._call(
            self.client.get_track(artist_id, track_id, with_lyrics=True)
        )
        return track, track.lyrics

    async def album_lyrics(self, artist_id: Any, album_id: Any) -> Album:
        """A release with the lyrics of every track filled in."""
        return await self.album(artist_id, album_id, with_lyrics=True)

    async def audio(
        self,
        artist_id: Any,
        track_id: Any,
        *,
        max_bytes: int = DEFAULT_PLAY_MIB * 1024 * 1024,
    ) -> Tuple[Track, bytes, str]:
        """The preview MP3 of one track, freshly signed.

        The link inside a cached album payload is signed with a timestamp, so a
        play always re-reads the track (one cheap request) instead of trusting
        an answer that can be half an hour old.
        """
        track = await self._track(artist_id, track_id)
        url = (track.streaming_url or {}).get("mp3-128")
        if not url:
            raise BandcampError(
                "Bandcamp has no preview for this track — the artist hid it "
                "from streaming (common on preorders)."
            )
        data, content_type = await self._call(
            self.client.fetch_audio(url, max_bytes=max_bytes)
        )
        return track, data, content_type

    async def _track(self, artist_id: Any, track_id: Any) -> Track:
        return await self._call(self.client.get_track(artist_id, track_id))

    # -- resolving --------------------------------------------------------
    async def resolve(self, text: str, kind: Optional[str] = None) -> Resolved:
        """Turn a link or a search phrase into one concrete entity.

        A link is resolved through the search endpoint (see the module
        docstring); a phrase is searched and the best hit for ``kind`` wins.
        """
        raw = (text or "").strip()
        if not raw:
            raise BandcampError("Give me a Bandcamp link or a search phrase.")
        parsed = parse_bandcamp_url(raw)
        wanted = parsed.kind if parsed is not None else kind
        query = parsed.search_query if parsed is not None else raw
        results = await self.search(query)
        ranked = rank_results(results, query, parsed, wanted)
        if kind and not ranked:
            ranked = rank_results(results, query, parsed, kind)
        if not ranked:
            if parsed is not None:
                raise BandcampError(
                    f"Could not resolve that link on Bandcamp: {truncate(parsed.url, 200)}"
                )
            raise BandcampError(
                f"Nothing on Bandcamp matched **{truncate(raw, 120)}**."
            )
        best = ranked[0]
        if parsed is not None:
            matched = _url_key(getattr(best, "url", "")) == parsed.key
            if not matched and score_result(best, query, parsed) < 40:
                # A link must resolve to itself or not at all; a fuzzy hit for
                # a mistyped URL reads as the wrong release.
                raise BandcampError(
                    f"Could not resolve that link on Bandcamp: {truncate(parsed.url, 200)}"
                )
        return await self._open_search_result(
            best, parsed.search_query if parsed else raw
        )

    async def _open_search_result(self, item: Any, query: str) -> Resolved:
        kind = getattr(item, "type", "")
        if kind == KIND_ARTIST:
            return Resolved(KIND_ARTIST, await self.artist(item.id), query, True)
        if kind == KIND_ALBUM:
            album = await self.album(getattr(item, "artist_id", 0), item.id)
            return Resolved(KIND_ALBUM, album, query, True)
        if kind == KIND_TRACK:
            track = await self.track(getattr(item, "artist_id", 0), item.id)
            return Resolved(KIND_TRACK, track, query, True)
        raise BandcampError("That search hit is not something I can open.")

    async def open_result(self, row: Dict[str, Any]) -> Resolved:
        """Open a row produced by :func:`result_row`."""
        kind = row.get("kind") or KIND_ALBUM
        item_id = int(row.get("id") or 0)
        artist_id = int(row.get("artist_id") or item_id)
        title = row.get("title") or ""
        if kind == KIND_ARTIST:
            return Resolved(KIND_ARTIST, await self.artist(item_id), title, True)
        if kind == KIND_ALBUM:
            return Resolved(
                KIND_ALBUM, await self.album(artist_id, item_id), title, True
            )
        if kind == KIND_TRACK:
            return Resolved(
                KIND_TRACK, await self.track(artist_id, item_id), title, True
            )
        raise BandcampError("That search hit is not something I can open.")
