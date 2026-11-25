"""Kick.com livestream alerts for Red-DiscordBot.

Watches Kick channels through the official public API (``api.kick.com/public/v1``),
announces streams when they go live, keeps the alert embed up to date while the
stream runs, and turns the alert into a "stream ended" embed (or deletes it) when
the stream stops.

Authentication uses an app access token (OAuth2 ``client_credentials`` against
``id.kick.com``). Credentials live in Red's shared API token store under the
service name ``kick``, so ``[p]set api kick client_id,<id> client_secret,<secret>``
works as well as ``[p]kickalert setcreds``.
"""

from __future__ import annotations

import asyncio
import contextlib
import io
import logging
import random
import re
import shutil
import tempfile
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple
from urllib.parse import urlsplit

import aiohttp
import discord
from redbot.core import Config, checks, commands
from redbot.core.bot import Red
from redbot.core.utils.menus import DEFAULT_CONTROLS, menu
from redbot.core.utils.views import SetApiView

log = logging.getLogger("red.freak_cogs.kickalerts")

KICK_AUTH_URL = "https://id.kick.com/oauth/token"
KICK_CHANNELS_URL = "https://api.kick.com/public/v1/channels"
KICK_SITE = "https://kick.com"
#: Web endpoint for a channel's clips. Keyless, unlike the public API, which
#: exposes no clip list at all.
KICK_CLIPS_URL = "https://kick.com/api/v2/channels/{slug}/clips"
#: kick.com answers a plain HTTP client; this is just enough of a browser.
BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)
#: Clip poll interval bounds and how many clip ids are remembered per streamer.
MIN_CLIPS_INTERVAL = 60
MAX_CLIPS_INTERVAL = 3600
#: Discord's upload limit for bots is 20 MiB; asking for more only produces a
#: failed send, so the clip setting is capped there.
MAX_CLIP_MIB = 20
SEEN_CLIP_LIMIT = 30
KICK_COLOR = 0x53FC18
OFFLINE_COLOR = 0x808080
DEBUG_COLOR = 0xFFAA00
SHARED_API_SERVICE = "kick"

#: The public API accepts at most 50 slugs per request.
MAX_SLUGS_PER_REQUEST = 50
#: Poll interval bounds in seconds.
MIN_INTERVAL = 30
MAX_INTERVAL = 3600
#: How many times a request is retried on 429/5xx before giving up.
MAX_ATTEMPTS = 3
#: Fallback wait when the API sends a 429 without a Retry-After header.
DEFAULT_RETRY_AFTER = 5.0
#: Kick sends this sentinel for "no stream" timestamps.
EPOCH_SENTINEL = "0001-01-01T00:00:00"
#: Placeholders a custom message may use.
TEMPLATE_FIELDS = (
    "streamer",
    "title",
    "game",
    "category",
    "url",
    "viewers",
    "language",
    "started",
    "uptime",
)

#: Default name of the Discord scheduled event. It takes the same placeholders
#: as a custom alert message, so a guild can localise it with one command.
DEFAULT_EVENT_NAME = "{streamer} is live on Kick"
#: Discord refuses an external event that starts in the past, so go-live is
#: rounded up to the next minute; the event turns ACTIVE at that moment.
EVENT_START_DELAY = 60
#: Nominal length in hours. The API completes an external event at its end time,
#: so a running stream extends it (see ``EVENT_EXTEND_BEFORE``).
EVENT_DEFAULT_HOURS = 8
MIN_EVENT_HOURS = 1
MAX_EVENT_HOURS = 12
#: Extend the end time when less than this many seconds are left.
EVENT_EXTEND_BEFORE = 3600
#: Field limits of a guild scheduled event.
EVENT_NAME_LIMIT = 100
EVENT_DESCRIPTION_LIMIT = 1000
EVENT_LOCATION_LIMIT = 100
#: Discord accepts an event cover of up to 10 MB; stay below it.
EVENT_IMAGE_LIMIT = 8 * 1024 * 1024
#: What happens to the event when the stream ends.
EVENT_OFFLINE_MODES = ("delete", "complete")

DEFAULT_GLOBAL: Dict[str, Any] = {
    "legacy_client_id": None,
    "legacy_client_secret": None,
}

DEFAULT_GUILD: Dict[str, Any] = {
    "streamers": {},
    "global_channel_id": None,
    "global_ping_role_id": None,
    "check_interval": 60,
    "embed_style": "detailed",
    "show_viewer_count": True,
    "show_category": True,
    "auto_delete": False,
    "offline_post": "new",
    "timezone_offset": 1,
    "enabled": True,
    "edit_live_alerts": True,
    "live_update_seconds": 300,
    "clips_enabled": False,
    "clips_interval": 300,
    "clips_channel_id": None,
    "clips_attach": True,
    "clips_max_mb": 20,
    "events_enabled": False,
    "event_hours": EVENT_DEFAULT_HOURS,
    "event_cover": True,
    "event_offline": "delete",
    "event_name": DEFAULT_EVENT_NAME,
    "event_description": None,
}

#: Keys every streamer entry carries; missing ones fall back to these.
STREAMER_DEFAULTS: Dict[str, Any] = {
    "channel_id": None,
    "ping_role_id": None,
    "custom_message": None,
    "delete_after_offline": None,
    "muted": False,
    "is_live": False,
    "stream_key": None,
    "started_at": None,
    "alert_message_id": None,
    "last_edit": None,
    "last_title": None,
    "last_category": None,
    "viewer_samples": 0,
    "viewer_total": 0,
    "peak_viewers": 0,
    "last_viewers": 0,
    "clips": True,
    "seen_clips": [],
    "clips_primed": False,
    "clip_message_id": None,
    "events": True,
    "event_id": None,
    "event_stream_key": None,
    "event_end": None,
}

SLUG_RE = re.compile(r"^[a-z0-9_-]{1,25}$")
TEMPLATE_RE = re.compile(r"\{(\w+)\}")


# --------------------------------------------------------------------------- #
# errors
# --------------------------------------------------------------------------- #
class KickError(Exception):
    """Base class for Kick API problems."""


class KickAuthError(KickError):
    """The API rejected the credentials or refused to hand out a token."""


class KickRateLimited(KickError):
    """The API kept answering 429 after every retry."""


# --------------------------------------------------------------------------- #
# data
# --------------------------------------------------------------------------- #
@dataclass(slots=True)
class StreamInfo:
    """Everything the alert embeds need about one Kick channel."""

    slug: str
    broadcaster_user_id: Optional[int] = None
    is_live: bool = False
    title: Optional[str] = None
    category: Optional[str] = None
    category_thumbnail: Optional[str] = None
    viewers: int = 0
    started_at: Optional[datetime] = None
    language: Optional[str] = None
    is_mature: bool = False
    thumbnail: Optional[str] = None
    tags: Tuple[str, ...] = ()
    banner: Optional[str] = None
    description: Optional[str] = None
    active_subscribers: Optional[int] = None
    raw: Dict[str, Any] = field(default_factory=dict)

    @property
    def url(self) -> str:
        """The channel page on kick.com."""
        return f"{KICK_SITE}/{self.slug}"

    @property
    def stream_key(self) -> Optional[str]:
        """Identity of the current broadcast, used to spot restarts.

        The channel endpoint carries no stream id, but every broadcast gets a
        fresh ``stream.start_time``, so that timestamp identifies the stream.
        """
        if not self.is_live or self.started_at is None:
            return None
        return self.started_at.isoformat()

    @property
    def uptime(self) -> Optional[timedelta]:
        """How long the current stream has been running."""
        if not self.is_live or self.started_at is None:
            return None
        return datetime.now(timezone.utc) - self.started_at

    @classmethod
    def from_payload(cls, payload: Dict[str, Any]) -> "StreamInfo":
        """Build a :class:`StreamInfo` from a ``/public/v1/channels`` entry."""
        stream = _as_dict(payload.get("stream"))
        category = _as_dict(payload.get("category"))
        info = cls(slug=str(payload.get("slug") or "").lower(), raw=payload)
        info.broadcaster_user_id = _as_int(payload.get("broadcaster_user_id"))
        info.title = _as_text(payload.get("stream_title")) or _as_text(
            stream.get("title")
        )
        info.category = _as_text(category.get("name"))
        info.category_thumbnail = _as_text(category.get("thumbnail"))
        info.language = _as_text(stream.get("language"))
        info.is_mature = bool(stream.get("is_mature"))
        info.thumbnail = _as_text(stream.get("thumbnail"))
        info.banner = _as_text(payload.get("banner_picture"))
        info.description = _as_text(payload.get("channel_description"))
        info.active_subscribers = _as_int(payload.get("active_subscribers_count"))
        info.viewers = _as_int(stream.get("viewer_count")) or 0
        info.is_live = bool(stream.get("is_live"))
        info.started_at = parse_kick_time(stream.get("start_time"))
        raw_tags = stream.get("custom_tags")
        if isinstance(raw_tags, list):
            info.tags = tuple(str(tag) for tag in raw_tags if tag)
        if not info.is_live:
            # An offline channel keeps stale stream values in the payload; only
            # the channel-level data (title, category, banner) is trustworthy.
            info.viewers = 0
            info.started_at = None
            info.thumbnail = None
            info.tags = ()
        return info


@dataclass(slots=True)
class StreamSummary:
    """What one broadcast looked like, for the end-of-stream message."""

    slug: str
    url: str
    title: Optional[str] = None
    category: Optional[str] = None
    started_at: Optional[datetime] = None
    ended_at: Optional[datetime] = None
    duration: Optional[timedelta] = None
    peak_viewers: Optional[int] = None
    average_viewers: Optional[int] = None
    samples: int = 0


def summarise_stream(
    info: StreamInfo,
    state: Mapping[str, Any],
    *,
    ended_at: Optional[datetime] = None,
) -> StreamSummary:
    """Turn the stored state of a finished stream into its statistics.

    ``viewer_samples``/``viewer_total`` are accumulated by :func:`accumulate_viewers`
    on every poll, so the average is the mean of the polled viewer counts.
    """
    started = parse_kick_time(state.get("started_at"))
    ended = ended_at or datetime.now(timezone.utc)
    samples = _as_int(state.get("viewer_samples")) or 0
    total = _as_int(state.get("viewer_total")) or 0
    peak = _as_int(state.get("peak_viewers"))
    return StreamSummary(
        slug=info.slug,
        url=info.url,
        title=_as_text(state.get("last_title")) or info.title,
        category=_as_text(state.get("last_category")) or info.category,
        started_at=started,
        ended_at=ended,
        duration=(ended - started) if started else None,
        peak_viewers=peak if samples and peak is not None else None,
        average_viewers=round(total / samples) if samples else None,
        samples=samples,
    )


def accumulate_viewers(state: Mapping[str, Any], viewers: Any) -> Dict[str, int]:
    """Add one viewer sample to the running statistics of a stream."""
    samples = _as_int(state.get("viewer_samples")) or 0
    total = _as_int(state.get("viewer_total")) or 0
    peak = _as_int(state.get("peak_viewers")) or 0
    count = max(0, _as_int(viewers) or 0)
    return {
        "viewer_samples": samples + 1,
        "viewer_total": total + count,
        "peak_viewers": max(peak, count),
        "last_viewers": count,
    }


def _summary_pairs(summary: StreamSummary) -> List[Tuple[str, str]]:
    """The statistics shown in an end-of-stream embed, as name/value pairs."""
    pairs: List[Tuple[str, str]] = []
    formatted = format_uptime(summary.duration)
    if formatted:
        pairs.append(("Stream time", formatted))
    if summary.peak_viewers is not None:
        pairs.append(("Peak viewers", f"{summary.peak_viewers:,}"))
    if summary.average_viewers is not None:
        pairs.append(("Average viewers", f"{summary.average_viewers:,}"))
    started = _stamp(summary.started_at, "t")
    if started:
        pairs.append(("Started", started))
    ended = _stamp(summary.ended_at, "t")
    if ended:
        pairs.append(("Ended", ended))
    return pairs


def _summary_footer(label: str) -> str:
    """Footer text for an end-of-stream embed."""
    return f"Kick.com | {label}"


def _as_text(value: Any) -> Optional[str]:
    """Return a non-empty string, or ``None``."""
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _as_dict(value: Any) -> Dict[str, Any]:
    """Return the value when it is a dict, otherwise an empty dict."""
    return value if isinstance(value, dict) else {}


def _as_int(value: Any) -> Optional[int]:
    """Return an int if the value looks like one."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return int(value)
    if isinstance(value, str) and value.strip().lstrip("-").isdigit():
        return int(value.strip())
    return None


def parse_kick_time(value: Any) -> Optional[datetime]:
    """Parse a Kick timestamp, treating their epoch sentinel as "no value"."""
    text = _as_text(value)
    if text is None:
        return None
    text = text.replace("Z", "+00:00")
    if text.startswith(EPOCH_SENTINEL):
        return None
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def format_uptime(delta: Optional[timedelta]) -> Optional[str]:
    """Render a duration as ``2h 14m``."""
    if delta is None:
        return None
    total = int(delta.total_seconds())
    if total < 0:
        return None
    hours, remainder = divmod(total, 3600)
    minutes, _ = divmod(remainder, 60)
    if hours:
        return f"{hours}h {minutes}m"
    if minutes:
        return f"{minutes}m"
    return f"{total}s"


def render_template(template: str, info: StreamInfo) -> str:
    """Fill ``{placeholder}`` fields in a custom alert message.

    Unknown placeholders are left untouched, so a typo stays visible instead of
    silently eating part of the message.
    """
    values = {
        "streamer": info.slug,
        "title": info.title or "No title",
        "game": info.category or "Unknown",
        "category": info.category or "Unknown",
        "url": info.url,
        "viewers": f"{info.viewers:,}",
        "language": info.language or "unknown",
        "started": (info.started_at.isoformat() if info.started_at else "unknown"),
        "uptime": format_uptime(info.uptime) or "unknown",
    }

    def replace(match: "re.Match[str]") -> str:
        key = match.group(1)
        return str(values[key]) if key in values else match.group(0)

    return TEMPLATE_RE.sub(replace, template)


def build_event_description(info: StreamInfo, template: Optional[str]) -> str:
    """The description of the Discord event, clamped to Discord's limit.

    Without a custom template the title leads and a second line carries the
    category and the channel link, which is what an event card has room for.
    """
    if template:
        return render_template(template, info).strip()[:EVENT_DESCRIPTION_LIMIT]
    lines = [info.title or f"{info.slug} is live on Kick"]
    tail = " · ".join(part for part in (info.category, info.url) if part)
    if tail:
        lines.append(tail)
    return "\n".join(lines)[:EVENT_DESCRIPTION_LIMIT]


def build_event_name(info: StreamInfo, template: Optional[str]) -> str:
    """The event title, clamped to Discord's 100 character limit."""
    text = render_template(template or DEFAULT_EVENT_NAME, info).strip()
    text = " ".join(text.split()) or render_template(DEFAULT_EVENT_NAME, info)
    return text[:EVENT_NAME_LIMIT]


def clean_slug(value: str) -> str:
    """Normalise user input into a Kick slug; accepts a channel URL too."""
    return _clean_slug(value)


def _clean_slug(value: Any) -> str:
    """A bare Kick slug from a slug, an ``@handle`` or a kick.com URL.

    The host is parsed rather than searched for: a substring check happily
    accepts ``kick.com.example.org/@someone`` and ``notkick.com`` as Kick links
    (CodeQL ``py/incomplete-url-substring-sanitization``).
    """
    if not isinstance(value, str):
        return ""
    text = value.strip()
    if not text:
        return ""
    if "://" in text or text.lower().startswith(("www.kick.com", "kick.com")):
        parts = urlsplit(text if "://" in text else "https://" + text)
        host = (parts.hostname or "").lower()
        if host != "kick.com" and not host.endswith(".kick.com"):
            return ""
        text = parts.path
    slug = text.split("?")[0].split("#")[0].strip("/").split("/")[0].strip().lower()
    slug = slug.lstrip("@").strip()
    return slug if SLUG_RE.match(slug) else ""


def ping_mention(guild: discord.Guild, role_id: Any) -> Optional[str]:
    """Mention text for a stored role id, safe for the @everyone role.

    ``Role.mention`` is always ``<@&id>``; for the default role that id is the
    guild id, and Discord renders it as ``@@everyone`` without pinging. The
    literal ``@everyone`` is the only form that displays and pings correctly.
    """
    parsed = _as_int(role_id)
    if not parsed:
        return None
    if parsed == guild.id:
        return "@everyone"
    role = guild.get_role(parsed)
    return role.mention if role is not None else None


def pings_everyone(guild: discord.Guild, role_id: Any) -> bool:
    """Whether a stored ping-role id is the guild's default (@everyone) role."""
    return _as_int(role_id) == guild.id


def _retry_delay(attempt: int, status: int, retry_after: Any = None) -> float:
    """Seconds to wait before retrying a failed request."""
    seconds = _as_int(retry_after)
    if isinstance(retry_after, str):
        with contextlib.suppress(ValueError):
            seconds = int(float(retry_after))
    if seconds:
        return min(float(seconds), 30.0)
    delay = 1.5**attempt
    return min(DEFAULT_RETRY_AFTER, delay) if status == 429 else delay


def _short(payload: Any, limit: int = 200) -> str:
    """Compact a payload for an error message."""
    text = payload if isinstance(payload, str) else repr(payload)
    return text[:limit] + ("..." if len(text) > limit else "")


def _clamp_interval(seconds: Any) -> int:
    """Clamp a configured poll interval into the supported range."""
    value = _as_int(seconds) or DEFAULT_GUILD["check_interval"]
    return max(MIN_INTERVAL, min(MAX_INTERVAL, value))


def _clamp_clip_mb(value: Any) -> int:
    """Clamp the clip file size setting into 1-20 MiB.

    MiB, not MB: Discord's 20 MiB limit is 20 * 1024 * 1024 bytes, and a clip
    measured as "20 MB" in decimal would already be rejected by the upload.
    """
    megabytes = _as_int(value)
    if megabytes is None:
        return int(DEFAULT_GUILD["clips_max_mb"])
    return max(1, min(MAX_CLIP_MIB, megabytes))


def _clamp_clips_interval(seconds: Any) -> int:
    """Clamp a stored clip poll interval into ``[MIN, MAX]_CLIPS_INTERVAL``."""
    value = _as_int(seconds)
    if value is None:
        return int(DEFAULT_GUILD["clips_interval"])
    return max(MIN_CLIPS_INTERVAL, min(MAX_CLIPS_INTERVAL, value))


def _clamp_update_seconds(seconds: Any) -> int:
    """Clamp the live-embed refresh interval (60s .. 3600s)."""
    value = _as_int(seconds) or DEFAULT_GUILD["live_update_seconds"]
    return max(60, min(3600, value))


def _clamp_event_hours(hours: Any) -> int:
    """Clamp the nominal event length into ``[MIN, MAX]_EVENT_HOURS``."""
    value = _as_int(hours) or EVENT_DEFAULT_HOURS
    return max(MIN_EVENT_HOURS, min(MAX_EVENT_HOURS, value))


def _stamp(moment: Optional[datetime], style: str = "R") -> Optional[str]:
    """Discord timestamp markup for a datetime."""
    if moment is None:
        return None
    return f"<t:{int(moment.timestamp())}:{style}>"


# --------------------------------------------------------------------------- #
# API client
# --------------------------------------------------------------------------- #
class KickAPI:
    """Minimal client for the Kick public API.

    ``_http_json`` is the single network seam: it returns ``(status, payload)``,
    so tests (or a different transport) can replace it without touching aiohttp.
    """

    def __init__(self) -> None:
        self._session: Optional[aiohttp.ClientSession] = None
        self._client_id: Optional[str] = None
        self._client_secret: Optional[str] = None
        self._token: Optional[str] = None
        self._token_expires_at: float = 0.0
        self._lock = asyncio.Lock()
        self.last_error: Optional[str] = None

    # -- credentials & lifecycle ------------------------------------------- #
    @property
    def has_credentials(self) -> bool:
        """Whether a client id and secret are configured."""
        return bool(self._client_id and self._client_secret)

    @property
    def token_cached(self) -> bool:
        """Whether a usable token is currently held."""
        return bool(self._token and time.time() < self._token_expires_at - 60)

    def set_credentials(
        self, client_id: Optional[str], client_secret: Optional[str]
    ) -> None:
        """Store credentials; drops any cached token if they changed."""
        if (client_id, client_secret) != (self._client_id, self._client_secret):
            self._token = None
            self._token_expires_at = 0.0
        self._client_id = client_id or None
        self._client_secret = client_secret or None

    async def open(self) -> None:
        """Create the HTTP session."""
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=20)
            )

    async def close(self) -> None:
        """Close the HTTP session."""
        if self._session is not None and not self._session.closed:
            await self._session.close()
        self._session = None

    # -- HTTP --------------------------------------------------------------- #
    async def _http_json(
        self,
        method: str,
        url: str,
        *,
        params: Optional[Sequence[Tuple[str, str]]] = None,
        data: Optional[Dict[str, str]] = None,
        headers: Optional[Dict[str, str]] = None,
    ) -> Tuple[int, Any]:
        """Perform one request and return ``(status, parsed_body)``."""
        await self.open()
        if self._session is None:  # open() could not establish one
            raise RuntimeError("KickAlerts: HTTP session is not available")
        async with self._session.request(
            method, url, params=params, data=data, headers=headers
        ) as response:
            retry_after = response.headers.get("Retry-After")
            try:
                payload: Any = await response.json(content_type=None)
            except Exception:  # noqa: BLE001 - a broken body means "show the text"
                payload = await response.text()
            if retry_after is not None and isinstance(payload, dict):
                payload.setdefault("retry_after", retry_after)
            return response.status, payload

    async def fetch_bytes(
        self, url: str, *, limit: int = EVENT_IMAGE_LIMIT
    ) -> Optional[bytes]:
        """Download a small file (the event cover) or return ``None``.

        The cover is cosmetic, so every failure is a quiet ``None`` instead of
        an exception the caller has to handle.
        """
        if not url:
            return None
        await self.open()
        if self._session is None:
            return None
        try:
            async with self._session.get(url) as response:
                if response.status != 200:
                    return None
                if (response.content_length or 0) > limit:
                    return None
                data = await response.content.read(limit + 1)
        except (aiohttp.ClientError, asyncio.TimeoutError):
            return None
        return data if 0 < len(data) <= limit else None

    async def _request_json(
        self,
        method: str,
        url: str,
        *,
        params: Optional[Sequence[Tuple[str, str]]] = None,
        data: Optional[Dict[str, str]] = None,
        headers: Optional[Dict[str, str]] = None,
        retry_auth: bool = True,
    ) -> Any:
        """Request JSON with retries, honouring 429 and refreshing stale tokens."""
        attempt = 0
        while True:
            attempt += 1
            status, payload = await self._http_json(
                method, url, params=params, data=data, headers=headers
            )
            if 200 <= status < 300:
                return payload
            if status in (401, 403):
                if retry_auth:
                    # An expired token is the common cause; force a fresh one once.
                    self._token = None
                    self._token_expires_at = 0.0
                    headers = await self._auth_headers()
                    retry_auth = False
                    continue
                self.last_error = f"HTTP {status}: {payload}"
                raise KickAuthError(
                    f"Kick rejected the request (HTTP {status}) — check the client "
                    f"id/secret: {_short(payload)}"
                )
            if status == 429 or status >= 500:
                if attempt >= MAX_ATTEMPTS:
                    if status == 429:
                        raise KickRateLimited(
                            "Kick is rate limiting this bot (HTTP 429), giving up "
                            f"after {attempt} attempts."
                        )
                    self.last_error = f"HTTP {status}: {payload}"
                    raise KickError(f"Kick answered HTTP {status}: {_short(payload)}")
                retry_after = (
                    payload.get("retry_after") if isinstance(payload, dict) else None
                )
                await asyncio.sleep(_retry_delay(attempt, status, retry_after))
                continue
            self.last_error = f"HTTP {status}: {payload}"
            raise KickError(f"Kick answered HTTP {status}: {_short(payload)}")

    # -- auth --------------------------------------------------------------- #
    async def _auth_headers(self) -> Dict[str, str]:
        """Headers carrying a valid bearer token."""
        token = await self.access_token()
        return {"Authorization": f"Bearer {token}", "Accept": "application/json"}

    async def access_token(self, *, force: bool = False) -> str:
        """Return a cached app access token, fetching one when needed."""
        async with self._lock:
            if not force and self.token_cached:
                return self._token  # type: ignore[return-value]
            if not self.has_credentials:
                raise KickAuthError(
                    "No Kick API credentials configured. Set them with "
                    "`[p]kickalert setcreds <client_id> <client_secret>` or "
                    "`[p]set api kick client_id,<id> client_secret,<secret>`."
                )
            payload = await self._request_json(
                "POST",
                KICK_AUTH_URL,
                data={
                    "grant_type": "client_credentials",
                    "client_id": self._client_id or "",
                    "client_secret": self._client_secret or "",
                },
                headers={
                    "Content-Type": "application/x-www-form-urlencoded",
                    "Accept": "application/json",
                },
                retry_auth=False,
            )
            token = payload.get("access_token") if isinstance(payload, dict) else None
            if not token:
                raise KickAuthError(
                    "Kick did not return an access token — check the client id "
                    "and secret."
                )
            expires_in = _as_int(payload.get("expires_in")) or 3600
            self._token = token
            self._token_expires_at = time.time() + expires_in
            self.last_error = None
            log.info("Kick token obtained, valid for %ss", expires_in)
            return token

    # -- data --------------------------------------------------------------- #
    async def get_channels(self, slugs: Iterable[str]) -> Dict[str, StreamInfo]:
        """Look up channels by slug, batched to the API's 50-per-request limit.

        Raises:
            KickAuthError: credentials missing or rejected.
            KickRateLimited: the API kept answering 429.
            KickError: any other API failure.
        """
        wanted = [slug for slug in dict.fromkeys(_clean_slug(s) for s in slugs) if slug]
        found: Dict[str, StreamInfo] = {}
        for index in range(0, len(wanted), MAX_SLUGS_PER_REQUEST):
            batch = wanted[index : index + MAX_SLUGS_PER_REQUEST]
            headers = await self._auth_headers()
            payload = await self._request_json(
                "GET",
                KICK_CHANNELS_URL,
                params=[("slug", slug) for slug in batch],
                headers=headers,
            )
            data = payload.get("data") if isinstance(payload, dict) else None
            if not isinstance(data, list):
                raise KickError(f"Unexpected payload from Kick: {_short(payload)}")
            for entry in data:
                if not isinstance(entry, dict):
                    continue
                info = StreamInfo.from_payload(entry)
                if info.slug:
                    found[info.slug] = info
            if index + MAX_SLUGS_PER_REQUEST < len(wanted):
                await asyncio.sleep(0.25)
        self.last_error = None
        return found

    async def get_channel(self, slug: str) -> Optional[StreamInfo]:
        """Look up a single channel, or ``None`` if it does not exist."""
        cleaned = _clean_slug(slug)
        if not cleaned:
            return None
        return (await self.get_channels([cleaned])).get(cleaned)

    async def download_clip(
        self, clip_url: Optional[str], max_bytes: int, timeout: int = 180
    ) -> Optional[bytes]:
        """Remux a clip's HLS playlist into an MP4 and return it.

        Kick serves clips only as ``.m3u8`` playlists (segments are ``.ts``), so a
        playable file needs ffmpeg. Everything else - a missing binary, a broken
        playlist, a conversion timeout - returns ``None`` instead of raising, so a
        failed conversion can never cost the clip announcement.
        """
        if not clip_url or ".m3u8" not in clip_url:
            return None
        ffmpeg = shutil.which("ffmpeg")
        if ffmpeg is None:
            raise KickError("ffmpeg is not installed, cannot convert clips")
        with tempfile.TemporaryDirectory(prefix="kickclip-") as workdir:
            target = Path(workdir) / "clip.mp4"
            command = [
                ffmpeg,
                "-v",
                "error",
                "-y",
                "-i",
                clip_url,
                "-t",
                "900",
                "-c",
                "copy",
                "-bsf:a",
                "aac_adtstoasc",
                str(target),
            ]
            proc = await asyncio.create_subprocess_exec(
                *command,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
            )
            try:
                _, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
            except asyncio.TimeoutError:
                proc.kill()
                await proc.wait()
                log.info("KickAlerts: clip conversion timed out")
                return None
            if proc.returncode != 0 or not target.exists():
                log.info(
                    "KickAlerts: clip conversion failed: %s",
                    (stderr or b"")[:300].decode("utf-8", "replace"),
                )
                return None
            if target.stat().st_size > max_bytes:
                log.info(
                    "KickAlerts: clip is %s bytes, over the %s byte limit",
                    target.stat().st_size,
                    max_bytes,
                )
                return None
            return target.read_bytes()

    async def get_clips(self, slug: str, limit: int = 10) -> List[ClipInfo]:
        """The newest clips of one channel, from Kick's web endpoint.

        The public API exposes no clip list, so this is the one request in the
        cog that does not go through ``api.kick.com`` - and it needs no token,
        which means clip alerts keep working when the credentials are missing.
        The endpoint ignores ``limit`` and answers with its own page (20 clips),
        so the caller compares ids instead of relying on a slice.
        """
        cleaned = _clean_slug(slug)
        if not cleaned:
            return []
        url = KICK_CLIPS_URL.format(slug=cleaned)
        status, payload = await self._http_json(
            "GET",
            url,
            params=(
                ("limit", str(max(1, min(50, limit)))),
                # Cloudflare answers this endpoint with ``max-age=14400``, so a
                # repeated poll would read a four-hour-old list. A changing
                # parameter forces a MISS and therefore a real answer.
                ("_", str(int(time.time() * 1000))),
            ),
            headers={"User-Agent": BROWSER_UA, "Accept": "application/json"},
        )
        if status == 429:
            raise KickRateLimited("kick.com is rate limiting the clip endpoint")
        if status != 200:
            raise KickError(f"kick.com answered HTTP {status} for {cleaned}'s clips")
        return parse_clips(cleaned, payload)


@dataclass(frozen=True, slots=True)
class ClipInfo:
    """One clip from a channel's clip list."""

    id: str
    slug: str
    title: Optional[str] = None
    thumbnail: Optional[str] = None
    video_url: Optional[str] = None
    duration: Optional[int] = None
    views: Optional[int] = None
    likes: Optional[int] = None
    creator: Optional[str] = None
    category: Optional[str] = None
    created_at: Optional[datetime] = None
    mature: bool = False

    @property
    def url(self) -> str:
        """The public page of this clip."""
        return f"{KICK_SITE}/{self.slug}/clips/{self.id}"


def parse_clips(slug: str, payload: Any) -> List[ClipInfo]:
    """Turn a ``/api/v2/channels/<slug>/clips`` answer into :class:`ClipInfo`.

    The endpoint returns the newest clips first; private clips are dropped so a
    hidden clip is never announced.
    """
    if not isinstance(payload, dict):
        return []
    raw = payload.get("clips")
    if not isinstance(raw, list):
        return []
    clips: List[ClipInfo] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        clip_id = _as_text(item.get("id"))
        if not clip_id:
            continue
        privacy = (_as_text(item.get("privacy")) or "public").lower()
        if privacy != "public":
            continue
        creator = _as_dict(item.get("creator"))
        category = _as_dict(item.get("category"))
        views = _as_int(item.get("view_count"))
        if views is None:
            views = _as_int(item.get("views"))
        likes = _as_int(item.get("likes_count"))
        if likes is None:
            likes = _as_int(item.get("likes"))
        clips.append(
            ClipInfo(
                id=clip_id,
                slug=slug,
                title=_as_text(item.get("title")),
                thumbnail=_as_text(item.get("thumbnail_url")),
                video_url=_as_text(item.get("video_url"))
                or _as_text(item.get("clip_url")),
                duration=_as_int(item.get("duration")),
                views=views,
                likes=likes,
                creator=_as_text(creator.get("username")),
                category=_as_text(category.get("name")),
                created_at=parse_kick_time(item.get("created_at")),
                mature=bool(item.get("is_mature")),
            )
        )
    return clips


# --------------------------------------------------------------------------- #
# embeds
# --------------------------------------------------------------------------- #
@dataclass(frozen=True, slots=True)
class EmbedSettings:
    """How a guild wants its alerts rendered."""

    style: str = "detailed"
    show_viewers: bool = True
    show_category: bool = True
    timezone_offset: int = 1

    @classmethod
    def from_guild_config(cls, data: Dict[str, Any]) -> "EmbedSettings":
        """Build settings from a raw guild config dict."""
        style = str(data.get("embed_style") or "detailed").lower()
        return cls(
            style="minimal" if style == "minimal" else "detailed",
            show_viewers=bool(data.get("show_viewer_count", True)),
            show_category=bool(data.get("show_category", True)),
            timezone_offset=_as_int(data.get("timezone_offset")) or 0,
        )

    @property
    def tz(self) -> timezone:
        """The configured timezone."""
        return timezone(timedelta(hours=max(-12, min(14, self.timezone_offset))))


@dataclass(slots=True)
class EventSettings:
    """How a guild wants its Discord scheduled events built."""

    enabled: bool = False
    hours: int = EVENT_DEFAULT_HOURS
    cover: bool = True
    offline: str = "delete"
    name: Optional[str] = DEFAULT_EVENT_NAME
    description: Optional[str] = None

    @classmethod
    def from_guild_config(cls, data: Dict[str, Any]) -> "EventSettings":
        """Build settings from a raw guild config dict."""
        mode = str(data.get("event_offline") or "delete").lower()
        name = data.get("event_name")
        description = data.get("event_description")
        return cls(
            enabled=bool(data.get("events_enabled", False)),
            hours=_clamp_event_hours(data.get("event_hours")),
            cover=bool(data.get("event_cover", True)),
            offline=mode if mode in EVENT_OFFLINE_MODES else "delete",
            name=str(name) if name else None,
            description=str(description) if description else None,
        )


def build_live_embed(info: StreamInfo, settings: EmbedSettings) -> discord.Embed:
    """The "is live" announcement embed."""
    embed = discord.Embed(
        colour=discord.Colour(KICK_COLOR),
        title=info.title or "Live on Kick",
        url=info.url,
        timestamp=datetime.now(settings.tz),
    )
    embed.set_author(name=f"{info.slug} is live on Kick", url=info.url)

    if settings.style == "minimal":
        parts: List[str] = []
        if settings.show_category and info.category:
            parts.append(f"Playing **{info.category}**")
        if settings.show_viewers:
            parts.append(f"{info.viewers:,} viewers")
        parts.append(f"**[Watch Now]({info.url})**")
        embed.description = " | ".join(parts)
    else:
        lines: List[str] = []
        if settings.show_category and info.category:
            lines.append(f"**Category:** {info.category}")
        if settings.show_viewers:
            lines.append(f"**Viewers:** {info.viewers:,}")
        started = _stamp(info.started_at)
        if started:
            lines.append(f"**Started:** {started} ({_stamp(info.started_at, 't')})")
        uptime = format_uptime(info.uptime)
        if uptime:
            lines.append(f"**Live for:** {uptime}")
        if info.language:
            lines.append(f"**Language:** {info.language}")
        if info.active_subscribers:
            lines.append(f"**Subscribers:** {info.active_subscribers:,}")
        if info.is_mature:
            lines.append("**18+ mature content**")
        if info.tags:
            lines.append("**Tags:** " + " ".join(f"`{tag}`" for tag in info.tags[:5]))
        lines.append("")
        lines.append(f"**[Watch Stream on Kick]({info.url})**")
        embed.description = "\n".join(lines)

    if info.thumbnail:
        embed.set_image(url=info.thumbnail)
    if info.category_thumbnail:
        embed.set_thumbnail(url=info.category_thumbnail)
    embed.set_footer(text="Kick.com | Live stream alert")
    return embed


def build_offline_embed(
    info: StreamInfo,
    settings: EmbedSettings,
    summary: Optional[StreamSummary] = None,
) -> discord.Embed:
    """The "stream ended" embed that replaces the live alert."""
    summary = summary or summarise_stream(info, {})
    lines = [f"**{info.slug}** is offline."]
    if summary.title:
        lines.append(f"> {summary.title}")
    lines.append(f"[Visit channel]({info.url})")
    embed = discord.Embed(
        colour=discord.Colour(OFFLINE_COLOR),
        description="\n".join(lines),
        timestamp=datetime.now(settings.tz),
    )
    embed.set_author(name=f"{info.slug} is offline", url=info.url)
    for name, value in _summary_pairs(summary):
        embed.add_field(name=name, value=value, inline=True)
    if info.banner:
        embed.set_thumbnail(url=info.banner)
    embed.set_footer(text=_summary_footer("Stream ended"))
    return embed


def build_summary_embed(
    info: StreamInfo, settings: EmbedSettings, summary: StreamSummary
) -> discord.Embed:
    """A fresh post for the end of a stream, with the stream's statistics."""
    embed = discord.Embed(
        colour=discord.Colour(OFFLINE_COLOR),
        title=f"{summary.slug} — stream ended",
        url=info.url,
        timestamp=datetime.now(settings.tz),
    )
    embed.set_author(name=f"{summary.slug} is offline", url=info.url)
    lines: List[str] = []
    if summary.title:
        lines.append(f"**{summary.title}**")
    if summary.category:
        lines.append(f"**Category:** {summary.category}")
    lines.append(f"[Visit channel]({info.url})")
    embed.description = "\n".join(lines)
    for name, value in _summary_pairs(summary):
        embed.add_field(name=name, value=value, inline=True)
    if info.banner:
        embed.set_thumbnail(url=info.banner)
    embed.set_footer(text=_summary_footer("Stream summary"))
    return embed


def build_clip_embed(clip: ClipInfo, settings: EmbedSettings) -> discord.Embed:
    """The "new clip" announcement embed."""
    embed = discord.Embed(
        colour=discord.Colour(KICK_COLOR),
        title=clip.title or "New clip on Kick",
        url=clip.url,
        timestamp=datetime.now(settings.tz),
    )
    embed.set_author(name=f"New clip from {clip.slug} on Kick", url=clip.url)
    lines: List[str] = []
    if settings.show_category and clip.category:
        lines.append(f"**Category:** {clip.category}")
    if settings.show_viewers and clip.views is not None:
        lines.append(f"**Views:** {clip.views:,}")
    if clip.creator:
        lines.append(f"**Clipped by:** {clip.creator}")
    if clip.duration:
        length = format_uptime(timedelta(seconds=clip.duration))
        if length:
            lines.append(f"**Length:** {length}")
    created = _stamp(clip.created_at)
    if created:
        lines.append(f"**Created:** {created}")
    if clip.mature:
        lines.append("**18+ mature content**")
    lines.append("")
    lines.append(f"**[Watch the clip on Kick]({clip.url})**")
    embed.description = "\n".join(lines)
    if clip.thumbnail:
        embed.set_image(url=clip.thumbnail)
    embed.set_footer(text="Kick.com | New clip")
    return embed


def build_status_embed(info: StreamInfo, settings: EmbedSettings) -> discord.Embed:
    """The embed used by ``[p]kickalert check``."""
    if info.is_live:
        return build_live_embed(info, settings)
    description = [f"**{info.slug}** is not streaming.", ""]
    if info.category:
        description.append(f"**Last category:** {info.category}")
    if info.title:
        description.append(f"**Last stream:** {info.title}")
    if info.active_subscribers:
        description.append(f"**Subscribers:** {info.active_subscribers:,}")
    description.append("")
    description.append(f"[Visit channel]({info.url})")
    embed = discord.Embed(
        colour=discord.Colour(OFFLINE_COLOR),
        title=f"{info.slug} is offline",
        url=info.url,
        description="\n".join(description),
        timestamp=datetime.now(settings.tz),
    )
    if info.banner:
        embed.set_thumbnail(url=info.banner)
    embed.set_footer(text="Kick.com")
    return embed


def build_debug_embed(slug: str, info: Optional[StreamInfo]) -> discord.Embed:
    """Raw payload view for ``[p]kickalert debug``; credentials never appear."""
    embed = discord.Embed(
        colour=discord.Colour(DEBUG_COLOR),
        title=f"Kick API debug: {slug}",
        timestamp=datetime.now(timezone.utc),
    )
    if info is None:
        embed.description = "The API returned no data for this slug."
        return embed
    lines = [
        f"**is_live:** {info.is_live}",
        f"**viewers:** {info.viewers}",
        f"**stream_key:** {info.stream_key}",
        f"**started_at:** {info.started_at}",
        "",
        "**=== raw payload ===**",
    ]
    for key, value in info.raw.items():
        text = str(value)
        if len(text) > 120:
            text = text[:120] + "..."
        lines.append(f"**{key}:** {text}")
    body = "\n".join(lines)
    if len(body) > 3900:
        body = body[:3900] + "\n... truncated"
    embed.description = body
    embed.set_footer(text="Raw API data — no credentials are read or shown here")
    return embed


def _as_demo_stream(info: StreamInfo) -> StreamInfo:
    """Turn an offline channel into a fake live one for ``[p]kickalert test``."""
    return StreamInfo(
        slug=info.slug,
        broadcaster_user_id=info.broadcaster_user_id,
        is_live=True,
        title=info.title or "Testing the alert settings",
        category=info.category or "Just Chatting",
        category_thumbnail=info.category_thumbnail,
        viewers=info.viewers or 1234,
        started_at=datetime.now(timezone.utc) - timedelta(minutes=42),
        language=info.language or "en",
        is_mature=info.is_mature,
        thumbnail=info.thumbnail,
        tags=info.tags or ("test",),
        banner=info.banner,
        description=info.description,
        active_subscribers=info.active_subscribers,
        raw=info.raw,
    )


# --------------------------------------------------------------------------- #
# the cog
# --------------------------------------------------------------------------- #
class KickAlerts(commands.Cog):
    """Announce Kick.com livestreams in Discord."""

    __version__ = "3.4.1"

    def __init__(self, bot: Red) -> None:
        self.bot = bot
        self.config = Config.get_conf(
            self, identifier=7274927492, force_registration=True
        )
        self.config.register_global(**DEFAULT_GLOBAL)
        self.config.register_guild(**DEFAULT_GUILD)
        self.api = KickAPI()
        self._watch_task: Optional[asyncio.Task] = None
        self._clips_task: Optional[asyncio.Task] = None
        # One conversion at a time: ffmpeg is cheap per clip but not free, and a
        # backlog of new clips should not spawn ten of them at once.
        self._clip_lock = asyncio.Semaphore(1)
        self._last_poll: Optional[datetime] = None
        self._last_result: Dict[str, int] = {}
        #: Guilds already warned about the missing Create Events permission.
        self._event_permission_warned: set = set()

    # -- lifecycle ---------------------------------------------------------- #
    async def cog_load(self) -> None:
        """Load credentials, migrate legacy ones and start both watchers."""
        await self._load_credentials(migrate=True)
        self._watch_task = asyncio.create_task(self._watch_loop())
        self._clips_task = asyncio.create_task(self._clips_loop())

    def cog_unload(self) -> None:
        """Stop both watchers and close the HTTP session.

        Red never awaits ``cog_unload``, so this must be synchronous: an
        ``async def`` here leaves the loops running after an unload and leaks the
        session on every reload.
        """
        for task in (self._watch_task, self._clips_task):
            if task is not None:
                task.cancel()
        self._watch_task = None
        self._clips_task = None
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None
        if loop is not None:
            loop.create_task(self.api.close())

    async def red_delete_data_for_user(self, *, requester: str, user_id: int) -> None:
        """Nothing is stored per user — everything here is guild level."""
        return

    async def red_get_data_for_user(self, *, user_id: int) -> Dict[str, io.BytesIO]:
        """Nothing is stored per user — everything here is guild level."""
        return {}

    def format_help_for_context(self, ctx: commands.Context) -> str:
        """Add the cog version to ``[p]help``."""
        base = super().format_help_for_context(ctx)
        return f"{base}\n\nCog version: {self.__version__}"

    # -- credentials -------------------------------------------------------- #
    async def _load_credentials(
        self, *, migrate: bool = False
    ) -> Tuple[Optional[str], Optional[str]]:
        """Read the shared API tokens, migrating pre-3.0 Config values once."""
        tokens = await self.bot.get_shared_api_tokens(SHARED_API_SERVICE)
        client_id = tokens.get("client_id")
        client_secret = tokens.get("client_secret")
        if not client_id and migrate:
            legacy_id = await self.config.legacy_client_id()
            legacy_secret = await self.config.legacy_client_secret()
            if legacy_id and legacy_secret:
                await self.bot.set_shared_api_tokens(
                    SHARED_API_SERVICE, client_id=legacy_id, client_secret=legacy_secret
                )
                await self.config.legacy_client_id.set(None)
                await self.config.legacy_client_secret.set(None)
                client_id, client_secret = legacy_id, legacy_secret
                log.info(
                    "KickAlerts: moved the stored credentials into Red's shared API tokens"
                )
        self.api.set_credentials(client_id, client_secret)
        return client_id, client_secret

    async def _credentials_set(self) -> bool:
        """Whether usable credentials are configured."""
        if not self.api.has_credentials:
            await self._load_credentials()
        return self.api.has_credentials

    # -- polling ------------------------------------------------------------ #
    async def _watch_loop(self) -> None:
        """Poll the API forever; no failure is allowed to kill this loop."""
        await self.bot.wait_until_ready()
        while True:
            delay = 60.0
            try:
                if await self._credentials_set():
                    delay = float(await self._next_interval())
                    self._last_result = await self._check_all_guilds()
                    self._last_poll = datetime.now(timezone.utc)
                else:
                    delay = 300.0
            except asyncio.CancelledError:
                raise
            except KickAuthError as exc:
                log.warning("KickAlerts: authentication problem: %s", exc)
                delay = 300.0
            except KickRateLimited as exc:
                log.warning("KickAlerts: %s", exc)
                delay = 120.0
            except KickError as exc:
                log.warning("KickAlerts: API problem: %s", exc)
                delay = 120.0
            except Exception:  # noqa: BLE001 - a poll must never take the cog down
                log.exception("KickAlerts: unexpected error while polling")
                delay = 120.0
            # Jitter keeps several bots from polling in lockstep.
            await asyncio.sleep(delay * random.uniform(0.95, 1.05))

    async def _next_interval(self) -> int:
        """The shortest interval any guild with active streamers asked for."""
        guilds = await self.config.all_guilds()
        intervals = [
            _clamp_interval(data.get("check_interval"))
            for data in guilds.values()
            if data.get("streamers") and data.get("enabled", True)
        ]
        return min(intervals) if intervals else DEFAULT_GUILD["check_interval"]

    # -- clip polling -------------------------------------------------------- #
    async def _clips_loop(self) -> None:
        """Poll every watched channel's clip list forever."""
        await self.bot.wait_until_ready()
        while True:
            delay = float(DEFAULT_GUILD["clips_interval"])
            try:
                delay = float(await self._clips_interval())
                await self._check_all_clips()
            except asyncio.CancelledError:
                raise
            except KickRateLimited as exc:
                log.warning("KickAlerts: clips: %s", exc)
                delay = max(delay, 300.0)
            except KickError as exc:
                log.warning("KickAlerts: clips: %s", exc)
                delay = max(delay, 120.0)
            except Exception:  # noqa: BLE001 - a poll must never kill the loop
                log.exception("KickAlerts: unexpected error while polling clips")
                delay = max(delay, 300.0)
            await asyncio.sleep(delay * random.uniform(0.95, 1.05))

    async def _clips_interval(self) -> int:
        """The shortest clip interval any guild asked for."""
        guilds = await self.config.all_guilds()
        values = [
            _clamp_clips_interval(data.get("clips_interval"))
            for data in guilds.values()
            if data.get("clips_enabled")
            and data.get("streamers")
            and data.get("enabled", True)
        ]
        return min(values) if values else int(DEFAULT_GUILD["clips_interval"])

    async def _check_all_clips(self) -> Dict[str, int]:
        """One clip pass: fetch every watched channel, then announce per guild."""
        guilds = await self.config.all_guilds()
        watched = {
            guild_id: data
            for guild_id, data in guilds.items()
            if data.get("clips_enabled")
            and data.get("streamers")
            and data.get("enabled", True)
        }
        result = {"guilds": len(watched), "channels": 0, "clips": 0}
        if not watched:
            return result
        slugs = sorted(
            {slug for data in watched.values() for slug in data["streamers"]}
        )
        result["channels"] = len(slugs)
        clips: Dict[str, List[ClipInfo]] = {}
        for slug in slugs:
            try:
                clips[slug] = await self.api.get_clips(slug, 10)
            except KickRateLimited:
                raise
            except KickError as exc:
                log.debug("KickAlerts: no clip list for %s: %s", slug, exc)
            # kick.com is a web host, not the API: keep the sweep gentle.
            await asyncio.sleep(0.3)
        for guild_id, data in watched.items():
            guild = self.bot.get_guild(guild_id)
            if guild is None:
                continue
            if await self.bot.cog_disabled_in_guild(self, guild):
                continue
            try:
                result["clips"] += await self._poll_guild_clips(guild, data, clips)
            except Exception:  # noqa: BLE001 - one guild must not stop the sweep
                log.exception("KickAlerts: clip pass for guild %s failed", guild_id)
        return result

    async def _poll_guild_clips(
        self,
        guild: discord.Guild,
        data: Dict[str, Any],
        clips: Dict[str, List[ClipInfo]],
    ) -> int:
        """Announce new clips of one guild's streamers and return the count."""
        settings = EmbedSettings.from_guild_config(data)
        announced = 0
        for slug, entry in (data.get("streamers") or {}).items():
            if slug not in clips:
                continue
            state = {**STREAMER_DEFAULTS, **(entry or {})}
            if not state.get("clips", True) or state.get("muted"):
                # Keep the list current so unmuting does not dump a backlog.
                await self._remember_clips(guild, slug, clips[slug])
                continue
            channel = self._resolve_clips_channel(guild, data, state)
            if channel is None:
                continue
            announced += await self._handle_clips(
                guild, slug, state, clips[slug], channel, settings, data
            )
        return announced

    def _resolve_clips_channel(
        self,
        guild: discord.Guild,
        data: Dict[str, Any],
        state: Dict[str, Any],
    ) -> Optional[discord.TextChannel]:
        """Clip channel: guild setting, then the streamer's own, then the default."""
        for channel_id in (
            data.get("clips_channel_id"),
            state.get("channel_id"),
            data.get("global_channel_id"),
        ):
            if not channel_id:
                continue
            channel = guild.get_channel(int(channel_id))
            if isinstance(channel, discord.TextChannel):
                return channel
        return None

    async def _handle_clips(
        self,
        guild: discord.Guild,
        slug: str,
        state: Dict[str, Any],
        clips: List[ClipInfo],
        channel: discord.TextChannel,
        settings: EmbedSettings,
        data: Optional[Dict[str, Any]] = None,
    ) -> int:
        data = data or {}
        """Post the clips that were not seen before."""
        seen = {str(item) for item in (state.get("seen_clips") or [])}
        if not state.get("clips_primed"):
            # The first scan records the list: a fresh install must not dump
            # every clip the channel ever had.
            await self._remember_clips(guild, slug, clips)
            return 0
        fresh = [clip for clip in clips if clip.id not in seen]
        if not fresh:
            return 0
        if not self._can_post(guild, channel, slug):
            return 0
        announce_file = bool(data.get("clips_attach", True))
        max_mb = _clamp_clip_mb(data.get("clips_max_mb"))
        announced = 0
        # Oldest first, so a backlog reads in the order it was clipped.
        for clip in reversed(fresh):
            try:
                await channel.send(embed=build_clip_embed(clip, settings))
                announced += 1
            except discord.HTTPException:
                log.exception("KickAlerts: could not post the clip %s", clip.id)
                break
            if announce_file:
                await self._send_clip_file(guild, channel, clip, max_mb)
        await self._remember_clips(guild, slug, clips)
        if announced:
            log.info("KickAlerts: announced %s clip(s) for %s", announced, slug)
        return announced

    async def _remember_clips(
        self, guild: discord.Guild, slug: str, clips: List[ClipInfo]
    ) -> None:
        """Merge the current clip ids into the streamer's seen list."""
        async with self.config.guild(guild).streamers() as streamers:
            entry = streamers.get(slug)
            if not isinstance(entry, dict):
                return
            known = [str(item) for item in (entry.get("seen_clips") or [])]
            merged: List[str] = []
            for clip in clips:
                if clip.id not in merged:
                    merged.append(clip.id)
            for clip_id in known:
                if clip_id not in merged:
                    merged.append(clip_id)
            entry["seen_clips"] = merged[:SEEN_CLIP_LIMIT]
            entry["clips_primed"] = True

    async def _send_clip_file(
        self,
        guild: discord.Guild,
        channel: discord.TextChannel,
        clip: ClipInfo,
        max_mb: int,
    ) -> None:
        """Convert the clip to an MP4 and post it under the embed."""
        guild_limit = _as_int(getattr(guild, "filesize_limit", None))
        limit = min(max_mb, MAX_CLIP_MIB) * 1024 * 1024
        if guild_limit:
            limit = min(limit, guild_limit)
        permissions = channel.permissions_for(guild.me)
        if not permissions.attach_files:
            log.warning(
                "KickAlerts: missing Attach Files in #%s, not posting the clip",
                channel,
            )
            return
        async with self._clip_lock:
            try:
                data = await self.api.download_clip(clip.video_url, limit)
            except KickError as exc:
                log.warning("KickAlerts: clip conversion unavailable: %s", exc)
                return
        if not data:
            return
        filename = f"{clip.slug}_{clip.id}.mp4"
        try:
            await channel.send(
                file=discord.File(io.BytesIO(data), filename=filename),
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except discord.HTTPException:
            log.exception("KickAlerts: could not upload the clip %s", clip.id)

    async def _check_all_guilds(self) -> Dict[str, int]:
        """One polling pass: fetch every watched slug and update every guild."""
        guild_configs = await self.config.all_guilds()
        watched = {
            guild_id: data
            for guild_id, data in guild_configs.items()
            if data.get("streamers") and data.get("enabled", True)
        }
        result = {"guilds": len(watched), "streamers": 0, "live": 0, "alerts": 0}
        if not watched:
            return result
        slugs = sorted(
            {slug for data in watched.values() for slug in data["streamers"]}
        )
        result["streamers"] = len(slugs)
        channels = await self.api.get_channels(slugs)
        for guild_id, data in watched.items():
            guild = self.bot.get_guild(guild_id)
            if guild is None:
                continue
            if await self.bot.cog_disabled_in_guild(self, guild):
                continue
            try:
                counts = await self._poll_guild(guild, data, channels)
            except Exception:  # noqa: BLE001 - one bad guild must not stop the rest
                log.exception("KickAlerts: failed to update guild %s", guild_id)
                continue
            result["live"] += counts["live"]
            result["alerts"] += counts["alerts"]
        return result

    async def _poll_guild(
        self,
        guild: discord.Guild,
        data: Dict[str, Any],
        channels: Dict[str, StreamInfo],
    ) -> Dict[str, int]:
        """Update every monitored streamer of one guild."""
        settings = EmbedSettings.from_guild_config(data)
        auto_delete = bool(data.get("auto_delete", False))
        edit_live = bool(data.get("edit_live_alerts", True))
        offline_post = str(data.get("offline_post") or "new").lower()
        if offline_post not in ("new", "edit"):
            offline_post = "new"
        update_seconds = _clamp_update_seconds(data.get("live_update_seconds"))
        global_channel = data.get("global_channel_id")
        global_role = data.get("global_ping_role_id")
        events = EventSettings.from_guild_config(data)
        counts = {"live": 0, "alerts": 0}
        for slug, entry in (data.get("streamers") or {}).items():
            info = channels.get(slug)
            if info is None:
                log.debug("KickAlerts: no API data for %s (guild %s)", slug, guild.id)
                continue
            if info.is_live:
                counts["live"] += 1
            action = await self._handle_streamer(
                guild,
                slug,
                entry,
                info,
                settings,
                events=events,
                auto_delete=auto_delete,
                edit_live=edit_live,
                offline_post=offline_post,
                update_seconds=update_seconds,
                global_channel=global_channel,
                global_role=global_role,
            )
            if action in ("announced", "ended", "updated"):
                counts["alerts"] += 1
        return counts

    async def _handle_streamer(
        self,
        guild: discord.Guild,
        slug: str,
        entry: Dict[str, Any],
        info: StreamInfo,
        settings: EmbedSettings,
        *,
        events: EventSettings,
        auto_delete: bool,
        edit_live: bool,
        offline_post: str,
        update_seconds: int,
        global_channel: Optional[int],
        global_role: Optional[int],
    ) -> str:
        """Apply one streamer's new state and return what happened."""
        state = {**STREAMER_DEFAULTS, **(entry or {})}
        was_live = bool(state.get("is_live"))
        previous_key = state.get("stream_key")
        # The Discord event follows the stream state, never the alert message:
        # it is stored per streamer, so a bot restart adopts a running stream
        # instead of creating a second event.
        await self._sync_event(guild, slug, info, state, events)
        channel = self._resolve_channel(guild, state.get("channel_id"), global_channel)
        if channel is None:
            log.debug(
                "KickAlerts: %s has no usable alert channel in %s", slug, guild.id
            )
            return "no-channel"

        if state.get("muted"):
            # Keep tracking the state so unmuting does not announce a stale stream.
            await self._store_state(
                guild, slug, is_live=info.is_live, stream_key=info.stream_key
            )
            return "muted"

        if info.is_live and (not was_live or info.stream_key != previous_key):
            # A new broadcast: the viewer statistics start from zero.
            stats = accumulate_viewers({}, info.viewers)
            delivered = await self._announce(
                guild, slug, info, settings, channel, state, global_role, stats=stats
            )
            return "announced" if delivered else "no-permission"

        if not info.is_live and was_live:
            await self._end_alert(
                guild,
                slug,
                info,
                settings,
                channel,
                state,
                auto_delete=auto_delete,
                offline_post=offline_post,
            )
            return "ended"

        if info.is_live:
            stats = accumulate_viewers(state, info.viewers)
            if edit_live and self._needs_refresh(state, info, update_seconds):
                edited = await self._refresh_alert(
                    guild, slug, info, settings, channel, state, stats=stats
                )
                return "updated" if edited else "unchanged"
            await self._store_state(
                guild, slug, is_live=True, stream_key=info.stream_key, **stats
            )
            return "unchanged"

        await self._store_state(
            guild,
            slug,
            is_live=False,
            stream_key=None,
            viewer_samples=0,
            viewer_total=0,
            peak_viewers=0,
            last_viewers=0,
        )
        return "unchanged"

    # -- alert actions ------------------------------------------------------ #
    def _resolve_channel(
        self,
        guild: discord.Guild,
        per_streamer: Optional[int],
        global_channel: Optional[int],
    ) -> Optional[discord.TextChannel]:
        """The channel an alert goes to: per streamer first, then the default."""
        for channel_id in (per_streamer, global_channel):
            if not channel_id:
                continue
            channel = guild.get_channel(int(channel_id))
            if isinstance(channel, discord.TextChannel):
                return channel
        return None

    def _alert_content(
        self,
        guild: discord.Guild,
        info: StreamInfo,
        state: Dict[str, Any],
        global_role: Optional[int],
    ) -> Optional[str]:
        """Role ping plus optional custom message for an announcement."""
        lines: List[str] = []
        role_id = state.get("ping_role_id") or global_role
        mention = ping_mention(guild, role_id)
        if mention:
            lines.append(mention)
        template = state.get("custom_message")
        if template:
            lines.append(render_template(str(template), info))
        return "\n".join(lines) if lines else None

    async def _announce(
        self,
        guild: discord.Guild,
        slug: str,
        info: StreamInfo,
        settings: EmbedSettings,
        channel: discord.TextChannel,
        state: Dict[str, Any],
        global_role: Optional[int],
        *,
        stats: Mapping[str, Any],
    ) -> bool:
        """Post the live alert. Returns ``False`` when the bot may not post."""
        if not self._can_post(guild, channel, slug):
            return False
        role_id = state.get("ping_role_id") or global_role
        try:
            message = await channel.send(
                content=self._alert_content(guild, info, state, global_role),
                embed=build_live_embed(info, settings),
                allowed_mentions=discord.AllowedMentions(
                    roles=True, everyone=pings_everyone(guild, role_id)
                ),
            )
        except discord.HTTPException:
            log.exception("KickAlerts: could not post the alert for %s", slug)
            return False
        await self._store_state(
            guild,
            slug,
            is_live=True,
            stream_key=info.stream_key,
            started_at=info.started_at.isoformat() if info.started_at else None,
            alert_message_id=message.id,
            last_edit=time.time(),
            last_title=info.title,
            last_category=info.category,
            **stats,
        )
        log.info("KickAlerts: announced %s in guild %s", slug, guild.id)
        return True

    def _needs_refresh(
        self, state: Dict[str, Any], info: StreamInfo, seconds: int
    ) -> bool:
        """Whether the live alert embed is worth editing right now."""
        if not state.get("alert_message_id"):
            return False
        if (
            state.get("last_title") != info.title
            or state.get("last_category") != info.category
        ):
            return True
        last_edit = state.get("last_edit") or 0
        return (time.time() - float(last_edit)) >= seconds

    async def _refresh_alert(
        self,
        guild: discord.Guild,
        slug: str,
        info: StreamInfo,
        settings: EmbedSettings,
        channel: discord.TextChannel,
        state: Dict[str, Any],
        *,
        stats: Mapping[str, Any],
    ) -> bool:
        """Edit the existing alert message with fresh numbers."""
        message_id = state.get("alert_message_id")
        if not message_id:
            await self._store_state(guild, slug, **stats)
            return False
        try:
            message = await channel.fetch_message(int(message_id))
            await message.edit(embed=build_live_embed(info, settings))
        except discord.NotFound:
            # Someone deleted the alert; stop trying to edit it.
            await self._store_state(
                guild, slug, alert_message_id=None, last_edit=None, **stats
            )
            return False
        except (discord.Forbidden, discord.HTTPException):
            log.debug("KickAlerts: could not refresh the alert for %s", slug)
            return False
        await self._store_state(
            guild,
            slug,
            last_edit=time.time(),
            last_title=info.title,
            last_category=info.category,
            **stats,
        )
        return True

    async def _end_alert(
        self,
        guild: discord.Guild,
        slug: str,
        info: StreamInfo,
        settings: EmbedSettings,
        channel: discord.TextChannel,
        state: Dict[str, Any],
        *,
        auto_delete: bool,
        offline_post: str,
    ) -> None:
        """Finish a stream: post a summary, delete the alert, or edit it in place.

        ``offline_post="new"`` replaces the live alert with a fresh post that
        carries the stream statistics; ``"edit"`` turns the live alert into the
        end-of-stream embed instead (``auto_delete`` affects only that path).
        """
        message_id = state.get("alert_message_id")
        summary = summarise_stream(info, state)
        remove = state.get("delete_after_offline")
        remove = auto_delete if remove is None else bool(remove)
        if offline_post == "new":
            if self._can_post(guild, channel, slug):
                try:
                    await channel.send(
                        embed=build_summary_embed(info, settings, summary)
                    )
                except discord.HTTPException:
                    log.exception(
                        "KickAlerts: could not post the stream summary for %s", slug
                    )
            # The summary replaces the live alert, otherwise both would sit in
            # the channel and the old one would still claim the stream is live.
            if message_id:
                await self._delete_message(channel, message_id, slug)
        elif message_id:
            try:
                message = await channel.fetch_message(int(message_id))
                if remove:
                    await message.delete()
                else:
                    await message.edit(
                        content=None,
                        embed=build_offline_embed(info, settings, summary),
                    )
            except discord.NotFound:
                pass
            except (discord.Forbidden, discord.HTTPException):
                log.debug("KickAlerts: could not update the ended alert for %s", slug)
        await self._store_state(
            guild,
            slug,
            is_live=False,
            stream_key=None,
            alert_message_id=None,
            last_edit=None,
            last_title=None,
            last_category=None,
            started_at=None,
            viewer_samples=0,
            viewer_total=0,
            peak_viewers=0,
            last_viewers=0,
        )

    def _can_post(
        self, guild: discord.Guild, channel: discord.TextChannel, slug: str
    ) -> bool:
        """Whether the bot may post embeds in this channel."""
        permissions = channel.permissions_for(guild.me)
        if permissions.send_messages and permissions.embed_links:
            return True
        log.warning(
            "KickAlerts: missing Send Messages/Embed Links in #%s (guild %s, %s)",
            channel,
            guild.id,
            slug,
        )
        return False

    async def _delete_message(
        self, channel: discord.TextChannel, message_id: Any, slug: str
    ) -> None:
        """Delete a stored alert message, ignoring one that is already gone."""
        try:
            message = await channel.fetch_message(int(message_id))
            await message.delete()
        except discord.NotFound:
            return
        except (discord.Forbidden, discord.HTTPException, TypeError, ValueError):
            log.debug("KickAlerts: could not delete the alert for %s", slug)

    async def _store_state(
        self, guild: discord.Guild, slug: str, **values: Any
    ) -> None:
        """Merge new state into one streamer entry."""
        async with self.config.guild(guild).streamers() as streamers:
            entry = streamers.get(slug)
            if not isinstance(entry, dict):
                return
            entry.update(values)

    # -- Discord scheduled events ------------------------------------------- #
    async def _sync_event(
        self,
        guild: discord.Guild,
        slug: str,
        info: StreamInfo,
        state: Dict[str, Any],
        events: EventSettings,
    ) -> str:
        """Keep the guild's scheduled event in step with one stream.

        Returns what happened: ``event-started``, ``event-kept``,
        ``event-ended``, ``event-off``, ``event-none``,
        ``event-no-permission`` or ``event-error``. A broken event never
        touches the alerts.
        """
        try:
            return await self._sync_event_inner(guild, slug, info, state, events)
        except Exception:  # noqa: BLE001 - the poll must survive a broken event
            log.exception("KickAlerts: event sync failed for %s", slug)
            return "event-error"

    async def _sync_event_inner(
        self,
        guild: discord.Guild,
        slug: str,
        info: StreamInfo,
        state: Dict[str, Any],
        events: EventSettings,
    ) -> str:
        """The actual state machine behind :meth:`_sync_event`."""
        wanted = (
            events.enabled
            and bool(state.get("events", True))
            and not state.get("muted")
        )
        event = await self._stored_event(guild, slug, state)
        if event is not None and event.status in (
            discord.EventStatus.completed,
            discord.EventStatus.canceled,
        ):
            # Discord closed it on its own (end time reached, or an admin did):
            # it no longer represents the running stream.
            event = None
            await self._forget_event(guild, slug)
        if not wanted:
            if event is None:
                return "event-none"
            await self._finish_event(guild, slug, event, events)
            return "event-off"
        if info.is_live:
            if event is not None and state.get("event_stream_key") == info.stream_key:
                await self._extend_event(guild, slug, event, state, events)
                return "event-kept"
            if event is not None:
                # The stream restarted between two polls: close the old card.
                await self._finish_event(guild, slug, event, events)
            return await self._start_event(guild, slug, info, events)
        if event is not None:
            await self._finish_event(guild, slug, event, events)
            return "event-ended"
        return "event-none"

    async def _stored_event(
        self, guild: discord.Guild, slug: str, state: Dict[str, Any]
    ) -> Optional[discord.ScheduledEvent]:
        """The event this cog created for a streamer, while it still exists."""
        raw = state.get("event_id")
        if not raw:
            return None
        try:
            event_id = int(raw)
        except (TypeError, ValueError):
            await self._forget_event(guild, slug)
            return None
        event = guild.get_scheduled_event(event_id)
        if event is not None:
            return event
        try:
            return await guild.fetch_scheduled_event(event_id)
        except discord.NotFound:
            # Deleted by hand: stop pointing at it.
            await self._forget_event(guild, slug)
            return None
        except (discord.Forbidden, discord.HTTPException) as exc:
            log.debug("KickAlerts: cannot fetch the event for %s: %s", slug, exc)
            return None

    async def _forget_event(self, guild: discord.Guild, slug: str) -> None:
        """Drop the stored event id of one streamer."""
        await self._store_state(
            guild, slug, event_id=None, event_stream_key=None, event_end=None
        )

    async def _drop_event(
        self, guild: discord.Guild, slug: str, entry: Optional[Mapping[str, Any]]
    ) -> bool:
        """Delete the event of a streamer that is being removed entirely."""
        raw = (entry or {}).get("event_id")
        if not raw:
            return False
        try:
            event_id = int(raw)
            event = guild.get_scheduled_event(event_id)
            if event is None:
                event = await guild.fetch_scheduled_event(event_id)
            await event.delete(reason="KickAlerts: streamer removed")
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            return False
        except (TypeError, ValueError):
            return False
        log.info("KickAlerts: removed the Discord event of %s", slug)
        return True

    async def _start_event(
        self, guild: discord.Guild, slug: str, info: StreamInfo, events: EventSettings
    ) -> str:
        """Create the guild event for a stream that is live right now."""
        if not self._can_create_events(guild):
            self._warn_missing_events_permission(guild, slug)
            return "event-no-permission"
        # An external event cannot start in the past, so it starts a minute
        # from now and Discord flips it to ACTIVE at that moment.
        start = datetime.now(timezone.utc) + timedelta(seconds=EVENT_START_DELAY)
        end = start + timedelta(hours=events.hours)
        try:
            event = await guild.create_scheduled_event(
                name=build_event_name(info, events.name),
                description=build_event_description(info, events.description),
                start_time=start,
                end_time=end,
                entity_type=discord.EntityType.external,
                privacy_level=discord.PrivacyLevel.guild_only,
                location=info.url[:EVENT_LOCATION_LIMIT],
                reason=f"KickAlerts: {slug} is live",
            )
        except discord.Forbidden:
            log.warning(
                "KickAlerts: no permission to create a Discord event in guild %s "
                "(needs Create Events)",
                guild.id,
            )
            return "event-no-permission"
        except discord.HTTPException as exc:
            log.warning("KickAlerts: could not create the event for %s: %s", slug, exc)
            return "event-error"
        if events.cover:
            await self._apply_cover(event, info, slug)
        await self._store_state(
            guild,
            slug,
            event_id=event.id,
            event_stream_key=info.stream_key,
            event_end=end.timestamp(),
        )
        log.info(
            "KickAlerts: started the Discord event %s for %s in guild %s",
            event.id,
            slug,
            guild.id,
        )
        return "event-started"

    async def _apply_cover(
        self, event: discord.ScheduledEvent, info: StreamInfo, slug: str
    ) -> None:
        """Use the stream thumbnail as the event cover, if it downloads."""
        if not info.thumbnail:
            return
        image = await self.api.fetch_bytes(info.thumbnail)
        if not image:
            return
        try:
            await event.edit(image=image, reason="KickAlerts: stream thumbnail")
        except (discord.Forbidden, discord.HTTPException):
            # The cover is cosmetic; the event itself is already correct.
            log.debug("KickAlerts: the cover image was rejected for %s", slug)

    async def _finish_event(
        self,
        guild: discord.Guild,
        slug: str,
        event: discord.ScheduledEvent,
        events: EventSettings,
    ) -> None:
        """Take the event down when the stream is over."""
        try:
            if (
                events.offline == "complete"
                and event.status == discord.EventStatus.active
            ):
                await event.edit(
                    status=discord.EventStatus.completed,
                    end_time=datetime.now(timezone.utc),
                    reason="KickAlerts: stream ended",
                )
            else:
                # Nothing ran (or history is unwanted): remove it instead of
                # leaving a canceled card behind.
                await event.delete(reason="KickAlerts: stream ended")
        except discord.NotFound:
            pass
        except discord.Forbidden:
            log.warning(
                "KickAlerts: no permission to remove event %s in guild %s",
                event.id,
                guild.id,
            )
        except discord.HTTPException as exc:
            log.warning("KickAlerts: could not finish the event for %s: %s", slug, exc)
        else:
            log.info("KickAlerts: finished the Discord event for %s", slug)
        await self._forget_event(guild, slug)

    async def _extend_event(
        self,
        guild: discord.Guild,
        slug: str,
        event: discord.ScheduledEvent,
        state: Dict[str, Any],
        events: EventSettings,
    ) -> None:
        """Push the end time out while a long stream is still running."""
        if event.status != discord.EventStatus.active:
            return
        try:
            end_ts = float(state.get("event_end") or 0)
        except (TypeError, ValueError):
            end_ts = 0.0
        # Discord completes an external event at its end time, so a stream that
        # runs longer would vanish from the Events tab mid-stream.
        if end_ts and time.time() < end_ts - EVENT_EXTEND_BEFORE:
            return
        new_end = datetime.now(timezone.utc) + timedelta(hours=events.hours)
        try:
            await event.edit(
                end_time=new_end, reason="KickAlerts: stream still running"
            )
        except (discord.Forbidden, discord.HTTPException) as exc:
            log.debug("KickAlerts: could not extend the event for %s: %s", slug, exc)
            return
        await self._store_state(guild, slug, event_end=new_end.timestamp())

    def _can_create_events(self, guild: discord.Guild) -> bool:
        """Whether the bot may create a guild scheduled event here."""
        me = guild.me
        if me is None and self.bot.user is not None:
            me = guild.get_member(self.bot.user.id)
        permissions = getattr(me, "guild_permissions", None)
        if permissions is None:
            return False
        can_create = getattr(permissions, "create_events", None)
        if can_create is None:  # discord.py too old to know the permission
            can_create = permissions.manage_events
        return bool(can_create)

    def _warn_missing_events_permission(self, guild: discord.Guild, slug: str) -> None:
        """Log a missing permission once per guild instead of every poll."""
        if guild.id in self._event_permission_warned:
            return
        self._event_permission_warned.add(guild.id)
        log.warning(
            "KickAlerts: missing Create Events in guild %s - cannot create the "
            "Discord event for %s",
            guild.id,
            slug,
        )

    # -- command group ------------------------------------------------------ #
    @commands.guild_only()
    @commands.group(name="kickalert", aliases=["ka", "kickalerts"])
    @checks.admin_or_permissions(manage_guild=True)
    async def kickalert(self, ctx: commands.Context) -> None:
        """Manage Kick.com livestream alerts."""
        if ctx.invoked_subcommand is None:
            await ctx.send_help(ctx.command)

    # -- setup -------------------------------------------------------------- #
    @kickalert.command(name="setcreds", aliases=["credentials", "auth"])
    @checks.is_owner()
    async def kickalert_setcreds(
        self, ctx: commands.Context, client_id: str, client_secret: str
    ) -> None:
        """Store the Kick API credentials (bot owner only).

        Create them at <https://kick.com/settings/developer>. The command message
        is deleted so the secret does not stay in the channel.
        """
        with contextlib.suppress(discord.HTTPException):
            await ctx.message.delete()
        await self.bot.set_shared_api_tokens(
            SHARED_API_SERVICE, client_id=client_id, client_secret=client_secret
        )
        self.api.set_credentials(client_id, client_secret)
        try:
            await self.api.access_token(force=True)
        except KickError as exc:
            await ctx.send(f"⚠️ Credentials saved, but Kick rejected them: {exc}")
            return
        await ctx.send("✅ Kick API credentials saved and verified.")

    @kickalert.command(name="authstatus")
    @checks.admin_or_permissions(manage_guild=True)
    async def kickalert_authstatus(self, ctx: commands.Context) -> None:
        """Check whether the Kick API authentication works."""
        if not await self._credentials_set():
            await ctx.send(
                "No Kick API credentials are set. Use "
                f"`{ctx.clean_prefix}kickalert setcreds <client_id> <client_secret>` "
                f"or `{ctx.clean_prefix}set api kick client_id,<id> "
                "client_secret,<secret>` — or press the button below to enter them "
                "through Red's secure form (bot owner only).",
                view=SetApiView(
                    default_service=SHARED_API_SERVICE,
                    default_keys={"client_id": "", "client_secret": ""},
                ),
            )
            return
        async with ctx.typing():
            try:
                await self.api.access_token(force=True)
            except KickError as exc:
                await ctx.send(f"❌ Authentication failed: {exc}")
                return
        await ctx.send("✅ Kick API authentication works.")

    @kickalert.command(name="add")
    @checks.admin_or_permissions(manage_guild=True)
    async def kickalert_add(
        self,
        ctx: commands.Context,
        username: str,
        channel: Optional[discord.TextChannel] = None,
    ) -> None:
        """Start monitoring a streamer, optionally in a specific channel.

        Accepts a slug or a channel URL: `[p]kickalert add xqc #stream-alerts`.
        """
        slug = clean_slug(username)
        if not slug:
            await ctx.send(f"❌ `{username}` is not a valid Kick channel name.")
            return
        async with ctx.typing():
            try:
                info = await self.api.get_channel(slug)
            except KickError as exc:
                await ctx.send(f"❌ Kick API problem: {exc}")
                return
        if info is None:
            await ctx.send(
                f"❌ No Kick channel called **{slug}** — check the spelling. "
                "(A renamed streamer also stops resolving under the old slug.)"
            )
            return

        streamers = await self.config.guild(ctx.guild).streamers() or {}
        if slug in streamers:
            await ctx.send(f"**{slug}** is already monitored.")
            return
        if (
            channel is None
            and await self.config.guild(ctx.guild).global_channel_id() is None
        ):
            await ctx.send(
                "No channel given and no default channel set — pass one to `add` or "
                f"run `{ctx.clean_prefix}kickalert setchannel #channel` first."
            )
            return
        async with self.config.guild(ctx.guild).streamers() as stored:
            stored[slug] = {
                **STREAMER_DEFAULTS,
                "channel_id": channel.id if channel else None,
                "is_live": info.is_live,
                "stream_key": info.stream_key,
                "started_at": info.started_at.isoformat() if info.started_at else None,
            }

        status = "currently live" if info.is_live else "offline"
        target = channel.mention if channel else "the default channel"
        embed = discord.Embed(
            colour=discord.Colour(KICK_COLOR),
            title="Streamer added",
            description=(
                f"Now monitoring **[{slug}]({info.url})** — {status}.\n\n"
                f"**Alerts to:** {target}"
            ),
            timestamp=datetime.now(timezone.utc),
        )
        if info.banner:
            embed.set_thumbnail(url=info.banner)
        embed.set_footer(
            text="A stream that is already running is not announced - the next one will be"
        )
        await ctx.send(embed=embed)

    @kickalert.command(name="remove", aliases=["delete", "rm"])
    @checks.admin_or_permissions(manage_guild=True)
    async def kickalert_remove(self, ctx: commands.Context, username: str) -> None:
        """Stop monitoring a streamer."""
        slug = clean_slug(username) or username.strip().lower()
        async with self.config.guild(ctx.guild).streamers() as streamers:
            if slug not in streamers:
                await ctx.send(f"**{slug}** is not monitored.")
                return
            entry = streamers.pop(slug)
        # A removed streamer must not leave its Discord event behind.
        async with ctx.typing():
            await self._drop_event(ctx.guild, slug, entry)
        await ctx.send(f"✅ Removed **{slug}** from the alerts.")

    @kickalert.command(name="list")
    @checks.admin_or_permissions(manage_guild=True)
    async def kickalert_list(self, ctx: commands.Context) -> None:
        """List every monitored streamer."""
        streamers = await self.config.guild(ctx.guild).streamers() or {}
        if not streamers:
            await ctx.send("No streamers monitored yet — add one with `add <name>`.")
            return
        data = await self.config.guild(ctx.guild).all()
        global_channel = data.get("global_channel_id")
        global_role = data.get("global_ping_role_id")
        entries = sorted(streamers.items())
        chunks = [entries[i : i + 8] for i in range(0, len(entries), 8)]
        pages: List[discord.Embed] = []
        for number, chunk in enumerate(chunks, start=1):
            embed = discord.Embed(
                colour=discord.Colour(KICK_COLOR),
                title="Monitored Kick streamers",
                timestamp=datetime.now(EmbedSettings.from_guild_config(data).tz),
            )
            for slug, entry in chunk:
                state = {**STREAMER_DEFAULTS, **(entry or {})}
                channel_id = state.get("channel_id") or global_channel
                channel = f"<#{channel_id}>" if channel_id else "not set"
                role = state.get("ping_role_id") or global_role
                ping = ping_mention(ctx.guild, role) or "none"
                flags = []
                if state.get("muted"):
                    flags.append("paused")
                if state.get("delete_after_offline"):
                    flags.append("deletes alert")
                if state.get("custom_message"):
                    flags.append("custom message")
                status = "🔴 LIVE" if state.get("is_live") else "⚫ offline"
                value = (
                    f"{status}\nChannel: {channel}\nPing: {ping}\n"
                    f"[Profile]({KICK_SITE}/{slug})"
                )
                if flags:
                    value += "\n" + ", ".join(flags)
                embed.add_field(name=slug, value=value, inline=True)
            embed.set_footer(
                text=f"Page {number}/{len(chunks)} · {len(streamers)} streamer(s)"
            )
            pages.append(embed)
        if len(pages) == 1:
            await ctx.send(embed=pages[0])
        else:
            await menu(ctx, pages, DEFAULT_CONTROLS, timeout=120)

    @kickalert.command(name="setchannel", aliases=["channel"])
    @checks.admin_or_permissions(manage_guild=True)
    async def kickalert_setchannel(
        self, ctx: commands.Context, channel: discord.TextChannel
    ) -> None:
        """Set the default channel for alerts without their own channel."""
        await self.config.guild(ctx.guild).global_channel_id.set(channel.id)
        await ctx.send(f"✅ Default alert channel is now {channel.mention}.")

    @kickalert.command(name="clearchannel")
    @checks.admin_or_permissions(manage_guild=True)
    async def kickalert_clearchannel(self, ctx: commands.Context) -> None:
        """Remove the default alert channel (per-streamer channels stay)."""
        await self.config.guild(ctx.guild).global_channel_id.set(None)
        await ctx.send("✅ Default alert channel removed.")

    @kickalert.command(name="setrole", aliases=["pingrole", "role"])
    @checks.admin_or_permissions(manage_guild=True)
    async def kickalert_setrole(
        self, ctx: commands.Context, role: discord.Role, username: Optional[str] = None
    ) -> None:
        """Set the ping role, for one streamer or as the default."""
        note = ""
        can_ping_everyone = ctx.channel.permissions_for(ctx.guild.me).mention_everyone
        if role.is_default() and not can_ping_everyone:
            note = "\n⚠️ I lack **Mention @everyone**, so the alert will not ping."
        mention = ping_mention(ctx.guild, role.id) or role.name
        if username is None:
            await self.config.guild(ctx.guild).global_ping_role_id.set(role.id)
            await ctx.send(f"✅ Default ping role is now {mention}.{note}")
            return
        slug = clean_slug(username) or username.strip().lower()
        async with self.config.guild(ctx.guild).streamers() as streamers:
            if slug not in streamers:
                await ctx.send(f"**{slug}** is not monitored.")
                return
            streamers[slug]["ping_role_id"] = role.id
        await ctx.send(f"✅ **{slug}** now pings {mention}.{note}")

    @kickalert.command(name="removerole")
    @checks.admin_or_permissions(manage_guild=True)
    async def kickalert_removerole(
        self, ctx: commands.Context, username: Optional[str] = None
    ) -> None:
        """Remove the ping role, for one streamer or the default."""
        if username is None:
            await self.config.guild(ctx.guild).global_ping_role_id.set(None)
            await ctx.send("✅ Default ping role removed.")
            return
        slug = clean_slug(username) or username.strip().lower()
        async with self.config.guild(ctx.guild).streamers() as streamers:
            if slug not in streamers:
                await ctx.send(f"**{slug}** is not monitored.")
                return
            streamers[slug]["ping_role_id"] = None
        await ctx.send(f"✅ Ping role removed for **{slug}**.")

    @kickalert.command(name="message", aliases=["custommsg", "msg"])
    @checks.admin_or_permissions(manage_guild=True)
    async def kickalert_message(
        self, ctx: commands.Context, username: str, *, message: Optional[str] = None
    ) -> None:
        """Set a custom message for one streamer's alerts.

        Placeholders: `{streamer}` `{title}` `{game}` `{category}` `{url}`
        `{viewers}` `{language}` `{started}` `{uptime}`.
        """
        slug = clean_slug(username) or username.strip().lower()
        async with self.config.guild(ctx.guild).streamers() as streamers:
            if slug not in streamers:
                await ctx.send(f"**{slug}** is not monitored.")
                return
            streamers[slug]["custom_message"] = message or None
        if message:
            await ctx.send(f"✅ Custom message for **{slug}**:\n{message}")
        else:
            await ctx.send(f"✅ Custom message for **{slug}** cleared.")

    @kickalert.command(name="toggle", aliases=["mute", "pause"])
    @checks.admin_or_permissions(manage_guild=True)
    async def kickalert_toggle(self, ctx: commands.Context, username: str) -> None:
        """Pause or resume alerts — for one streamer, or `all` for this server."""
        if username.strip().lower() in ("all", "*"):
            current = await self.config.guild(ctx.guild).enabled()
            await self.config.guild(ctx.guild).enabled.set(not current)
            await ctx.send(
                "✅ Alerts for this server are now **paused**."
                if current
                else "✅ Alerts for this server are **active** again."
            )
            return
        slug = clean_slug(username) or username.strip().lower()
        async with self.config.guild(ctx.guild).streamers() as streamers:
            if slug not in streamers:
                await ctx.send(f"**{slug}** is not monitored.")
                return
            muted = not bool(streamers[slug].get("muted"))
            streamers[slug]["muted"] = muted
        await ctx.send(
            f"✅ Alerts for **{slug}** are now **{'paused' if muted else 'active'}**."
        )

    @kickalert.command(name="interval")
    @checks.admin_or_permissions(manage_guild=True)
    async def kickalert_interval(self, ctx: commands.Context, seconds: int) -> None:
        """Set how often the API is polled (30-3600 seconds)."""
        if not MIN_INTERVAL <= seconds <= MAX_INTERVAL:
            await ctx.send(
                f"❌ Choose between {MIN_INTERVAL} and {MAX_INTERVAL} seconds."
            )
            return
        await self.config.guild(ctx.guild).check_interval.set(seconds)
        await ctx.send(f"✅ Interval set to **{seconds}s** for this server.")

    # -- clips -------------------------------------------------------------- #
    @kickalert.command(name="clips")
    @checks.admin_or_permissions(manage_guild=True)
    async def kickalert_clips(self, ctx: commands.Context, state: bool) -> None:
        """Turn clip announcements for this server on or off."""
        await self.config.guild(ctx.guild).clips_enabled.set(state)
        if not state:
            await ctx.send("Clip announcements are off.")
            return
        channel_id = await self.config.guild(ctx.guild).clips_channel_id()
        interval = await self.config.guild(ctx.guild).clips_interval()
        where = (
            f"<#{channel_id}>"
            if channel_id
            else "each streamer's alert channel (no clip channel set)"
        )
        await ctx.send(
            f"Clip announcements are on. New clips go to {where}, checked every "
            f"{interval}s.\n"
            "The first check per streamer only records what already exists, so "
            "nothing old gets posted."
        )

    @kickalert.command(name="clipchannel")
    @checks.admin_or_permissions(manage_guild=True)
    async def kickalert_clipchannel(
        self, ctx: commands.Context, channel: Optional[discord.TextChannel] = None
    ) -> None:
        """Where clip alerts go; omit the channel to fall back per streamer."""
        await self.config.guild(ctx.guild).clips_channel_id.set(
            channel.id if channel else None
        )
        if channel:
            await ctx.send(f"Clips will be posted in {channel.mention}.")
        else:
            await ctx.send(
                "Clip channel cleared - clips go to each streamer's alert channel, "
                "then to the default channel."
            )

    @kickalert.command(name="clipinterval")
    @checks.admin_or_permissions(manage_guild=True)
    async def kickalert_clipinterval(self, ctx: commands.Context, seconds: int) -> None:
        """How often clip lists are checked (60-3600 seconds)."""
        if not MIN_CLIPS_INTERVAL <= seconds <= MAX_CLIPS_INTERVAL:
            await ctx.send(
                f"Choose between {MIN_CLIPS_INTERVAL} and {MAX_CLIPS_INTERVAL} seconds."
            )
            return
        await self.config.guild(ctx.guild).clips_interval.set(seconds)
        await ctx.send(f"Clip check interval set to **{seconds}s**.")

    @kickalert.command(name="clipfiles")
    @checks.admin_or_permissions(manage_guild=True)
    async def kickalert_clipfiles(self, ctx: commands.Context, state: bool) -> None:
        """Post the clip itself as an MP4 under the announcement."""
        await self.config.guild(ctx.guild).clips_attach.set(state)
        if not state:
            await ctx.send("Clip files are off - announcements stay embed-only.")
            return
        if shutil.which("ffmpeg") is None:
            await ctx.send(
                "**ffmpeg is not installed on the bot host**, so clips cannot be "
                "converted to MP4. Install it and reload the cog."
            )
            return
        await ctx.send(
            "Clip files are on: every announced clip is converted to MP4 and "
            f"posted as a file (up to `{ctx.clean_prefix}kickalert clipmax` MiB, "
            "default 20). Clips over the limit are skipped."
        )

    @kickalert.command(name="clipmax")
    @checks.admin_or_permissions(manage_guild=True)
    async def kickalert_clipmax(self, ctx: commands.Context, megabytes: int) -> None:
        """Largest clip file to attach, in MiB (1-20)."""
        value = _clamp_clip_mb(megabytes)
        await self.config.guild(ctx.guild).clips_max_mb.set(value)
        note = "" if value == megabytes else f" (clamped from {megabytes})"
        await ctx.send(f"Clip files up to {value} MiB are attached{note}.")

    @kickalert.command(name="clipsset")
    @checks.admin_or_permissions(manage_guild=True)
    async def kickalert_clipsset(
        self, ctx: commands.Context, username: str, state: bool
    ) -> None:
        """Turn clip announcements for one streamer on or off."""
        slug = _clean_slug(username)
        async with self.config.guild(ctx.guild).streamers() as streamers:
            if slug not in streamers:
                await ctx.send(f"`{username}` is not being monitored.")
                return
            streamers[slug]["clips"] = state
        await ctx.send(f"Clips for `{slug}` are now {'on' if state else 'off'}.")

    @kickalert.command(name="clipscheck")
    @checks.admin_or_permissions(manage_guild=True)
    async def kickalert_clipscheck(self, ctx: commands.Context, username: str) -> None:
        """Show a channel's newest clips without announcing anything."""
        slug = _clean_slug(username)
        if not slug:
            await ctx.send("That is not a Kick slug.")
            return
        async with ctx.typing():
            try:
                clips = await self.api.get_clips(slug, 5)
            except KickRateLimited as exc:
                await ctx.send(f"kick.com is rate limiting right now: {exc}")
                return
            except KickError as exc:
                await ctx.send(f"The clip list could not be read: {exc}")
                return
        if not clips:
            await ctx.send(f"`{slug}` has no public clips.")
            return
        lines = []
        for clip in clips[:5]:
            created = _stamp(clip.created_at, "R") or "unknown"
            lines.append(
                f"**[{(clip.title or clip.id)[:60]}]({clip.url})** - "
                f"{created} - {clip.views or 0} views"
            )
        embed = discord.Embed(
            colour=discord.Colour(KICK_COLOR),
            title=f"Newest clips of {slug}",
            description="\n".join(lines),
            timestamp=datetime.now(
                EmbedSettings.from_guild_config(
                    await self.config.guild(ctx.guild).all()
                ).tz
            ),
        )
        embed.set_footer(text="Kick.com | nothing was posted")
        await ctx.send(embed=embed)

    @kickalert.command(name="events")
    @checks.admin_or_permissions(manage_guild=True)
    async def kickalert_events(self, ctx: commands.Context, state: bool) -> None:
        """Create a Discord event while a monitored streamer is live.

        The event carries the stream title, the category and the channel link,
        so it shows up in the server's **Events** tab while the stream runs and
        is removed when it ends. Needs the **Create Events** permission.
        """
        await self.config.guild(ctx.guild).events_enabled.set(state)
        if not state:
            await ctx.send(
                "✅ Discord events are **off**. An event that is still up gets "
                "removed on the next check."
            )
            return
        lines = [
            "✅ Discord events are **on**: every live stream of an enabled "
            "streamer gets a scheduled event, and it disappears when the stream "
            "ends."
        ]
        if not self._can_create_events(ctx.guild):
            lines.append(
                "⚠️ The bot is missing the **Create Events** permission in this "
                "server, so nothing is created until it has that permission."
            )
        await ctx.send("\n\n".join(lines))

    @kickalert.command(name="eventset")
    @checks.admin_or_permissions(manage_guild=True)
    async def kickalert_eventset(
        self, ctx: commands.Context, username: str, state: bool
    ) -> None:
        """Turn the Discord event of one streamer on or off.

        Only picks a streamer in or out of `[p]kickalert events true`.
        """
        slug = clean_slug(username) or username.strip().lower()
        async with self.config.guild(ctx.guild).streamers() as streamers:
            entry = streamers.get(slug)
            if not isinstance(entry, dict):
                await ctx.send(f"**{slug}** is not monitored.")
                return
            entry["events"] = state
        await ctx.send(
            f"✅ Discord events for **{slug}** are now "
            f"**{'on' if state else 'off'}**."
        )

    @kickalert.command(name="eventhours")
    @checks.admin_or_permissions(manage_guild=True)
    async def kickalert_eventhours(self, ctx: commands.Context, hours: int) -> None:
        """How long the event is scheduled for, in hours (1-12).

        Discord completes an external event at its end time; a stream that runs
        longer keeps pushing that time out, so this is the window on the card.
        """
        stamped = _clamp_event_hours(hours)
        await self.config.guild(ctx.guild).event_hours.set(stamped)
        note = (
            ""
            if stamped == hours
            else f" (clamped into {MIN_EVENT_HOURS}-{MAX_EVENT_HOURS} h)"
        )
        await ctx.send(f"✅ Discord events are scheduled for **{stamped} h**{note}.")

    @kickalert.command(name="eventname")
    @checks.admin_or_permissions(manage_guild=True)
    async def kickalert_eventname(self, ctx: commands.Context, *, template: str) -> None:
        """Template for the event title; `reset` restores the default.

        Placeholders: `{streamer}` `{title}` `{game}` `{category}` `{url}` —
        the result is cut to Discord's 100 characters.
        """
        if template.strip().lower() in ("reset", "default"):
            await self.config.guild(ctx.guild).event_name.set(None)
            await ctx.send(f"✅ Event title reset to `{DEFAULT_EVENT_NAME}`.")
            return
        cleaned = " ".join(template.split())
        if not cleaned:
            await ctx.send("❌ The template is empty.")
            return
        await self.config.guild(ctx.guild).event_name.set(
            cleaned[:EVENT_NAME_LIMIT]
        )
        await ctx.send(f"✅ Event titles now look like `{cleaned[:EVENT_NAME_LIMIT]}`.")

    @kickalert.command(name="eventdescription", aliases=["eventdesc"])
    @checks.admin_or_permissions(manage_guild=True)
    async def kickalert_eventdescription(
        self, ctx: commands.Context, *, template: str
    ) -> None:
        """Template for the event description; `reset` goes back to automatic.

        Automatic is the stream title followed by `category · channel link`.
        """
        if template.strip().lower() in ("reset", "default", "auto"):
            await self.config.guild(ctx.guild).event_description.set(None)
            await ctx.send("✅ Event descriptions are automatic again.")
            return
        cleaned = template.strip()
        await self.config.guild(ctx.guild).event_description.set(
            cleaned[:EVENT_DESCRIPTION_LIMIT]
        )
        await ctx.send(
            "✅ Event descriptions are now:\n" + cleaned[:EVENT_DESCRIPTION_LIMIT]
        )

    @kickalert.command(name="eventcover")
    @checks.admin_or_permissions(manage_guild=True)
    async def kickalert_eventcover(self, ctx: commands.Context, state: bool) -> None:
        """Use the stream thumbnail as the event's cover image."""
        await self.config.guild(ctx.guild).event_cover.set(state)
        await ctx.send(
            f"✅ Event cover images are **{'on' if state else 'off'}**."
        )

    @kickalert.command(name="eventoffline")
    @checks.admin_or_permissions(manage_guild=True)
    async def kickalert_eventoffline(self, ctx: commands.Context, mode: str) -> None:
        """What to do with the event when the stream ends: `delete` or `complete`.

        `delete` leaves nothing behind, `complete` keeps the event as a finished
        entry with its real end time.
        """
        choice = mode.strip().lower()
        if choice not in EVENT_OFFLINE_MODES:
            await ctx.send(
                "❌ Pick `delete` or `complete`.\n"
                "• `delete` — the event disappears when the stream ends\n"
                "• `complete` — it stays in the server's past events"
            )
            return
        await self.config.guild(ctx.guild).event_offline.set(choice)
        text = (
            "the event is deleted"
            if choice == "delete"
            else "the event is completed and stays in the past events"
        )
        await ctx.send(f"✅ When a stream ends, {text}.")

    @kickalert.command(name="eventtest")
    @checks.admin_or_permissions(manage_guild=True)
    async def kickalert_eventtest(self, ctx: commands.Context, username: str) -> None:
        """Create one event right now, to check the permission and the look.

        Run it while the streamer is live — an event for an offline channel is
        removed again at the next check.
        """
        slug = clean_slug(username) or username.strip().lower()
        streamers = await self.config.guild(ctx.guild).streamers() or {}
        if slug not in streamers:
            await ctx.send(f"**{slug}** is not monitored — add it first.")
            return
        async with ctx.typing():
            try:
                info = await self.api.get_channel(slug)
            except KickError as exc:
                await ctx.send(f"❌ Kick API problem: {exc}")
                return
            if info is None:
                await ctx.send(f"❌ No Kick channel called **{slug}**.")
                return
            data = await self.config.guild(ctx.guild).all()
            events = EventSettings.from_guild_config(data)
            state = {**STREAMER_DEFAULTS, **(streamers.get(slug) or {})}
            existing = await self._stored_event(ctx.guild, slug, state)
            if existing is not None and existing.status not in (
                discord.EventStatus.completed,
                discord.EventStatus.canceled,
            ):
                await ctx.send(
                    f"ℹ️ **{slug}** already has an event "
                    f"(`{existing.status.name}`): {existing.url}"
                )
                return
            action = await self._start_event(ctx.guild, slug, info, events)
            stored = (
                (await self.config.guild(ctx.guild).streamers()) or {}
            ).get(slug) or {}
            event_id = stored.get("event_id")
        if action != "event-started" or not event_id:
            if action == "event-no-permission":
                await ctx.send(
                    "❌ The bot is missing **Create Events** in this server, so it "
                    "cannot create a scheduled event."
                )
            else:
                await ctx.send(
                    f"❌ Could not create the event (`{action}`) — see the bot log."
                )
            return
        event = ctx.guild.get_scheduled_event(int(event_id))
        embed = discord.Embed(
            colour=discord.Colour(KICK_COLOR),
            title="Discord event created",
            description=(
                f"**{event.name if event else slug}**\n"
                f"{event.url if event else ''}"
            ),
            timestamp=datetime.now(timezone.utc),
        )
        if event is not None:
            embed.add_field(name="Status", value=event.status.name, inline=True)
            embed.add_field(
                name="Start", value=_stamp(event.start_time, "f") or "?", inline=True
            )
            embed.add_field(
                name="End", value=_stamp(event.end_time, "f") or "?", inline=True
            )
        embed.set_footer(
            text="An event for an offline streamer is removed at the next check."
        )
        await ctx.send(embed=embed)

    @kickalert.command(name="eventclear", aliases=["eventdelete"])
    @checks.admin_or_permissions(manage_guild=True)
    async def kickalert_eventclear(self, ctx: commands.Context, username: str) -> None:
        """Delete the Discord event this cog created for a streamer."""
        slug = clean_slug(username) or username.strip().lower()
        streamers = await self.config.guild(ctx.guild).streamers() or {}
        entry = streamers.get(slug)
        if not isinstance(entry, dict):
            await ctx.send(f"**{slug}** is not monitored.")
            return
        async with ctx.typing():
            removed = await self._drop_event(ctx.guild, slug, entry)
            await self._forget_event(ctx.guild, slug)
        if removed:
            await ctx.send(f"✅ The Discord event of **{slug}** is gone.")
        else:
            await ctx.send(f"ℹ️ **{slug}** has no event stored.")

    @kickalert.command(name="style")
    @checks.admin_or_permissions(manage_guild=True)
    async def kickalert_style(self, ctx: commands.Context, style: str) -> None:
        """Set the alert look: `detailed` or `minimal`."""
        style = style.strip().lower()
        if style not in ("detailed", "minimal"):
            await ctx.send("❌ Use `detailed` or `minimal`.")
            return
        await self.config.guild(ctx.guild).embed_style.set(style)
        await ctx.send(f"✅ Alert style is now **{style}**.")

    @kickalert.command(name="autodelete")
    @checks.admin_or_permissions(manage_guild=True)
    async def kickalert_autodelete(self, ctx: commands.Context, toggle: bool) -> None:
        """Delete the live alert when the stream ends instead of editing it.

        Only affects `offlinepost edit`; in `new` mode the live alert is replaced
        by the summary post anyway.
        """
        await self.config.guild(ctx.guild).auto_delete.set(toggle)
        await ctx.send(
            "✅ Alerts are **deleted** when the stream ends."
            if toggle
            else "✅ Alerts are **edited** into a stream-ended message."
        )

    @kickalert.command(name="offlinepost", aliases=["endpost", "summary"])
    @checks.admin_or_permissions(manage_guild=True)
    async def kickalert_offlinepost(
        self, ctx: commands.Context, mode: Optional[str] = None
    ) -> None:
        """What happens when a stream ends: `new` summary post or `edit` the alert.

        `new` removes the live alert and posts a fresh embed with stream time,
        peak viewers and average viewers. `edit` turns the live alert into the
        end-of-stream embed, which carries the same statistics.
        """
        if mode is None:
            current = str(await self.config.guild(ctx.guild).offline_post() or "new")
            await ctx.send(
                f"Stream end posts: **{current}** — change it with "
                f"`{ctx.clean_prefix}kickalert offlinepost new|edit`."
            )
            return
        choice = mode.strip().lower()
        if choice not in ("new", "edit"):
            await ctx.send("❌ Use `new` or `edit`.")
            return
        await self.config.guild(ctx.guild).offline_post.set(choice)
        await ctx.send(
            "✅ When a stream ends I **post a new summary embed** and remove the "
            "live alert."
            if choice == "new"
            else "✅ When a stream ends I **edit the live alert** into the "
            "end-of-stream embed."
        )

    @kickalert.command(name="editlive")
    @checks.admin_or_permissions(manage_guild=True)
    async def kickalert_editlive(self, ctx: commands.Context, toggle: bool) -> None:
        """Keep the live alert embed updated while the stream runs."""
        await self.config.guild(ctx.guild).edit_live_alerts.set(toggle)
        await ctx.send(
            "✅ Live alerts are updated while the stream runs."
            if toggle
            else "✅ Live alerts are posted once and left alone."
        )

    @kickalert.command(name="timezone", aliases=["tz"])
    @checks.admin_or_permissions(manage_guild=True)
    async def kickalert_timezone(self, ctx: commands.Context, offset: int) -> None:
        """Set the offset for embed timestamps (-12 to 14).

        Times inside the embeds are Discord timestamps and always render in the
        viewer's own timezone; this offset only affects the embed's own timestamp.
        """
        if not -12 <= offset <= 14:
            await ctx.send("❌ Choose an offset between -12 and +14.")
            return
        await self.config.guild(ctx.guild).timezone_offset.set(offset)
        sign = "+" if offset >= 0 else "-"
        await ctx.send(f"✅ Timezone set to **UTC{sign}{abs(offset)}**.")

    @kickalert.command(name="test")
    @checks.admin_or_permissions(manage_guild=True)
    async def kickalert_test(self, ctx: commands.Context, username: str) -> None:
        """Post a sample alert so the current settings can be judged."""
        slug = clean_slug(username)
        if not slug:
            await ctx.send(f"❌ `{username}` is not a valid Kick channel name.")
            return
        async with ctx.typing():
            try:
                info = await self.api.get_channel(slug)
            except KickError as exc:
                await ctx.send(f"❌ Kick API problem: {exc}")
                return
        if info is None:
            await ctx.send(f"❌ No Kick channel called **{slug}**.")
            return
        if not info.is_live:
            info = _as_demo_stream(info)
        settings = EmbedSettings.from_guild_config(
            await self.config.guild(ctx.guild).all()
        )
        await ctx.send(
            "**Test announcement** (sample data, no real stream was announced):",
            embed=build_live_embed(info, settings),
        )

    @kickalert.command(name="check", aliases=["status"])
    @checks.admin_or_permissions(manage_guild=True)
    async def kickalert_check(self, ctx: commands.Context, username: str) -> None:
        """Show whether a streamer is live right now."""
        slug = clean_slug(username)
        if not slug:
            await ctx.send(f"❌ `{username}` is not a valid Kick channel name.")
            return
        async with ctx.typing():
            try:
                info = await self.api.get_channel(slug)
            except KickError as exc:
                await ctx.send(f"❌ Kick API problem: {exc}")
                return
        if info is None:
            await ctx.send(f"❌ No Kick channel called **{slug}**.")
            return
        settings = EmbedSettings.from_guild_config(
            await self.config.guild(ctx.guild).all()
        )
        await ctx.send(embed=build_status_embed(info, settings))

    @kickalert.command(name="debug")
    @checks.admin_or_permissions(manage_guild=True)
    async def kickalert_debug(self, ctx: commands.Context, username: str) -> None:
        """Show the raw API payload for one channel (troubleshooting)."""
        slug = clean_slug(username)
        if not slug:
            await ctx.send(f"❌ `{username}` is not a valid Kick channel name.")
            return
        async with ctx.typing():
            try:
                info = await self.api.get_channel(slug)
            except KickError as exc:
                await ctx.send(f"❌ Kick API problem: {exc}")
                return
        await ctx.send(embed=build_debug_embed(slug, info))

    @kickalert.command(name="settings")
    @checks.admin_or_permissions(manage_guild=True)
    async def kickalert_settings(self, ctx: commands.Context) -> None:
        """Show the current configuration."""
        data = await self.config.guild(ctx.guild).all()
        settings = EmbedSettings.from_guild_config(data)
        streamers = data.get("streamers") or {}
        channel = data.get("global_channel_id")
        role = data.get("global_ping_role_id")
        live = sum(1 for entry in streamers.values() if (entry or {}).get("is_live"))
        muted = sum(1 for entry in streamers.values() if (entry or {}).get("muted"))
        embed = discord.Embed(
            colour=discord.Colour(KICK_COLOR),
            title="KickAlerts settings",
            timestamp=datetime.now(settings.tz),
        )
        embed.add_field(
            name="API",
            value="credentials set" if await self._credentials_set() else "**not set**",
            inline=True,
        )
        embed.add_field(
            name="Alerts",
            value="enabled" if data.get("enabled", True) else "**paused**",
            inline=True,
        )
        embed.add_field(
            name="Streamers", value=f"{len(streamers)} ({live} live)", inline=True
        )
        embed.add_field(
            name="Default channel",
            value=f"<#{channel}>" if channel else "not set",
            inline=True,
        )
        embed.add_field(
            name="Default ping role",
            value=ping_mention(ctx.guild, role) or "none",
            inline=True,
        )
        embed.add_field(
            name="Interval", value=f"{data.get('check_interval', 60)}s", inline=True
        )
        clips_channel = data.get("clips_channel_id")
        if data.get("clips_enabled"):
            clips_value = (
                f"every {_clamp_clips_interval(data.get('clips_interval'))}s -> "
                f"{f'<#{clips_channel}>' if clips_channel else 'per streamer'}"
            )
        else:
            clips_value = "off"
        embed.add_field(name="Clips", value=clips_value, inline=True)
        if data.get("clips_enabled"):
            embed.add_field(
                name="Clip files",
                value=(
                    f"MP4 up to {_clamp_clip_mb(data.get('clips_max_mb'))} MiB"
                    if data.get("clips_attach", True)
                    else "off"
                ),
                inline=True,
            )
        if data.get("events_enabled"):
            events_value = (
                f"{_clamp_event_hours(data.get('event_hours'))} h · "
                f"{str(data.get('event_offline') or 'delete')}"
            )
            if not self._can_create_events(ctx.guild):
                events_value += " · **no Create Events**"
        else:
            events_value = "off"
        embed.add_field(name="Events", value=events_value, inline=True)
        embed.add_field(
            name="Style", value=str(data.get("embed_style", "detailed")), inline=True
        )
        embed.add_field(
            name="Viewers / category",
            value=(
                f"{data.get('show_viewer_count', True)} / "
                f"{data.get('show_category', True)}"
            ),
            inline=True,
        )
        embed.add_field(
            name="Stream ends",
            value=(
                "new summary post"
                if str(data.get("offline_post") or "new") == "new"
                else (
                    "delete the alert" if data.get("auto_delete") else "edit the alert"
                )
            ),
            inline=True,
        )
        embed.add_field(
            name="Live updates",
            value=(
                f"every {_clamp_update_seconds(data.get('live_update_seconds'))}s"
                if data.get("edit_live_alerts", True)
                else "off"
            ),
            inline=True,
        )
        embed.add_field(
            name="Timezone",
            value=(
                f"UTC{'+' if settings.timezone_offset >= 0 else ''}"
                f"{settings.timezone_offset}"
            ),
            inline=True,
        )
        if muted:
            embed.add_field(name="Paused streamers", value=str(muted), inline=True)
        if self._last_poll:
            embed.add_field(
                name="Last poll",
                value=(
                    f"{_stamp(self._last_poll)} · "
                    f"{self._last_result.get('streamers', 0)} checked"
                ),
                inline=True,
            )
        await ctx.send(embed=embed)

    @kickalert.command(name="toggleviewers")
    @checks.admin_or_permissions(manage_guild=True)
    async def kickalert_toggleviewers(
        self, ctx: commands.Context, toggle: bool
    ) -> None:
        """Show or hide the viewer count in the embeds."""
        await self.config.guild(ctx.guild).show_viewer_count.set(toggle)
        await ctx.send(f"✅ Viewer count is now **{'shown' if toggle else 'hidden'}**.")

    @kickalert.command(name="togglecategory")
    @checks.admin_or_permissions(manage_guild=True)
    async def kickalert_togglecategory(
        self, ctx: commands.Context, toggle: bool
    ) -> None:
        """Show or hide the category in the embeds."""
        await self.config.guild(ctx.guild).show_category.set(toggle)
        await ctx.send(f"✅ Category is now **{'shown' if toggle else 'hidden'}**.")

    @kickalert.command(name="clear")
    @checks.admin_or_permissions(manage_guild=True)
    async def kickalert_clear(
        self, ctx: commands.Context, confirm: bool = False
    ) -> None:
        """Remove every streamer and reset all settings for this server."""
        if not confirm:
            await ctx.send(
                "This deletes **all** streamers and settings of this server. Run "
                f"`{ctx.clean_prefix}kickalert clear True` to confirm."
            )
            return
        streamers = await self.config.guild(ctx.guild).streamers() or {}
        async with ctx.typing():
            for slug, entry in streamers.items():
                if isinstance(entry, dict):
                    await self._drop_event(ctx.guild, slug, entry)
        await self.config.guild(ctx.guild).clear()
        await ctx.send("✅ All KickAlerts settings and streamers were cleared.")

    @kickalert.command(name="force", aliases=["forcecheck"])
    @checks.admin_or_permissions(manage_guild=True)
    async def kickalert_force(self, ctx: commands.Context) -> None:
        """Check every monitored streamer right now."""
        streamers = await self.config.guild(ctx.guild).streamers() or {}
        if not streamers:
            await ctx.send("No streamers to check.")
            return
        async with ctx.typing():
            try:
                channels = await self.api.get_channels(streamers.keys())
            except KickError as exc:
                await ctx.send(f"❌ Kick API problem: {exc}")
                return
            data = await self.config.guild(ctx.guild).all()
            counts = await self._poll_guild(ctx.guild, data, channels)
            self._last_result = counts
            self._last_poll = datetime.now(timezone.utc)
        missing = [slug for slug in streamers if slug not in channels]
        note = f"\n⚠️ No API data for: {', '.join(missing)}" if missing else ""
        await ctx.send(
            f"Checked **{len(channels)}** channel(s) — **{counts['live']}** live, "
            f"**{counts['alerts']}** alert(s) updated.{note}"
        )

    # -- listeners ---------------------------------------------------------- #
    @commands.Cog.listener()
    async def on_red_api_tokens_update(
        self, service_name: str, api_tokens: Mapping[str, str]
    ) -> None:
        """Pick up credentials set with ``[p]set api`` without a reload."""
        if service_name != SHARED_API_SERVICE:
            return
        self.api.set_credentials(
            api_tokens.get("client_id"), api_tokens.get("client_secret")
        )
        log.info("KickAlerts: picked up updated Kick credentials")

    @commands.Cog.listener()
    async def on_guild_channel_delete(self, channel: discord.abc.GuildChannel) -> None:
        """Drop references to a channel that was deleted."""
        guild = getattr(channel, "guild", None)
        if guild is None:
            return
        if await self.config.guild(guild).global_channel_id() == channel.id:
            await self.config.guild(guild).global_channel_id.set(None)
        if await self.config.guild(guild).clips_channel_id() == channel.id:
            await self.config.guild(guild).clips_channel_id.set(None)
        async with self.config.guild(guild).streamers() as streamers:
            for slug, entry in streamers.items():
                if isinstance(entry, dict) and entry.get("channel_id") == channel.id:
                    entry["channel_id"] = None
                    log.debug(
                        "KickAlerts: %s lost its channel in guild %s", slug, guild.id
                    )


__all__ = [
    "KickAlerts",
    "KickAPI",
    "KickAuthError",
    "KickError",
    "KickRateLimited",
    "StreamInfo",
    "StreamSummary",
    "EmbedSettings",
    "accumulate_viewers",
    "build_live_embed",
    "build_offline_embed",
    "build_status_embed",
    "build_debug_embed",
    "build_summary_embed",
    "clean_slug",
    "format_uptime",
    "parse_kick_time",
    "ping_mention",
    "render_template",
    "summarise_stream",
]


async def setup(bot: Red) -> None:
    """Load the cog."""
    await bot.add_cog(KickAlerts(bot))
