"""Discogs API transport and formatting helpers.

This module deliberately imports nothing from ``discord`` or ``redbot`` so the
request layer, the rate limiter and the text helpers can be exercised against
the live API on their own (``python -c "import api; ..."``).  The cog next to it
handles embeds, views and configuration.

Discogs facts this module is built around (verified against the live API):

* A ``User-Agent`` header is mandatory.  Without one the API answers ``200``
  with an empty body, which reads like "no results" everywhere downstream.
* Unauthenticated requests get **25 requests per minute**, an optional user
  token raises that to **60**.  The limits arrive in the ``X-Discogs-Ratelimit``
  headers of every response and the API answers ``429`` with
  ``{"message": "You are making requests too quickly."}`` once they are used up.
* Paging is capped: ``per_page`` goes up to 100 and deep pages stop being
  served after roughly 10k results, so the cog keeps its own page arithmetic.
* Search *works* without a token today (contrary to the docs), so the cog is
  usable out of the box and a token only buys a bigger rate-limit budget.
"""

from __future__ import annotations

import asyncio
import logging
import re
import shlex
import time
from collections import deque
from typing import Any, Deque, Dict, List, Optional, Sequence, Tuple

import aiohttp

log = logging.getLogger("red.freak_cogs.discogs.api")

API_ROOT = "https://api.discogs.com"
SITE_ROOT = "https://www.discogs.com"
#: Discogs asks for a unique, identifying user agent (RFC 1945 style).
USER_AGENT = "freak-cogs-discogs/1.0 (+https://github.com/cens0r3d/freak-cogs)"
HTTP_TIMEOUT = 20
#: Rate-limit window the API documents, in seconds.
WINDOW_SECONDS = 60
#: Requests cached per client.  Entities are stable for a long time, searches
#: are not, so the two TTLs differ.
CACHE_SECONDS = 1800
SEARCH_CACHE_SECONDS = 300
MAX_CACHE_ENTRIES = 400
#: Result kinds the ``/database/search`` endpoint understands.
SEARCH_TYPES = ("release", "master", "artist", "label")
#: Sort keys accepted by ``/database/search``.
SORT_KEYS = (
    "year",
    "artist",
    "title",
    "label",
    "catno",
    "format",
    "rating",
    "added",
    "want",
)
#: Discogs' own genre taxonomy.  Everything else people call a genre (Techno,
#: House, Ambient …) is a *style* on Discogs, and filtering by it as a genre
#: returns zero results — hence :func:`split_genre_filter`.
DISCOGS_GENRES = (
    "Blues",
    "Brass & Military",
    "Children's",
    "Classical",
    "Electronic",
    "Folk, World, & Country",
    "Funk / Soul",
    "Hip Hop",
    "Jazz",
    "Latin",
    "Non-Music",
    "Pop",
    "Reggae",
    "Rock",
    "Stage & Screen",
)
#: Currencies offered by the marketplace stats endpoint.
CURRENCIES = (
    "EUR",
    "USD",
    "GBP",
    "CHF",
    "CAD",
    "AUD",
    "JPY",
    "SEK",
    "NOK",
    "DKK",
    "PLN",
    "BRL",
)


class DiscogsError(Exception):
    """Base class for everything the Discogs API can answer with."""


class DiscogsNotFound(DiscogsError):
    """The release, artist, label or master does not exist."""


class DiscogsAuthError(DiscogsError):
    """The token was rejected, or the endpoint needs a different one."""


class DiscogsRateLimited(DiscogsError):
    """The API is still refusing after a backoff."""


class DiscogsUnavailable(DiscogsError):
    """Network trouble, a timeout, or a server-side error."""


# --------------------------------------------------------------------------
# text helpers (pure, unit-testable)
# --------------------------------------------------------------------------

_MARKUP_TAG = re.compile(
    r"\[/?(?:b|i|u|s|quote|url(?:=[^\]]*)?|list|\*|hr)\]", re.IGNORECASE
)
_MARKUP_ENTITY = re.compile(r"\[(?:a|l|r|m|s|e|w)=([^\]]+)\]", re.IGNORECASE)
_MARKUP_LEFT = re.compile(r"\[/?(?:b|i|u|s|quote|url|list|\*)[^\]]*\]", re.IGNORECASE)


def strip_markup(text: Optional[str]) -> str:
    """Turn Discogs' BBCode-ish profile/notes markup into readable plain text."""
    if not text:
        return ""
    cleaned = _MARKUP_ENTITY.sub(r"\1", text)
    cleaned = _MARKUP_TAG.sub("", cleaned)
    cleaned = _MARKUP_LEFT.sub("", cleaned)
    cleaned = cleaned.replace("\r\n", "\n").replace("\r", "\n")
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    return cleaned.strip()


def truncate(text: str, limit: int, *, suffix: str = "…") -> str:
    """Hard-cut ``text`` at ``limit`` characters, adding an ellipsis."""
    text = text or ""
    if limit <= 0:
        return ""
    if len(text) <= limit:
        return text
    cut = text[: limit - len(suffix)]
    # Prefer a word boundary so a URL or a sentence does not end mid-word.
    space = cut.rfind(" ")
    if space > limit * 0.6:
        cut = cut[:space]
    return cut.rstrip(" ,;:-\n") + suffix


def join_artists(
    artists: Sequence[Dict[str, Any]], *, fallback: str = "Unknown artist"
) -> str:
    """Render a Discogs artist list, honouring the per-entry ``join`` string."""
    parts: List[str] = []
    for artist in artists or []:
        name = (artist.get("name") or "").strip()
        if not name:
            continue
        anv = (artist.get("anv") or "").strip()
        if anv and anv != name:
            name = f"{anv} ({name})" if _looks_like_real_name(name, anv) else anv
        parts.append(name + (artist.get("join") or ""))
    text = "".join(parts).strip()
    return text or fallback


def _looks_like_real_name(primary: str, credit: str) -> bool:
    """Discogs keeps the credited name in ``anv`` and the canonical one in ``name``."""
    return bool(primary) and primary.lower() not in credit.lower()


def format_count(number: Any) -> str:
    """``4498`` -> ``4,498``."""
    try:
        return f"{int(number):,}"
    except (TypeError, ValueError):
        return str(number)


def format_duration(seconds: Any) -> str:
    """``214`` (int seconds, videos) or ``"3:32"`` (tracklists) -> ``"3:32"``."""
    if seconds is None or seconds == "":
        return ""
    if isinstance(seconds, str):
        return seconds.strip()
    try:
        total = int(round(float(seconds)))
    except (TypeError, ValueError):
        return str(seconds)
    return f"{total // 60}:{total % 60:02d}"


def format_price(value: Any, currency: str = "") -> str:
    """``(0.67, "EUR")`` -> ``"0.67 EUR"``; unknown values become an em dash."""
    if value in (None, "", 0):
        return "—"
    try:
        amount = float(value)
    except (TypeError, ValueError):
        return str(value)
    if amount >= 1000:
        text = f"{amount:,.0f}"
    elif amount >= 10:
        text = f"{amount:.2f}"
    else:
        text = f"{amount:.2f}"
    return f"{text} {currency}".strip()


def format_formats(
    formats: Sequence[Dict[str, Any]], *, quantity: Optional[int] = None
) -> str:
    """Render the ``formats`` array: ``2x Vinyl, LP, Album, Stereo``."""
    chunks: List[str] = []
    for entry in formats or []:
        name = (entry.get("name") or "").strip()
        descriptions = [d for d in (entry.get("descriptions") or []) if d]
        qty = entry.get("qty")
        label = name
        try:
            if qty and int(qty) > 1:
                label = f"{int(qty)}x {name}"
        except (TypeError, ValueError):
            pass
        if descriptions:
            label = (
                ", ".join([label] + descriptions) if label else ", ".join(descriptions)
            )
        if label:
            chunks.append(label)
    text = " · ".join(chunks) if chunks else ""
    if not text:
        return "—"
    if quantity and quantity > 1 and len(chunks) >= 1:
        text = f"{text} ({quantity} items)"
    return text


def format_release_line(release: Dict[str, Any]) -> str:
    """One line of a release list: ``Rick Astley – Never Gonna Give You Up``."""
    artists = join_artists(release.get("artists") or [], fallback="")
    title = (release.get("title") or "").strip()
    if artists and title:
        return f"{artists} – {title}"
    return title or artists or "Untitled"


def track_line(track: Dict[str, Any]) -> str:
    """Render one tracklist entry; headings (``type_ == "heading"``) get bold."""
    title = (track.get("title") or "").strip() or "Untitled"
    if (track.get("type_") or "").lower() == "heading":
        return f"**{title}**"
    position = (track.get("position") or "").strip()
    duration = format_duration(track.get("duration"))
    left = f"{position}  {title}" if position else title
    if duration:
        return f"{left}  ·  {duration}"
    return left


def format_tracklist(
    tracks: Sequence[Dict[str, Any]],
    *,
    limit: int = 1024,
    max_lines: Optional[int] = None,
) -> List[str]:
    """Split a tracklist into embed-field-sized chunks, never mid-track."""
    chunks: List[str] = []
    current: List[str] = []
    length = 0
    count = 0
    for track in tracks or []:
        line = track_line(track)
        if not line:
            continue
        extra = len(line) + (1 if current else 0)
        if current and (length + extra > limit or (max_lines and count >= max_lines)):
            chunks.append("\n".join(current))
            current, length, count = [], 0, 0
            extra = len(line)
        if len(line) > limit:  # a pathological title, never lose it entirely
            line = truncate(line, limit, suffix="…")
            extra = len(line)
        current.append(line)
        length += extra
        count += 1
    if current:
        chunks.append("\n".join(current))
    return chunks or ["—"]


def format_artist_credits(
    entries: Sequence[Dict[str, Any]], *, limit: int = 400
) -> str:
    """Render the ``extraartists`` array (instruments, production, …)."""
    lines: List[str] = []
    for entry in entries or []:
        name = (entry.get("name") or "").strip()
        role = (entry.get("role") or "").strip()
        tracks = (entry.get("tracks") or "").strip()
        if not name:
            continue
        line = f"{name} — {role}" if role else name
        if tracks:
            line += f" ({tracks})"
        lines.append(line)
    return truncate("\n".join(lines), limit) if lines else ""


def site_url(payload: Dict[str, Any], fallback: str = "") -> str:
    """Prefer the public www.discogs.com URL from an API payload."""
    uri = (payload.get("uri") or "").strip()
    if uri.startswith("http"):
        return uri
    if uri.startswith("/"):
        return SITE_ROOT + uri
    return fallback or SITE_ROOT


def parse_year_filter(value: Optional[str]) -> Optional[int]:
    """Accept ``1995``, ``95`` or ``1995-1999`` (first year wins) as a year filter."""
    if value in (None, ""):
        return None
    text = str(value).strip()
    match = re.match(r"^(\d{4})", text)
    if match:
        return int(match.group(1))
    match = re.match(r"^(\d{2})$", text)
    if match:
        short = int(match.group(1))
        return 1900 + short if short > 30 else 2000 + short
    return None


def parse_decade(value: Optional[str]) -> Optional[Tuple[int, int]]:
    """``"90s"``, ``"1990s"`` -> ``(1990, 1999)``; a plain year stays that year."""
    if value in (None, ""):
        return None
    text = str(value).strip().lower().rstrip("s")
    match = re.match(r"^(\d{4})", text)
    if match:
        start = int(match.group(1))
        if text.endswith("0"):
            start -= start % 10
            return start, start + 9
        return start, start
    match = re.match(r"^(\d{2})$", text)
    if match:
        short = int(match.group(1))
        base = 1900 + short if short > 30 else 2000 + short
        return base, base + 9
    return None


def parse_flag_arguments(
    text: str, aliases: Dict[str, str]
) -> Tuple[Dict[str, str], str, List[str]]:
    """Split ``--flag value`` pairs out of a free-text command argument.

    ``aliases`` maps what the user may type (``"format"``) to the internal key
    (``"fmt"``).  Returns ``(values, remaining_text, unknown_flags)``; a flag
    without a value is stored as ``"true"`` so a bare ``--sort`` is an error the
    caller can describe instead of silently becoming a query word.
    """
    try:
        tokens = shlex.split(text or "")
    except ValueError:
        tokens = (text or "").split()
    values: Dict[str, str] = {}
    rest: List[str] = []
    unknown: List[str] = []
    index = 0
    while index < len(tokens):
        token = tokens[index]
        if token.startswith("--") and len(token) > 2:
            name = token[2:]
            value: Optional[str] = None
            if "=" in name:
                name, _, value = name.partition("=")
            key = aliases.get(name.strip().lower())
            if key is None:
                unknown.append(name)
                index += 1
                continue
            if (
                value is None
                and index + 1 < len(tokens)
                and not tokens[index + 1].startswith("--")
            ):
                value = tokens[index + 1]
                index += 1
            values[key] = (value or "true").strip()
        else:
            rest.append(token)
        index += 1
    return values, " ".join(rest).strip(), unknown


def split_genre_filter(
    value: Optional[str],
) -> Tuple[Optional[str], Optional[str], str]:
    """Map what a user types as a genre onto Discogs' taxonomy.

    Returns ``(field, value, note)`` where ``field`` is ``"genre"`` for a real
    Discogs genre and ``"style"`` for everything else (Techno, House, Ambient …).
    """
    if not value:
        return None, None, ""
    needle = re.sub(r"[^a-z]", "", str(value).lower())
    for genre in DISCOGS_GENRES:
        if re.sub(r"[^a-z]", "", genre.lower()) == needle:
            return "genre", genre, ""
    return (
        "style",
        str(value),
        f"“{value}” is a style on Discogs — searched as style",
    )


def describe_search_filters(filters: Dict[str, Any]) -> str:
    """Compact ``genre: Techno · year: 1999`` line for footers and titles."""
    labels = {
        "genre": "genre",
        "style": "style",
        "country": "country",
        "year": "year",
        "format": "format",
        "label": "label",
        "artist": "artist",
        "catno": "catno",
        "barcode": "barcode",
        "track": "track",
        "sort": "sort",
    }
    parts = [
        f"{labels[key]}: {value}"
        for key, value in filters.items()
        if value and key in labels
    ]
    return " · ".join(parts)


def describe_entity(kind: str, payload: Dict[str, Any]) -> str:
    """Human label for a search row of the given kind."""
    return {
        "release": "Release",
        "master": "Master release",
        "artist": "Artist",
        "label": "Label",
    }.get(kind, kind.title() if kind else "Entry")


def row_from_search(result: Dict[str, Any]) -> Dict[str, Any]:
    """Normalise a search result into the row shape the list view renders."""
    kind = (result.get("type") or "release").lower()
    title = (result.get("title") or "Untitled").strip()
    community = result.get("community") or {}
    bits: List[str] = []
    year = result.get("year")
    if year:
        bits.append(str(year))
    country = result.get("country")
    if country:
        bits.append(str(country))
    formats = result.get("format") or []
    if formats:
        bits.append(", ".join(str(f) for f in formats[:3]))
    labels = [str(name) for name in (result.get("label") or []) if name]
    if labels:
        bits.append(labels[0])
    stats = []
    if community.get("want"):
        stats.append(f"{format_count(community['want'])} want")
    if community.get("have"):
        stats.append(f"{format_count(community['have'])} have")
    if stats:
        bits.append(" / ".join(stats))
    return {
        "id": result.get("id") or result.get("master_id"),
        "kind": kind,
        "title": title,
        "subtitle": " · ".join(bits),
        "url": site_url(result),
        "thumb": result.get("cover_image") or result.get("thumb") or "",
    }


def row_from_release(
    release: Dict[str, Any], *, kind: Optional[str] = None
) -> Dict[str, Any]:
    """Normalise an artist/label release list entry into the row shape."""
    release_kind = (kind or release.get("type") or "release").lower()
    if release_kind == "master" and release.get("main_release"):
        entity_id = release.get("id")
    else:
        entity_id = release.get("id")
    bits: List[str] = []
    year = release.get("year")
    if year:
        bits.append(str(year))
    if release.get("label"):
        bits.append(str(release["label"]))
    if release.get("catno"):
        bits.append(str(release["catno"]))
    if release.get("format"):
        bits.append(str(release["format"]))
    if release.get("country"):
        bits.append(str(release["country"]))
    return {
        "id": entity_id,
        "kind": release_kind,
        "title": format_release_line(release),
        "subtitle": " · ".join(bits),
        "url": SITE_ROOT + (release.get("uri") or "") if release.get("uri") else "",
        "thumb": release.get("thumb") or "",
    }


def row_from_version(version: Dict[str, Any]) -> Dict[str, Any]:
    """Normalise a ``/masters/{id}/versions`` entry."""
    bits: List[str] = []
    for key in ("released", "country", "format", "label", "catno"):
        value = version.get(key)
        if value:
            bits.append(str(value))
    stats = version.get("stats") or {}
    community = stats.get("community") or {}
    if community.get("in_wantlist"):
        bits.append(f"{format_count(community['in_wantlist'])} want")
    return {
        "id": version.get("id"),
        "kind": "release",
        "title": (version.get("title") or "Untitled").strip(),
        "subtitle": " · ".join(bits),
        "url": SITE_ROOT + (version.get("uri") or "") if version.get("uri") else "",
        "thumb": version.get("thumb") or "",
    }


# --------------------------------------------------------------------------
# client
# --------------------------------------------------------------------------


class DiscogsClient:
    """Thin async wrapper around the Discogs v2 REST API.

    The client owns its rate-limit accounting: it keeps a sliding window of the
    request timestamps it has sent, learns the real limit from the response
    headers, and waits instead of collecting ``429``s.
    """

    def __init__(self, session: aiohttp.ClientSession) -> None:
        self._session = session
        self._token: Optional[str] = None
        self._lock = asyncio.Lock()
        self._window: Deque[float] = deque()
        self._limit = 25
        self._remaining: Optional[int] = None
        self._blocked_until = 0.0
        self._cache: Dict[str, Tuple[float, Any]] = {}
        self.requests = 0
        self.failures = 0

    # -- token ------------------------------------------------------------
    def set_token(self, token: Optional[str]) -> None:
        """Install (or clear) a user token.  The next request picks it up."""
        token = (token or "").strip() or None
        if token != self._token:
            log.info("Discogs token %s", "set" if token else "cleared")
            # The limit differs per auth state, so cached responses stay valid
            # but the learned limit does not.
            self._limit = 25
            self._remaining = None
        self._token = token

    @property
    def has_token(self) -> bool:
        return bool(self._token)

    # -- lifecycle --------------------------------------------------------
    async def close(self) -> None:
        if not self._session.closed:
            await self._session.close()

    def stats(self) -> Dict[str, Any]:
        now = time.monotonic()
        return {
            "limit": self._limit,
            "remaining": self._remaining,
            "requests": self.requests,
            "failures": self.failures,
            "cached": len(self._cache),
            "token": self.has_token,
            "blocked_for": max(0, int(self._blocked_until - now)),
            "window": len([t for t in self._window if now - t < WINDOW_SECONDS]),
        }

    # -- rate limiting ----------------------------------------------------
    def _note_headers(self, headers: Any) -> None:
        def as_int(name: str) -> Optional[int]:
            raw = headers.get(name)
            try:
                return int(raw) if raw is not None else None
            except (TypeError, ValueError):
                return None

        limit = as_int("X-Discogs-Ratelimit")
        if limit and 5 <= limit <= 600:
            self._limit = limit
        remaining = as_int("X-Discogs-Ratelimit-Remaining")
        if remaining is not None:
            self._remaining = max(0, remaining)

    async def _acquire_slot(self) -> None:
        """Wait until a request fits into the sliding one-minute window."""
        while True:
            await asyncio.sleep(0)  # let other waiters queue up first
            async with self._lock:
                now = time.monotonic()
                while self._window and now - self._window[0] >= WINDOW_SECONDS:
                    self._window.popleft()
                wait = 0.0
                if self._blocked_until > now:
                    wait = self._blocked_until - now
                elif len(self._window) >= max(5, self._limit - 1):
                    wait = WINDOW_SECONDS - (now - self._window[0]) + 0.2
                if wait <= 0:
                    self._window.append(now)
                    return
            log.debug("Discogs rate limit: waiting %.1fs", wait)
            await asyncio.sleep(wait + 0.05)

    # -- cache ------------------------------------------------------------
    def clear_cache(self) -> int:
        dropped = len(self._cache)
        self._cache.clear()
        return dropped

    def _cache_key(self, path: str, params: Optional[Dict[str, Any]]) -> str:
        if not params:
            return path
        items = sorted(
            (str(k), str(v)) for k, v in params.items() if v not in (None, "")
        )
        return path + "?" + "&".join(f"{k}={v}" for k, v in items)

    def _cache_get(self, key: str) -> Any:
        hit = self._cache.get(key)
        if not hit:
            return None
        expires, payload = hit
        if expires <= time.monotonic():
            self._cache.pop(key, None)
            return None
        return payload

    def _cache_put(self, key: str, payload: Any, ttl: int) -> None:
        if ttl <= 0 or not isinstance(payload, (dict, list)):
            return
        if len(self._cache) >= MAX_CACHE_ENTRIES:
            now = time.monotonic()
            for stale in [
                k for k, (expires, _) in self._cache.items() if expires <= now
            ]:
                self._cache.pop(stale, None)
            while len(self._cache) >= MAX_CACHE_ENTRIES:
                self._cache.pop(next(iter(self._cache)), None)
        self._cache[key] = (time.monotonic() + ttl, payload)

    # -- transport --------------------------------------------------------
    def _headers(self) -> Dict[str, str]:
        headers = {
            "User-Agent": USER_AGENT,
            "Accept": "application/json",
        }
        if self._token:
            headers["Authorization"] = f"Discogs token={self._token}"
        return headers

    async def request(
        self,
        path: str,
        params: Optional[Dict[str, Any]] = None,
        *,
        ttl: int = CACHE_SECONDS,
    ) -> Dict[str, Any]:
        """GET an API path and return the decoded payload, cached for ``ttl``."""
        key = self._cache_key(path, params)
        cached = self._cache_get(key)
        if cached is not None:
            return cached

        await self._acquire_slot()
        url = path if path.startswith("http") else API_ROOT + path
        clean = {k: v for k, v in (params or {}).items() if v not in (None, "")}
        attempts = 0
        while True:
            attempts += 1
            self.requests += 1
            try:
                timeout = aiohttp.ClientTimeout(total=HTTP_TIMEOUT)
                async with self._session.get(
                    url, params=clean, headers=self._headers(), timeout=timeout
                ) as response:
                    self._note_headers(response.headers)
                    if response.status == 429:
                        delay = _retry_after(response.headers)
                        self.failures += 1
                        if attempts > 2:
                            raise DiscogsRateLimited(
                                "Discogs is rate limiting this bot "
                                f"({self._limit} requests/minute). Try again in about "
                                f"{int(delay)} seconds."
                            )
                        log.warning("Discogs 429 — backing off %ss", int(delay))
                        async with self._lock:
                            self._window.clear()
                            self._blocked_until = time.monotonic() + delay
                        await asyncio.sleep(delay)
                        continue
                    if response.status >= 500 and attempts == 1:
                        self.failures += 1
                        log.warning(
                            "Discogs %s on %s — retrying once", response.status, path
                        )
                        await asyncio.sleep(2)
                        continue
                    payload = await _read_json(response)
                    if response.status in (200, 201):
                        result = (
                            payload
                            if isinstance(payload, dict)
                            else {"results": payload}
                        )
                        self._cache_put(key, result, ttl)
                        return result
                    message = ""
                    if isinstance(payload, dict):
                        message = str(payload.get("message") or "").strip()
                    raise _error_for(response.status, message, path)
            except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
                self.failures += 1
                if attempts == 1:
                    log.warning("Discogs request failed (%s) — retrying once", exc)
                    await asyncio.sleep(1.5)
                    continue
                raise DiscogsUnavailable(
                    "Discogs did not answer in time. Try again in a moment."
                ) from exc

    # -- endpoints --------------------------------------------------------
    async def search(
        self,
        query: Optional[str] = None,
        *,
        kind: str = "release",
        page: int = 1,
        per_page: int = 25,
        **filters: Any,
    ) -> Dict[str, Any]:
        """``/database/search`` — the one endpoint that makes the cog useful."""
        kind = (kind or "release").lower()
        if kind not in SEARCH_TYPES:
            raise DiscogsError(f"Unknown search type `{kind}`.")
        params: Dict[str, Any] = {
            "type": kind,
            "page": max(1, int(page)),
            "per_page": max(1, min(100, int(per_page))),
        }
        if query:
            params["q"] = query
        params.update({k: v for k, v in filters.items() if v not in (None, "", [])})
        return await self.request("/database/search", params, ttl=SEARCH_CACHE_SECONDS)

    async def release(self, release_id: int) -> Dict[str, Any]:
        return await self.request(f"/releases/{int(release_id)}")

    async def master(self, master_id: int) -> Dict[str, Any]:
        return await self.request(f"/masters/{int(master_id)}")

    async def artist(self, artist_id: int) -> Dict[str, Any]:
        return await self.request(f"/artists/{int(artist_id)}")

    async def label(self, label_id: int) -> Dict[str, Any]:
        return await self.request(f"/labels/{int(label_id)}")

    async def artist_releases(
        self,
        artist_id: int,
        *,
        page: int = 1,
        per_page: int = 25,
        sort: str = "year",
        sort_order: str = "desc",
    ) -> Dict[str, Any]:
        return await self.request(
            f"/artists/{int(artist_id)}/releases",
            {
                "page": max(1, int(page)),
                "per_page": max(1, min(100, int(per_page))),
                "sort": sort,
                "sort_order": sort_order,
            },
            ttl=SEARCH_CACHE_SECONDS,
        )

    async def label_releases(
        self, label_id: int, *, page: int = 1, per_page: int = 25
    ) -> Dict[str, Any]:
        return await self.request(
            f"/labels/{int(label_id)}/releases",
            {"page": max(1, int(page)), "per_page": max(1, min(100, int(per_page)))},
            ttl=SEARCH_CACHE_SECONDS,
        )

    async def master_versions(
        self, master_id: int, *, page: int = 1, per_page: int = 25
    ) -> Dict[str, Any]:
        return await self.request(
            f"/masters/{int(master_id)}/versions",
            {"page": max(1, int(page)), "per_page": max(1, min(100, int(per_page)))},
            ttl=SEARCH_CACHE_SECONDS,
        )

    async def marketplace_stats(
        self, release_id: int, currency: str = "EUR"
    ) -> Dict[str, Any]:
        return await self.request(
            f"/marketplace/stats/{int(release_id)}",
            {"curr_abbr": (currency or "EUR").upper()},
            ttl=SEARCH_CACHE_SECONDS,
        )


def _retry_after(headers: Any) -> float:
    raw = headers.get("Retry-After") if headers else None
    if raw is None:
        return 30.0
    try:
        value = float(str(raw))
    except (TypeError, ValueError):
        return 30.0
    return max(5.0, min(120.0, value))


async def _read_json(response: aiohttp.ClientResponse) -> Any:
    try:
        return await response.json(content_type=None)
    except (ValueError, aiohttp.ContentTypeError):
        text = await response.text()
        return {"message": text.strip()} if text.strip() else {}


def _error_for(status: int, message: str, path: str) -> DiscogsError:
    detail = message or f"HTTP {status}"
    if status == 404:
        return DiscogsNotFound(detail)
    if status in (401, 403):
        return DiscogsAuthError(
            f"{detail} — this needs a valid Discogs token (`[p]discogs token`)."
        )
    if status == 429:
        return DiscogsRateLimited(detail)
    if status >= 500:
        return DiscogsUnavailable(f"Discogs answered {status}: {detail}")
    return DiscogsError(f"{detail} (HTTP {status})")
