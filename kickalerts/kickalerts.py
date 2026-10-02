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
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

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
    "timezone_offset": 1,
    "enabled": True,
    "edit_live_alerts": True,
    "live_update_seconds": 300,
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


def clean_slug(value: str) -> str:
    """Normalise user input into a Kick slug; accepts a channel URL too."""
    return _clean_slug(value)


def _clean_slug(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    slug = value.strip().lower()
    if "kick.com" in slug:
        slug = slug.split("kick.com", 1)[1]
    slug = slug.strip("/").split("/")[0].split("?")[0].strip().lstrip("@")
    return slug if SLUG_RE.match(slug) else ""


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


def _clamp_update_seconds(seconds: Any) -> int:
    """Clamp the live-embed refresh interval (60s .. 3600s)."""
    value = _as_int(seconds) or DEFAULT_GUILD["live_update_seconds"]
    return max(60, min(3600, value))


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
        assert self._session is not None  # just opened
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
    info: StreamInfo, settings: EmbedSettings, duration: Optional[timedelta] = None
) -> discord.Embed:
    """The "stream ended" embed that replaces the live alert."""
    lines = [f"**{info.slug}** is offline."]
    formatted = format_uptime(duration)
    if formatted:
        lines.append(f"Was live for {formatted}.")
    lines.append(f"[Visit channel]({info.url})")
    embed = discord.Embed(
        colour=discord.Colour(OFFLINE_COLOR),
        description="\n".join(lines),
        timestamp=datetime.now(settings.tz),
    )
    embed.set_author(name=f"{info.slug} is offline", url=info.url)
    if info.banner:
        embed.set_thumbnail(url=info.banner)
    embed.set_footer(text="Kick.com | Stream ended")
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

    __version__ = "3.1.0"

    def __init__(self, bot: Red) -> None:
        self.bot = bot
        self.config = Config.get_conf(
            self, identifier=7274927492, force_registration=True
        )
        self.config.register_global(**DEFAULT_GLOBAL)
        self.config.register_guild(**DEFAULT_GUILD)
        self.api = KickAPI()
        self._watch_task: Optional[asyncio.Task] = None
        self._last_poll: Optional[datetime] = None
        self._last_result: Dict[str, int] = {}

    # -- lifecycle ---------------------------------------------------------- #
    async def cog_load(self) -> None:
        """Load credentials, migrate legacy ones and start the watcher."""
        await self._load_credentials(migrate=True)
        self._watch_task = asyncio.create_task(self._watch_loop())

    async def cog_unload(self) -> None:
        """Stop the watcher and close the HTTP session."""
        if self._watch_task is not None:
            self._watch_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._watch_task
            self._watch_task = None
        await self.api.close()

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
        update_seconds = _clamp_update_seconds(data.get("live_update_seconds"))
        global_channel = data.get("global_channel_id")
        global_role = data.get("global_ping_role_id")
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
                auto_delete=auto_delete,
                edit_live=edit_live,
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
        auto_delete: bool,
        edit_live: bool,
        update_seconds: int,
        global_channel: Optional[int],
        global_role: Optional[int],
    ) -> str:
        """Apply one streamer's new state and return what happened."""
        state = {**STREAMER_DEFAULTS, **(entry or {})}
        was_live = bool(state.get("is_live"))
        previous_key = state.get("stream_key")
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
            delivered = await self._announce(
                guild, slug, info, settings, channel, state, global_role
            )
            return "announced" if delivered else "no-permission"

        if not info.is_live and was_live:
            await self._end_alert(
                guild, slug, info, settings, channel, state, auto_delete=auto_delete
            )
            return "ended"

        if info.is_live:
            if edit_live and self._needs_refresh(state, info, update_seconds):
                edited = await self._refresh_alert(
                    guild, slug, info, settings, channel, state
                )
                return "updated" if edited else "unchanged"
            await self._store_state(
                guild, slug, is_live=True, stream_key=info.stream_key
            )
            return "unchanged"

        await self._store_state(guild, slug, is_live=False, stream_key=None)
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
        if role_id:
            role = guild.get_role(int(role_id))
            if role is not None:
                lines.append(role.mention)
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
    ) -> bool:
        """Post the live alert. Returns ``False`` when the bot may not post."""
        permissions = channel.permissions_for(guild.me)
        if not (permissions.send_messages and permissions.embed_links):
            log.warning(
                "KickAlerts: missing Send Messages/Embed Links in #%s (guild %s)",
                channel,
                guild.id,
            )
            return False
        try:
            message = await channel.send(
                content=self._alert_content(guild, info, state, global_role),
                embed=build_live_embed(info, settings),
                allowed_mentions=discord.AllowedMentions(roles=True, everyone=False),
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
    ) -> bool:
        """Edit the existing alert message with fresh numbers."""
        message_id = state.get("alert_message_id")
        if not message_id:
            return False
        try:
            message = await channel.fetch_message(int(message_id))
            await message.edit(embed=build_live_embed(info, settings))
        except discord.NotFound:
            # Someone deleted the alert; stop trying to edit it.
            await self._store_state(guild, slug, alert_message_id=None, last_edit=None)
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
    ) -> None:
        """Delete the alert or turn it into the offline embed."""
        message_id = state.get("alert_message_id")
        started = parse_kick_time(state.get("started_at"))
        duration = (datetime.now(timezone.utc) - started) if started else None
        if message_id:
            remove = state.get("delete_after_offline")
            remove = auto_delete if remove is None else bool(remove)
            try:
                message = await channel.fetch_message(int(message_id))
                if remove:
                    await message.delete()
                else:
                    await message.edit(
                        content=None,
                        embed=build_offline_embed(info, settings, duration),
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
        )

    async def _store_state(
        self, guild: discord.Guild, slug: str, **values: Any
    ) -> None:
        """Merge new state into one streamer entry."""
        async with self.config.guild(guild).streamers() as streamers:
            entry = streamers.get(slug)
            if not isinstance(entry, dict):
                return
            entry.update(values)

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
            del streamers[slug]
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
                ping = f"<@&{role}>" if role else "none"
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
        if username is None:
            await self.config.guild(ctx.guild).global_ping_role_id.set(role.id)
            await ctx.send(f"✅ Default ping role is now {role.mention}.")
            return
        slug = clean_slug(username) or username.strip().lower()
        async with self.config.guild(ctx.guild).streamers() as streamers:
            if slug not in streamers:
                await ctx.send(f"**{slug}** is not monitored.")
                return
            streamers[slug]["ping_role_id"] = role.id
        await ctx.send(f"✅ **{slug}** now pings {role.mention}.")

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
        """Delete the alert when the stream ends instead of editing it."""
        await self.config.guild(ctx.guild).auto_delete.set(toggle)
        await ctx.send(
            "✅ Alerts are **deleted** when the stream ends."
            if toggle
            else "✅ Alerts are **edited** into a stream-ended message."
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
            value=f"<@&{role}>" if role else "none",
            inline=True,
        )
        embed.add_field(
            name="Interval", value=f"{data.get('check_interval', 60)}s", inline=True
        )
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
            value="delete the alert" if data.get("auto_delete") else "edit the alert",
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
    "EmbedSettings",
    "build_live_embed",
    "build_offline_embed",
    "build_status_embed",
    "build_debug_embed",
    "clean_slug",
    "format_uptime",
    "parse_kick_time",
    "render_template",
]


async def setup(bot: Red) -> None:
    """Load the cog."""
    await bot.add_cog(KickAlerts(bot))
