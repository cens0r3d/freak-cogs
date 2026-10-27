"""TikTok livestream and new-content announcements for Red-DiscordBot.

TikTok ships no public API for watching a creator, so this cog talks to two
endpoints of the public web frontend that answer without a login, a session
cookie or a request signature:

* ``/api-live/user/room/`` - the live room of one handle. ``data.user.status`` is
  ``2`` while the creator is live and ``4`` otherwise, next to the room title, the
  broadcast's start time, its cover image and the current viewer count.
* ``/embed/@handle`` - the profile's embed page. It carries a ``videoList`` array
  with the newest clips (id, description, cover, play count). That order is not
  chronological, because TikTok puts a pinned clip first, so new content is
  detected by comparing ids and never by looking at position zero.

Both are polled with jitter and bounded concurrency. A ``403``/``429`` from
TikTok parks all requests for a growing back-off window instead of hammering the
endpoint, and every state change is stored per guild in Red's Config.
"""

from __future__ import annotations

import asyncio
import contextlib
import io
import json
import logging
import random
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

import aiohttp
import discord
from redbot.core import Config, checks, commands
from redbot.core.bot import Red
from redbot.core.utils import bounded_gather
from redbot.core.utils.chat_formatting import humanize_list

log = logging.getLogger("red.freak_cogs.tiktokwatch")

LIVE_API = "https://www.tiktok.com/api-live/user/room/"
CONTENT_URL = "https://www.tiktok.com/embed/@{handle}"
PROFILE_URL = "https://www.tiktok.com/@{handle}"
LIVE_URL = "https://www.tiktok.com/@{handle}/live"
VIDEO_URL = "https://www.tiktok.com/@{handle}/video/{video_id}"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

#: Mobile app endpoint: the one place that hands out clip details (and the
#: MP4 URLs) without a request signature.
MOBILE_FEED = "https://api22-normal-c-alisg.tiktokv.com/aweme/v1/feed/"
MOBILE_UA = (
    "Mozilla/5.0 (Linux; Android 10; K) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/122.0.0.0 Mobile Safari/537.36"
)
MOBILE_PARAMS = {
    "region": "US",
    "carrier_region": "US",
    "iid": "7318518857994389254",
    "device_id": "7318517321748022790",
    "channel": "googleplay",
    "app_name": "musical_ly",
    "version_code": "300904",
    "device_platform": "android",
    "device_type": "ASUS_Z01QD",
    "os_version": "9",
}
#: Discord's own ceiling for a bot upload is the guild's filesize_limit; this is
#: the sane default and can be lowered per guild.
DEFAULT_VIDEO_MAX_MB = 24

TIKTOK_COLOR = 0xFE2C55
CONTENT_COLOR = 0x25F4EE
OFFLINE_COLOR = 0x808080
DEBUG_COLOR = 0xFFAA00

#: TikTok reports this status code for a handle that does not exist.
STATUS_USER_NOT_FOUND = 19881007
#: ``data.user.status`` while the creator is live.
STATUS_LIVE = 2

#: Poll interval bounds in seconds.
MIN_LIVE_INTERVAL = 30
MAX_LIVE_INTERVAL = 3600
MIN_CONTENT_INTERVAL = 120
MAX_CONTENT_INTERVAL = 7200
#: How many clips are remembered per account, so an old clip never re-announces.
SEEN_VIDEO_LIMIT = 30
#: Concurrent requests in one sweep. TikTok block pages appear above this.
REQUEST_CONCURRENCY = 4
#: Back-off window grows with consecutive blocks, capped here.
MAX_BLOCK_SECONDS = 900
#: Placeholders a custom message may use.
TEMPLATE_FIELDS = ("handle", "title", "url", "viewers", "started")

DEFAULT_GLOBAL: Dict[str, Any] = {
    "blocked_until": 0.0,
    "block_reason": None,
    "fail_streak": 0,
}

DEFAULT_GUILD: Dict[str, Any] = {
    "accounts": {},
    "check_interval": 90,
    "content_interval": 600,
    "announce_live": True,
    "announce_content": True,
    "announce_videos": True,
    "video_max_mb": DEFAULT_VIDEO_MAX_MB,
    "announce_existing": False,
    "show_viewers": True,
    "hour_format": "local",
    "timezone_offset": 1,
    "offline_post": "new",
    "edit_live_alerts": True,
    "live_update_seconds": 300,
    "enabled": True,
}

#: Keys every account entry carries; missing ones fall back to these.
ACCOUNT_DEFAULTS: Dict[str, Any] = {
    "channel_id": None,
    "ping_role_id": None,
    "custom_message": None,
    "muted": False,
    "missing": False,
    "is_live": False,
    "stream_id": None,
    "stream_key": None,
    "announced_at": None,
    "room_id": None,
    "started_at": None,
    "last_title": None,
    "last_edit": None,
    "alert_message_id": None,
    "peak_viewers": 0,
    "seen_videos": [],
    "primed": False,
    "last_video_id": None,
}

HANDLE_RE = re.compile(r"^@?([A-Za-z0-9_.]{1,24})$")
VIDEO_ID_RE = re.compile(r"/@[A-Za-z0-9_.]+/video/(\d+)")
TEMPLATE_RE = re.compile(r"\{(\w+)\}")
TIKTOK_SPAN = re.compile(r"<[^>]+>")


# --------------------------------------------------------------------------- #
# errors
# --------------------------------------------------------------------------- #
class TikTokError(Exception):
    """Base class for a TikTok endpoint problem."""


class TikTokBlocked(TikTokError):
    """TikTok answered 403/429; the request must be backed off."""

    def __init__(self, message: str, retry_after: float = 60.0) -> None:
        super().__init__(message)
        self.retry_after = retry_after


class TikTokNotFound(TikTokError):
    """The handle does not exist (or cannot be watched)."""


# --------------------------------------------------------------------------- #
# small helpers
# --------------------------------------------------------------------------- #
def _as_int(value: Any) -> Optional[int]:
    """Best-effort int conversion that survives ``None`` and junk."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _as_text(value: Any) -> Optional[str]:
    """A stripped string, or ``None`` when there is nothing to show."""
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def clean_handle(value: Any) -> Optional[str]:
    """Normalise ``@name``, profile URLs and pasted links to a bare handle."""
    text = str(value or "").strip()
    if not text:
        return None
    text = text.split("?")[0].split("#")[0].rstrip("/")
    if "tiktok.com" in text:
        part = text.split("tiktok.com/", 1)[-1]
        part = part.lstrip("@").split("/")[0]
        text = part
    match = HANDLE_RE.match(text.lstrip("@"))
    if not match:
        return None
    return match.group(1)


def _clamp(seconds: Any, low: int, high: int, fallback: int) -> int:
    """Clamp a stored interval into ``[low, high]``."""
    value = _as_int(seconds)
    if value is None:
        return fallback
    return max(low, min(high, value))


def format_duration(delta: Optional[timedelta]) -> Optional[str]:
    """``2 h 15 min`` style duration, or ``None``."""
    if delta is None:
        return None
    total = int(max(0, delta.total_seconds()))
    if total < 60:
        return f"{total} s"
    hours, rest = divmod(total, 3600)
    minutes = rest // 60
    if hours and minutes:
        return f"{hours} h {minutes} min"
    if hours:
        return f"{hours} h"
    return f"{minutes} min"


def format_start(
    moment: Optional[datetime], tz: timezone, style: str = "local"
) -> Optional[str]:
    """Format a start time for a reader in the guild's timezone.

    Discord renders ``<t:...>`` in the client's locale, so a 24 h clock cannot be
    produced with markup - the local path formats server-side and prefixes the
    date whenever the moment is not today.
    """
    if moment is None:
        return None
    if style == "relative":
        return f"<t:{int(moment.timestamp())}:R>"
    local = moment.astimezone(tz)
    if local.date() == datetime.now(tz).date():
        return local.strftime("%H:%M")
    return local.strftime("%d.%m. %H:%M")


def _tz_from(offset: Any) -> timezone:
    """A fixed-offset timezone from a stored hour offset."""
    hours = _as_int(offset) or 0
    return timezone(timedelta(hours=max(-12, min(14, hours))))


def ping_mention(guild: discord.Guild, role_id: Any) -> Optional[str]:
    """Mention text for a stored role id, safe for the ``@everyone`` role.

    ``Role.mention`` is always ``<@&id>``, and the default role's id *is* the
    guild id, so that raw form renders as ``@@everyone`` and pings nobody.
    Comparing the ids directly needs no role object and survives a cache miss.
    """
    identifier = _as_int(role_id)
    if not identifier:
        return None
    if identifier == guild.id:
        return "@everyone"
    role = guild.get_role(identifier)
    return role.mention if role is not None else None


def pings_everyone(guild: discord.Guild, role_id: Any) -> bool:
    """Whether the stored ping role id is the guild's default role."""
    return _as_int(role_id) == guild.id


def render_template(template: str, values: Dict[str, Any]) -> str:
    """Fill ``{placeholder}`` in a custom message, leaving unknown ones alone."""

    def replace(match: "re.Match[str]") -> str:
        key = match.group(1)
        if key not in values:
            return match.group(0)
        value = values[key]
        return "" if value is None else str(value)

    return TEMPLATE_RE.sub(replace, template)


def _json_slice_at(text: str, index: int) -> Optional[str]:
    """Slice a JSON array/object out of a larger document, quote and nest aware."""
    depth = 0
    in_string = False
    escaped = False
    for position in range(index, len(text)):
        char = text[position]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char in "[{":
            depth += 1
        elif char in "]}":
            depth -= 1
            if depth == 0:
                return text[index : position + 1]
    return None


# --------------------------------------------------------------------------- #
# payload models
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class LiveInfo:
    """What one live-room lookup said about a handle."""

    handle: str
    is_live: bool
    title: Optional[str] = None
    cover_url: Optional[str] = None
    started_at: Optional[datetime] = None
    #: ``None`` when TikTok sent a stub room and withheld the counters.
    viewers: Optional[int] = None
    avatar_url: Optional[str] = None
    stream_id: Optional[int] = None
    room_id: Optional[int] = None
    raw_status: Optional[int] = None

    @property
    def url(self) -> str:
        """The public live URL of the handle."""
        return LIVE_URL.format(handle=self.handle)

    @property
    def stream_key(self) -> Optional[str]:
        """A key that identifies this broadcast, from whatever TikTok sent.

        Not every live room carries ``streamId``: some payloads ship a sparse
        ``liveRoom`` with only the viewer count, and a missing id used to look
        like a brand-new broadcast on every poll - one alert per interval. The
        start time and the current room id are equally per-broadcast, so the key
        falls back through them.
        """
        if self.stream_id:
            return f"stream:{self.stream_id}"
        if self.started_at is not None:
            return f"start:{int(self.started_at.timestamp())}"
        if self.room_id:
            return f"room:{self.room_id}"
        return None

    @property
    def uptime(self) -> Optional[timedelta]:
        """How long the broadcast has been running."""
        if self.started_at is None:
            return None
        return datetime.now(timezone.utc) - self.started_at


@dataclass(frozen=True)
class VideoInfo:
    """One clip from a profile's embed page."""

    video_id: str
    desc: Optional[str] = None
    cover_url: Optional[str] = None
    play_count: Optional[str] = None

    def url(self, handle: str) -> str:
        """The public URL of this clip."""
        return VIDEO_URL.format(handle=handle, video_id=self.video_id)


@dataclass(frozen=True)
class ClipPage:
    """What one profile embed page carried: clips plus the profile's own face."""

    videos: List[VideoInfo] = field(default_factory=list)
    avatar_url: Optional[str] = None
    nickname: Optional[str] = None


@dataclass(frozen=True)
class VideoStats:
    """Clip details only the mobile app endpoint hands out."""

    created_at: Optional[datetime] = None
    plays: Optional[int] = None
    likes: Optional[int] = None
    comments: Optional[int] = None
    shares: Optional[int] = None
    duration_s: Optional[int] = None
    play_url: Optional[str] = None
    download_url: Optional[str] = None
    size_bytes: Optional[int] = None


def _pick_addr(addr: Any) -> Tuple[Optional[str], Optional[int]]:
    """First URL and declared size of a TikTok ``play_addr`` style object."""
    if not isinstance(addr, dict):
        return None, None
    urls = [url for url in (addr.get("url_list") or []) if isinstance(url, str) and url]
    size = _as_int(addr.get("data_size"))
    return (urls[0] if urls else None, size)


def parse_video_stats(video_id: str, payload: Any) -> Optional[VideoStats]:
    """Read one clip's details out of an ``aweme/v1/feed`` answer."""
    if not isinstance(payload, dict):
        return None
    items = payload.get("aweme_list")
    if not isinstance(items, list):
        return None
    item: Dict[str, Any] = {}
    for candidate in items:
        if not isinstance(candidate, dict):
            continue
        if str(candidate.get("aweme_id") or "") == str(video_id):
            item = candidate
            break
    if not item:
        return None
    video_raw = item.get("video")
    video: Dict[str, Any] = video_raw if isinstance(video_raw, dict) else {}
    stats_raw = item.get("statistics")
    stats: Dict[str, Any] = stats_raw if isinstance(stats_raw, dict) else {}
    play_url, size = _pick_addr(video.get("play_addr"))
    if play_url is None:
        play_url, size = _pick_addr(video.get("play_addr_h264"))
    download_url, download_size = _pick_addr(video.get("download_addr"))
    created = None
    created_raw = _as_int(item.get("create_time"))
    if created_raw:
        try:
            created = datetime.fromtimestamp(created_raw, tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            created = None
    duration_ms = _as_int(video.get("duration"))
    return VideoStats(
        created_at=created,
        plays=_as_int(stats.get("play_count")),
        likes=_as_int(stats.get("digg_count")),
        comments=_as_int(stats.get("comment_count")),
        shares=_as_int(stats.get("share_count")),
        duration_s=(duration_ms // 1000) if duration_ms else None,
        play_url=play_url,
        download_url=download_url,
        size_bytes=size or download_size,
    )


def format_day(moment: Optional[datetime], tz: timezone) -> Optional[str]:
    """``21.08.2026`` - a clip's upload date needs the year."""
    if moment is None:
        return None
    return moment.astimezone(tz).strftime("%d.%m.%Y")


def format_length(seconds: Optional[int]) -> Optional[str]:
    """``1:23`` for a clip duration."""
    if not seconds or seconds < 0:
        return None
    minutes, rest = divmod(int(seconds), 60)
    return f"{minutes}:{rest:02d}"


def parse_clip_page(html: str) -> ClipPage:
    """Read the clip list and the profile image out of an embed page."""
    videos = parse_videos(html)
    if not videos and '"videoList"' not in html:
        # The page rendered without its embedded JSON: keep the ids we can see.
        videos = [VideoInfo(video_id=video_id) for video_id in video_ids(html)]
    avatar = None
    nickname = None
    marker = html.find('"userInfo":')
    if marker != -1:
        start = html.find("{", marker)
        blob = _json_slice_at(html, start) if start != -1 else None
        if blob:
            try:
                info = json.loads(blob)
            except ValueError:
                info = None
            if isinstance(info, dict):
                avatar = _as_text(info.get("avatarThumbUrl")) or _as_text(
                    info.get("avatarLargerUrl")
                )
                nickname = _as_text(info.get("nickname"))
    return ClipPage(videos=videos, avatar_url=avatar, nickname=nickname)


def parse_live_payload(handle: str, payload: Any) -> Optional[LiveInfo]:
    """Turn an ``api-live/user/room`` payload into a :class:`LiveInfo`.

    Returns ``None`` when TikTok says the handle does not exist, so a typo in a
    handle is not reported as "offline forever".
    """
    if not isinstance(payload, dict):
        return None
    code = _as_int(payload.get("statusCode"))
    if code == STATUS_USER_NOT_FOUND:
        return None
    data = payload.get("data")
    if not isinstance(data, dict):
        return None
    user_raw = data.get("user")
    user: Dict[str, Any] = user_raw if isinstance(user_raw, dict) else {}
    room_raw = data.get("liveRoom")
    room: Dict[str, Any] = room_raw if isinstance(room_raw, dict) else {}
    status = _as_int(user.get("status"))
    if status is None:
        status = _as_int(room.get("status"))
    stats_raw = room.get("liveRoomStats")
    stats: Dict[str, Any] = stats_raw if isinstance(stats_raw, dict) else {}
    title = _as_text(room.get("title"))
    cover = _as_text(room.get("coverUrl"))
    started = None
    start_raw = _as_int(room.get("startTime"))
    if start_raw:
        try:
            started = datetime.fromtimestamp(start_raw, tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            started = None
    # Some rooms come back as a stub: live status and a room id, but an empty
    # title, no cover and startTime 0. Their ``liveRoomStats`` are all zeros too,
    # so a 0 there is "withheld", not "nobody is watching".
    stub_room = not title and not cover and not started
    user_count = _as_int(stats.get("userCount"))
    avatar = (
        _as_text(user.get("avatarLarger"))
        or _as_text(user.get("avatarMedium"))
        or _as_text(user.get("avatarThumb"))
    )
    return LiveInfo(
        handle=handle,
        is_live=status == STATUS_LIVE,
        title=title,
        cover_url=cover,
        started_at=started,
        viewers=None if stub_room else (user_count or 0),
        avatar_url=avatar,
        stream_id=_as_int(room.get("streamId")),
        room_id=_as_int(user.get("roomId")),
        raw_status=status,
    )


def parse_videos(html: str) -> List[VideoInfo]:
    """Read the ``videoList`` out of a profile embed page."""
    marker = html.find('"videoList":')
    if marker == -1:
        return []
    start = html.find("[", marker)
    if start == -1:
        return []
    blob = _json_slice_at(html, start)
    if not blob:
        return []
    items: Any = None
    try:
        items = json.loads(blob)
    except ValueError:
        return []
    if not isinstance(items, list):
        return []
    videos: List[VideoInfo] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        video_id = _as_text(item.get("id"))
        if not video_id or not video_id.isdigit():
            continue
        videos.append(
            VideoInfo(
                video_id=video_id,
                desc=_as_text(item.get("desc")),
                cover_url=_as_text(item.get("coverUrl"))
                or _as_text(item.get("originCoverUrl")),
                play_count=_play_count(item.get("playCount")),
            )
        )
    return videos


def _play_count(value: Any) -> Optional[str]:
    """A play count as text; TikTok sends an int and sometimes a display form."""
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return f"{int(value):,}".replace(",", "'")
    text = str(value).strip()
    return text or None


def video_ids(html: str) -> List[str]:
    """Fallback: every clip id on the page, in page order."""
    seen: List[str] = []
    for video_id in VIDEO_ID_RE.findall(html):
        if video_id not in seen:
            seen.append(video_id)
    return seen


def _retry_after_header(value: Any) -> Optional[float]:
    """Parse a ``Retry-After`` header value into seconds."""
    seconds = _as_int(value)
    if seconds is None or seconds < 0:
        return None
    return float(min(seconds, MAX_BLOCK_SECONDS))


# --------------------------------------------------------------------------- #
# HTTP client
# --------------------------------------------------------------------------- #
class TikTokAPI:
    """Thin async client for the two public endpoints."""

    def __init__(self) -> None:
        self._session: Optional[aiohttp.ClientSession] = None

    async def open(self) -> None:
        """Create the HTTP session on first use."""
        if self._session is not None and not self._session.closed:
            return
        self._session = aiohttp.ClientSession(
            headers={
                "User-Agent": USER_AGENT,
                "Referer": "https://www.tiktok.com/",
                "Accept-Language": "en-US,en;q=0.9",
            },
            timeout=aiohttp.ClientTimeout(total=25, connect=10),
            # A cookie jar would hand back whatever TikTok sets on the first
            # call and make every later request look like one browser.
            cookie_jar=aiohttp.DummyCookieJar(),
        )

    async def close(self) -> None:
        """Close the session; safe to call twice."""
        session, self._session = self._session, None
        if session is not None and not session.closed:
            await session.close()

    async def _text(
        self,
        url: str,
        params: Optional[Dict[str, str]] = None,
        headers: Optional[Dict[str, str]] = None,
    ) -> str:
        """GET one URL and return its body, mapping TikTok's answers to errors."""
        await self.open()
        if self._session is None:  # pragma: no cover - open() guarantees it
            raise TikTokError("no HTTP session")
        try:
            async with self._session.get(
                url, params=params, headers=headers
            ) as response:
                body = await response.text(errors="replace")
                if response.status in (403, 429):
                    raise TikTokBlocked(
                        f"TikTok answered HTTP {response.status}",
                        _retry_after_header(response.headers.get("Retry-After"))
                        or 60.0,
                    )
                if 400 <= response.status < 500:
                    raise TikTokNotFound(f"TikTok answered HTTP {response.status}")
                if response.status != 200:
                    raise TikTokError(f"TikTok answered HTTP {response.status}")
                return body
        except asyncio.TimeoutError as exc:
            raise TikTokError("the request to TikTok timed out") from exc
        except aiohttp.ClientError as exc:
            raise TikTokError(f"network problem talking to TikTok: {exc}") from exc

    async def live_status(self, handle: str) -> Optional[LiveInfo]:
        """Look up one handle's live room. ``None`` means "no such handle"."""
        body = await self._text(
            LIVE_API, {"aid": "1988", "uniqueId": handle, "sourceType": "54"}
        )
        try:
            payload = json.loads(body)
        except ValueError as exc:
            raise TikTokError("the live endpoint answered with no JSON") from exc
        return parse_live_payload(handle, payload)

    async def clip_page(self, handle: str) -> ClipPage:
        """The clips and the profile image from a profile's embed page."""
        html = await self._text(CONTENT_URL.format(handle=handle))
        return parse_clip_page(html)

    async def video_stats(self, video_id: str) -> Optional[VideoStats]:
        """Upload date, counters and MP4 URLs for one clip.

        Only the mobile app endpoint exposes these without a signature. It is
        called for a clip that is about to be announced, never during a poll.
        """
        params = {"aweme_id": video_id, **MOBILE_PARAMS}
        body = await self._text(MOBILE_FEED, params, headers={"User-Agent": MOBILE_UA})
        try:
            payload = json.loads(body)
        except ValueError as exc:
            raise TikTokError("the mobile feed answered with no JSON") from exc
        return parse_video_stats(video_id, payload)

    async def download(
        self, url: str, max_bytes: int, timeout: int = 90
    ) -> Optional[bytes]:
        """Fetch a clip into memory, giving up as soon as it exceeds the cap."""
        await self.open()
        if self._session is None:  # pragma: no cover
            raise TikTokError("no HTTP session")
        try:
            async with self._session.get(
                url, timeout=aiohttp.ClientTimeout(total=timeout)
            ) as response:
                if response.status not in (200, 206):
                    # A missing or expired CDN URL is not worth an exception: the
                    # caller simply posts the embed without a file.
                    log.info(
                        "TikTokWatch: the video CDN answered HTTP %s", response.status
                    )
                    return None
                buffer = bytearray()
                async for chunk in response.content.iter_chunked(64 * 1024):
                    buffer.extend(chunk)
                    if len(buffer) > max_bytes:
                        log.info(
                            "TikTokWatch: clip is larger than %s bytes, skipping",
                            max_bytes,
                        )
                        return None
                return bytes(buffer)
        except asyncio.TimeoutError:
            log.info("TikTokWatch: the video download timed out")
            return None
        except aiohttp.ClientError as exc:
            log.info("TikTokWatch: the video download failed: %s", exc)
            return None


# --------------------------------------------------------------------------- #
# embeds
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class EmbedSettings:
    """How a guild wants its announcements rendered."""

    show_viewers: bool = True
    timezone_offset: int = 1
    hour_format: str = "local"

    @classmethod
    def from_guild_config(cls, data: Dict[str, Any]) -> "EmbedSettings":
        """Build settings from a raw guild config dict."""
        style = str(data.get("hour_format") or "local").lower()
        return cls(
            show_viewers=bool(data.get("show_viewers", True)),
            timezone_offset=_as_int(data.get("timezone_offset")) or 0,
            hour_format="relative" if style == "relative" else "local",
        )

    @property
    def tz(self) -> timezone:
        """The configured fixed-offset timezone."""
        return _tz_from(self.timezone_offset)


def build_live_embed(info: LiveInfo, settings: EmbedSettings) -> discord.Embed:
    """The "is live" announcement embed."""
    # No "Live on TikTok" placeholder title: the watch link below already says
    # it, and a duplicate line just pushes the real content down.
    embed = discord.Embed(
        colour=discord.Colour(TIKTOK_COLOR),
        title=info.title or None,
        url=info.url if info.title else None,
        timestamp=datetime.now(settings.tz),
    )
    embed.set_author(name=f"@{info.handle} is live on TikTok", url=info.url)
    lines: List[str] = []
    if settings.show_viewers and info.viewers is not None:
        lines.append(f"**Viewers:** {info.viewers:,}")
    started = format_start(info.started_at, settings.tz, settings.hour_format)
    if started:
        lines.append(f"**Started:** {started}")
    uptime = format_duration(info.uptime)
    if uptime:
        lines.append(f"**Live for:** {uptime}")
    lines.append("")
    lines.append(f"**[Watch on TikTok]({info.url})**")
    embed.description = "\n".join(lines)
    if info.cover_url:
        embed.set_image(url=info.cover_url)
        if info.avatar_url:
            embed.set_thumbnail(url=info.avatar_url)
    elif info.avatar_url:
        # No stream cover (stub rooms): the avatar carries the embed, and small
        # as a thumbnail it was barely visible.
        embed.set_image(url=info.avatar_url)
    embed.set_footer(text="TikTok LIVE")
    return embed


def build_content_embed(
    handle: str,
    video: VideoInfo,
    settings: EmbedSettings,
    avatar_url: Optional[str] = None,
    nickname: Optional[str] = None,
    stats: Optional[VideoStats] = None,
) -> discord.Embed:
    """The "new clip" announcement embed."""
    url = video.url(handle)
    embed = discord.Embed(
        colour=discord.Colour(CONTENT_COLOR),
        url=url,
        timestamp=datetime.now(settings.tz),
    )
    author_name = f"New TikTok from @{handle}"
    if nickname:
        author_name = f"New TikTok from {nickname} (@{handle})"
    if avatar_url:
        embed.set_author(
            name=author_name,
            url=PROFILE_URL.format(handle=handle),
            icon_url=avatar_url,
        )
    else:
        embed.set_author(name=author_name, url=PROFILE_URL.format(handle=handle))
    description = TIKTOK_SPAN.sub("", video.desc or "").strip()
    if len(description) > 900:
        description = description[:897] + "..."
    lines: List[str] = []
    if description:
        lines.append(f"> {description}")
        lines.append("")
    counters: List[str] = []
    if stats is not None and stats.plays is not None:
        counters.append(f"**Plays:** {_play_count(stats.plays)}")
        if stats.likes is not None:
            counters.append(f"**Likes:** {_play_count(stats.likes)}")
        if stats.comments is not None:
            counters.append(f"**Comments:** {_play_count(stats.comments)}")
    elif video.play_count:
        counters.append(f"**Plays:** {video.play_count}")
    if counters:
        lines.append(" - ".join(counters))
    meta: List[str] = []
    if stats is not None:
        day = format_day(stats.created_at, settings.tz)
        if day:
            meta.append(f"**Posted:** {day}")
        length = format_length(stats.duration_s)
        if length:
            meta.append(f"**Length:** {length}")
    if meta:
        lines.append(" - ".join(meta))
    lines.append("")
    lines.append(f"**[Watch on TikTok]({url})**")
    embed.description = "\n".join(lines)
    if video.cover_url:
        embed.set_image(url=video.cover_url)
    elif avatar_url:
        embed.set_image(url=avatar_url)
    embed.set_footer(text="TikTok | New clip")
    return embed


def build_offline_embed(
    info: LiveInfo, settings: EmbedSettings, duration: Optional[timedelta]
) -> discord.Embed:
    """The "stream ended" embed that replaces the live alert."""
    lines = [f"**@{info.handle}** is offline now."]
    if info.title:
        lines.append(f"> {info.title}")
    lines.append(f"[Visit the profile]({PROFILE_URL.format(handle=info.handle)})")
    embed = discord.Embed(
        colour=discord.Colour(OFFLINE_COLOR),
        description="\n".join(lines),
        timestamp=datetime.now(settings.tz),
    )
    embed.set_author(name=f"@{info.handle} is offline", url=info.url)
    if info.avatar_url:
        embed.set_image(url=info.avatar_url)
    pretty = format_duration(duration)
    if pretty:
        embed.add_field(name="Duration", value=pretty, inline=True)
    embed.set_footer(text="TikTok LIVE | Stream ended")
    return embed


def build_status_embed(
    handle: str,
    info: Optional[LiveInfo],
    page: Optional[ClipPage],
    settings: EmbedSettings,
) -> discord.Embed:
    """The embed used by ``[p]tiktok check``."""
    if info is None:
        embed = discord.Embed(
            colour=discord.Colour(OFFLINE_COLOR),
            title=f"@{handle}: no such account",
            description=(
                "TikTok does not know this handle, or the account cannot go live.\n"
                f"[Open the profile]({PROFILE_URL.format(handle=handle)})"
            ),
            timestamp=datetime.now(settings.tz),
        )
        return embed
    if info.is_live:
        return build_live_embed(info, settings)
    lines = [f"**@{handle}** is not live right now.", ""]
    if info.title:
        lines.append(f"**Last room:** {info.title}")
    videos = page.videos if page else []
    if videos:
        newest = videos[0]
        lines.append(f"**Latest clip:** {newest.desc or newest.video_id}")
    lines.append("")
    lines.append(f"[Open the profile]({PROFILE_URL.format(handle=handle)})")
    embed = discord.Embed(
        colour=discord.Colour(OFFLINE_COLOR),
        title=f"@{handle} is offline",
        url=PROFILE_URL.format(handle=handle),
        description="\n".join(lines),
        timestamp=datetime.now(settings.tz),
    )
    avatar = (page.avatar_url if page else None) or info.avatar_url
    if avatar:
        embed.set_thumbnail(url=avatar)
    embed.set_footer(text="TikTok")
    return embed


def build_debug_embed(
    handle: str, info: Optional[LiveInfo], page: Optional[ClipPage]
) -> discord.Embed:
    """Raw view for ``[p]tiktok debug``; no cookies or session data are read."""
    embed = discord.Embed(
        colour=discord.Colour(DEBUG_COLOR),
        title=f"TikTok debug: @{handle}",
        timestamp=datetime.now(timezone.utc),
    )
    lines: List[str] = []
    if info is None:
        lines.append("**live endpoint:** no data (handle unknown)")
    else:
        lines.append(f"**live status:** {info.raw_status}")
        lines.append(f"**is_live:** {info.is_live}")
        lines.append(f"**title:** {info.title}")
        lines.append(f"**started_at:** {info.started_at}")
        lines.append(f"**viewers:** {info.viewers}")
        lines.append(f"**stream_id:** {info.stream_id}")
        lines.append(f"**room_id:** {info.room_id}")
        lines.append(f"**cover:** {'yes' if info.cover_url else 'no'}")
    lines.append("")
    if page is None:
        lines.append("**embed page:** not fetched")
    elif not page.videos:
        lines.append("**embed page:** no clip list found")
    else:
        lines.append(f"**embed page:** {len(page.videos)} clips")
        lines.append(f"**avatar:** {'yes' if page.avatar_url else 'no'}")
        for video in page.videos[:5]:
            lines.append(
                f"- `{video.video_id}` plays={video.play_count} desc={(video.desc or '')[:60]}"
            )
    body = "\n".join(lines)
    embed.description = body[:3900]
    embed.set_footer(text="Raw endpoint data - no credentials involved")
    return embed


def fake_live(info: LiveInfo) -> LiveInfo:
    """Turn an offline handle into a fake live one for ``[p]tiktok test``."""
    return LiveInfo(
        handle=info.handle,
        is_live=True,
        title=info.title or "Testing the announcement settings",
        cover_url=info.cover_url,
        started_at=datetime.now(timezone.utc) - timedelta(minutes=42),
        viewers=info.viewers or 1234,
        stream_id=info.stream_id or 1,
        room_id=info.room_id,
        raw_status=STATUS_LIVE,
    )


# --------------------------------------------------------------------------- #
# the cog
# --------------------------------------------------------------------------- #
class TikTokWatch(commands.Cog):
    """Announce TikTok livestreams and new clips in Discord."""

    __version__ = "1.0.6"

    def __init__(self, bot: Red) -> None:
        self.bot = bot
        self.config = Config.get_conf(
            self, identifier=7274927493, force_registration=True
        )
        self.config.register_global(**DEFAULT_GLOBAL)
        self.config.register_guild(**DEFAULT_GUILD)
        self.api = TikTokAPI()
        self._live_task: Optional[asyncio.Task] = None
        self._content_task: Optional[asyncio.Task] = None
        self._blocked_until: float = 0.0
        self._block_reason: Optional[str] = None
        self._fail_streak: int = 0
        self._last_live_poll: Optional[datetime] = None
        self._last_live_result: Dict[str, int] = {}
        self._last_content_poll: Optional[datetime] = None
        self._last_content_result: Dict[str, int] = {}
        self._last_error: Optional[str] = None

    # -- lifecycle ---------------------------------------------------------- #
    async def cog_load(self) -> None:
        """Restore the block state and start both watchers."""
        global_data = await self.config.all()
        self._blocked_until = float(global_data.get("blocked_until") or 0.0)
        self._block_reason = global_data.get("block_reason")
        self._fail_streak = _as_int(global_data.get("fail_streak")) or 0
        self._live_task = asyncio.create_task(self._live_loop())
        self._content_task = asyncio.create_task(self._content_loop())

    def cog_unload(self) -> None:
        """Cancel both watchers and close the HTTP session.

        Red never awaits ``cog_unload``, so this has to be synchronous: an
        ``async def`` here would leave both loops running after an unload and
        leak the session on every reload.
        """
        for task in (self._live_task, self._content_task):
            if task is not None:
                task.cancel()
        self._live_task = None
        self._content_task = None
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None
        if loop is not None:
            loop.create_task(self.api.close())

    async def red_delete_data_for_user(self, *, requester: str, user_id: int) -> None:
        """Nothing is stored per user - everything here is guild level."""
        return

    async def red_get_data_for_user(self, *, user_id: int) -> Dict[str, io.BytesIO]:
        """Nothing is stored per user - everything here is guild level."""
        return {}

    def format_help_for_context(self, ctx: commands.Context) -> str:
        """Add the cog version to ``[p]help``."""
        base = super().format_help_for_context(ctx)
        return f"{base}\n\nCog version: {self.__version__}"

    # -- blocking ----------------------------------------------------------- #
    def _block_remaining(self) -> float:
        """Seconds left in the current back-off window."""
        return max(0.0, self._blocked_until - time.time())

    async def _register_block(self, error: TikTokBlocked) -> None:
        """Park every request for a growing window after a 403/429."""
        self._fail_streak += 1
        wait = min(MAX_BLOCK_SECONDS, max(30.0, error.retry_after) * self._fail_streak)
        self._blocked_until = time.time() + wait
        self._block_reason = str(error)
        self._last_error = str(error)
        log.warning(
            "TikTokWatch: TikTok is rate limiting us (%s); pausing for %.0f s",
            error,
            wait,
        )
        with contextlib.suppress(Exception):
            await self.config.blocked_until.set(self._blocked_until)
            await self.config.block_reason.set(self._block_reason)
            await self.config.fail_streak.set(self._fail_streak)

    async def _clear_block(self) -> None:
        """Forget a previous block after a successful sweep."""
        if not self._fail_streak and not self._block_reason:
            return
        self._fail_streak = 0
        self._block_reason = None
        self._blocked_until = 0.0
        with contextlib.suppress(Exception):
            await self.config.blocked_until.set(0.0)
            await self.config.block_reason.set(None)
            await self.config.fail_streak.set(0)

    # -- loops -------------------------------------------------------------- #
    async def _live_loop(self) -> None:
        """Poll every watched handle's live room forever."""
        await self.bot.wait_until_ready()
        while True:
            delay = float(DEFAULT_GUILD["check_interval"])
            try:
                delay = float(
                    await self._next_interval("check_interval", MIN_LIVE_INTERVAL)
                )
                remaining = self._block_remaining()
                if remaining > 0:
                    delay = max(delay, remaining)
                else:
                    self._last_live_result = await self._sweep_live()
                    self._last_live_poll = datetime.now(timezone.utc)
            except asyncio.CancelledError:
                raise
            except TikTokBlocked as exc:
                await self._register_block(exc)
                delay = max(delay, self._block_remaining())
            except Exception:  # noqa: BLE001 - a poll must never kill the loop
                log.exception("TikTokWatch: unexpected error while polling live status")
                self._last_error = "unexpected error while polling live status"
                delay = max(delay, 120.0)
            await asyncio.sleep(delay * random.uniform(0.95, 1.08))

    async def _content_loop(self) -> None:
        """Poll every watched handle's embed page for new clips forever."""
        await self.bot.wait_until_ready()
        while True:
            delay = float(DEFAULT_GUILD["content_interval"])
            try:
                delay = float(
                    await self._next_interval("content_interval", MIN_CONTENT_INTERVAL)
                )
                remaining = self._block_remaining()
                if remaining > 0:
                    delay = max(delay, remaining)
                else:
                    self._last_content_result = await self._sweep_content()
                    self._last_content_poll = datetime.now(timezone.utc)
            except asyncio.CancelledError:
                raise
            except TikTokBlocked as exc:
                await self._register_block(exc)
                delay = max(delay, self._block_remaining())
            except Exception:  # noqa: BLE001
                log.exception("TikTokWatch: unexpected error while polling content")
                self._last_error = "unexpected error while polling content"
                delay = max(delay, 300.0)
            await asyncio.sleep(delay * random.uniform(0.95, 1.08))

    async def _next_interval(self, key: str, minimum: int) -> int:
        """The shortest interval any active guild asked for."""
        guilds = await self.config.all_guilds()
        values = [
            _clamp(data.get(key), minimum, MAX_LIVE_INTERVAL, DEFAULT_GUILD[key])
            for data in guilds.values()
            if data.get("accounts") and data.get("enabled", True)
        ]
        return min(values) if values else int(DEFAULT_GUILD[key])

    def _active_guilds(
        self, guilds: Dict[int, Dict[str, Any]], announce_key: str
    ) -> Dict[int, Dict[str, Any]]:
        """Guilds that have accounts, are enabled and want this alert kind."""
        return {
            guild_id: data
            for guild_id, data in guilds.items()
            if data.get("accounts")
            and data.get("enabled", True)
            and data.get(announce_key, True)
        }

    # -- sweeps ------------------------------------------------------------- #
    async def _fetch_live(self, handles: List[str]) -> Dict[str, Optional[LiveInfo]]:
        """Fetch live rooms concurrently; holds the first block error back."""
        results: Dict[str, Optional[LiveInfo]] = {}
        blocked: Optional[TikTokBlocked] = None

        async def one(handle: str) -> None:
            nonlocal blocked
            try:
                results[handle] = await self.api.live_status(handle)
            except TikTokBlocked as exc:
                blocked = blocked or exc
            except TikTokNotFound:
                results[handle] = None
            except TikTokError as exc:
                self._last_error = str(exc)
                log.debug("TikTokWatch: live lookup for %s failed: %s", handle, exc)

        await bounded_gather(
            *[one(handle) for handle in handles],
            limit=REQUEST_CONCURRENCY,
            return_exceptions=True,
        )
        if blocked is not None:
            raise blocked
        return results

    async def _sweep_live(self) -> Dict[str, int]:
        """One live pass over every guild."""
        guilds = await self.config.all_guilds()
        watched = self._active_guilds(guilds, "announce_live")
        result = {"guilds": len(watched), "accounts": 0, "live": 0, "alerts": 0}
        if not watched:
            return result
        handles = sorted({h for data in watched.values() for h in data["accounts"]})
        result["accounts"] = len(handles)
        infos = await self._fetch_live(handles)
        await self._clear_block()
        for guild_id, data in watched.items():
            guild = self.bot.get_guild(guild_id)
            if guild is None:
                continue
            if await self.bot.cog_disabled_in_guild(self, guild):
                continue
            try:
                counts = await self._poll_guild_live(guild, data, infos)
            except Exception:  # noqa: BLE001 - one guild must not stop the sweep
                log.exception("TikTokWatch: failed to update guild %s", guild_id)
                continue
            result["live"] += counts["live"]
            result["alerts"] += counts["alerts"]
        return result

    async def _fetch_content(self, handles: List[str]) -> Dict[str, ClipPage]:
        """Fetch embed pages concurrently; holds the first block error back."""
        results: Dict[str, ClipPage] = {}
        blocked: Optional[TikTokBlocked] = None

        async def one(handle: str) -> None:
            nonlocal blocked
            try:
                results[handle] = await self.api.clip_page(handle)
            except TikTokBlocked as exc:
                blocked = blocked or exc
            except TikTokNotFound:
                results[handle] = ClipPage()
            except TikTokError as exc:
                self._last_error = str(exc)
                log.debug("TikTokWatch: content lookup for %s failed: %s", handle, exc)

        await bounded_gather(
            *[one(handle) for handle in handles],
            limit=REQUEST_CONCURRENCY,
            return_exceptions=True,
        )
        if blocked is not None:
            raise blocked
        return results

    async def _sweep_content(self) -> Dict[str, int]:
        """One content pass over every guild."""
        guilds = await self.config.all_guilds()
        watched = self._active_guilds(guilds, "announce_content")
        result = {"guilds": len(watched), "accounts": 0, "new": 0}
        if not watched:
            return result
        handles = sorted({h for data in watched.values() for h in data["accounts"]})
        result["accounts"] = len(handles)
        pages = await self._fetch_content(handles)
        await self._clear_block()
        for guild_id, data in watched.items():
            guild = self.bot.get_guild(guild_id)
            if guild is None:
                continue
            if await self.bot.cog_disabled_in_guild(self, guild):
                continue
            try:
                result["new"] += await self._poll_guild_content(guild, data, pages)
            except Exception:  # noqa: BLE001
                log.exception("TikTokWatch: failed to update guild %s", guild_id)
        return result

    async def _poll_guild_live(
        self,
        guild: discord.Guild,
        data: Dict[str, Any],
        infos: Dict[str, Optional[LiveInfo]],
    ) -> Dict[str, int]:
        """Apply one guild's new live states."""
        settings = EmbedSettings.from_guild_config(data)
        counts = {"live": 0, "alerts": 0}
        for handle, entry in (data.get("accounts") or {}).items():
            if handle not in infos:
                continue
            info = infos[handle]
            if info is not None and info.is_live:
                counts["live"] += 1
            action = await self._handle_live(guild, handle, entry, info, data, settings)
            if action in ("announced", "ended"):
                counts["alerts"] += 1
        return counts

    async def _handle_live(
        self,
        guild: discord.Guild,
        handle: str,
        entry: Dict[str, Any],
        info: Optional[LiveInfo],
        data: Dict[str, Any],
        settings: EmbedSettings,
    ) -> str:
        """Apply one account's live state and return what happened."""
        state = {**ACCOUNT_DEFAULTS, **(entry or {})}
        if info is None:
            await self._store_account(guild, handle, missing=True)
            return "missing"
        if state.get("missing"):
            await self._store_account(guild, handle, missing=False)
        if state.get("muted"):
            # Keep the state current so unmuting does not announce a stale stream.
            await self._store_account(
                guild,
                handle,
                is_live=info.is_live,
                stream_id=info.stream_id,
                stream_key=info.stream_key,
                room_id=info.room_id,
                last_title=info.title,
            )
            return "muted"
        channel = self._resolve_channel(guild, state.get("channel_id"))
        if channel is None:
            return "no-channel"
        was_live = bool(state.get("is_live"))
        stored_key = state.get("stream_key")
        current_key = info.stream_key
        new_broadcast = False
        if info.is_live:
            if not was_live:
                new_broadcast = True
            elif stored_key is None:
                # Either state written before stream keys existed, or a room
                # that hides its ids: remember the key, do not post twice.
                await self._store_account(guild, handle, stream_key=current_key)
            elif current_key is not None and current_key != stored_key:
                new_broadcast = True  # a genuine restart of the stream
        if info.is_live and new_broadcast:
            delivered = await self._announce_live(
                guild, handle, info, settings, channel, state, data
            )
            return "announced" if delivered else "no-permission"
        if info.is_live:
            if bool(data.get("edit_live_alerts", True)) and self._needs_refresh(
                state, info, _clamp(data.get("live_update_seconds"), 60, 3600, 300)
            ):
                await self._refresh_live(guild, handle, info, settings, channel, state)
                return "updated"
            peak = max(_as_int(state.get("peak_viewers")) or 0, info.viewers or 0)
            await self._store_account(
                guild, handle, last_title=info.title, peak_viewers=peak
            )
            return "unchanged"
        if was_live:
            await self._end_alert(guild, handle, info, settings, channel, state, data)
            return "ended"
        await self._store_account(guild, handle, peak_viewers=0)
        return "unchanged"

    async def _poll_guild_content(
        self,
        guild: discord.Guild,
        data: Dict[str, Any],
        pages: Dict[str, ClipPage],
    ) -> int:
        """Apply one guild's new clips and return how many were announced."""
        settings = EmbedSettings.from_guild_config(data)
        announced = 0
        for handle, entry in (data.get("accounts") or {}).items():
            if handle not in pages:
                continue
            page = pages[handle]
            if not page.videos:
                continue
            action = await self._handle_content(
                guild, handle, entry, page, data, settings
            )
            if action == "announced":
                announced += 1
        return announced

    async def _handle_content(
        self,
        guild: discord.Guild,
        handle: str,
        entry: Dict[str, Any],
        page: ClipPage,
        data: Dict[str, Any],
        settings: EmbedSettings,
    ) -> str:
        """Detect new clips for one account.

        The embed page lists a pinned clip first, so position zero says nothing
        about age: new content is whatever id is not in the stored set.
        """
        videos = page.videos
        state = {**ACCOUNT_DEFAULTS, **(entry or {})}
        known = [str(item) for item in (state.get("seen_videos") or [])]
        known_set = set(known)
        fresh = [video for video in videos if video.video_id not in known_set]
        if not state.get("primed"):
            announce = bool(data.get("announce_existing", False))
            newest = videos[0] if videos else None
            chosen = [newest] if announce and newest is not None else []
        else:
            chosen = fresh
        if not chosen:
            if fresh:
                await self._store_account(
                    guild,
                    handle,
                    primed=True,
                    seen_videos=self._remember(known, fresh),
                )
            elif not state.get("primed"):
                await self._store_account(guild, handle, primed=True)
            return "unchanged"
        channel = self._resolve_channel(guild, state.get("channel_id"))
        if channel is None:
            return "no-channel"
        if state.get("muted"):
            await self._store_account(
                guild,
                handle,
                primed=True,
                seen_videos=self._remember(known, fresh or chosen),
                last_video_id=chosen[0].video_id,
            )
            return "muted"
        attach_video = bool(data.get("announce_videos", True))
        max_mb = _clamp(data.get("video_max_mb"), 1, 25, DEFAULT_VIDEO_MAX_MB)
        # Announce oldest-first so a backlog reads in the order it was posted.
        for video in reversed(chosen):
            if not self._can_post(guild, channel, handle):
                return "no-permission"
            values = {
                "handle": f"@{handle}",
                "title": video.desc,
                "url": video.url(handle),
                "viewers": None,
                "started": None,
            }
            content = self._alert_text(state, guild, values)
            stats = await self._clip_stats(handle, video)
            try:
                await channel.send(
                    content=content,
                    embed=build_content_embed(
                        handle,
                        video,
                        settings,
                        avatar_url=page.avatar_url,
                        nickname=page.nickname,
                        stats=stats,
                    ),
                    allowed_mentions=discord.AllowedMentions(
                        roles=True,
                        everyone=pings_everyone(guild, state.get("ping_role_id")),
                    ),
                )
            except discord.HTTPException:
                log.exception("TikTokWatch: could not post a clip for %s", handle)
                return "no-permission"
            if attach_video:
                await self._send_video(guild, channel, handle, video, stats, max_mb)
        await self._store_account(
            guild,
            handle,
            primed=True,
            seen_videos=self._remember(known, chosen),
            last_video_id=chosen[0].video_id,
        )
        log.info("TikTokWatch: announced %s new clip(s) for @%s", len(chosen), handle)
        return "announced"

    async def _clip_stats(self, handle: str, video: VideoInfo) -> Optional[VideoStats]:
        """Fetch upload date, counters and MP4 URLs for one clip.

        Best effort: the announcement goes out even when this endpoint fails, so
        an enrichment can never cost a clip alert.
        """
        try:
            return await self.api.video_stats(video.video_id)
        except TikTokBlocked as exc:
            log.warning(
                "TikTokWatch: mobile feed rate limited while enriching %s: %s",
                handle,
                exc,
            )
        except TikTokError as exc:
            log.debug("TikTokWatch: no clip details for %s: %s", video.video_id, exc)
        return None

    async def _send_video(
        self,
        guild: discord.Guild,
        channel: discord.TextChannel,
        handle: str,
        video: VideoInfo,
        stats: Optional[VideoStats],
        max_mb: int,
    ) -> None:
        """Post the clip itself as an MP4, if it fits Discord's upload limit."""
        if stats is None:
            return
        url = stats.play_url or stats.download_url
        if not url:
            return
        guild_limit = _as_int(getattr(guild, "filesize_limit", None))
        limit = min(
            max_mb * 1024 * 1024, guild_limit or DEFAULT_VIDEO_MAX_MB * 1024 * 1024
        )
        if stats.size_bytes and stats.size_bytes > limit:
            log.info(
                "TikTokWatch: clip %s is %s bytes, over the %s byte limit",
                video.video_id,
                stats.size_bytes,
                limit,
            )
            return
        data = await self.api.download(url, limit)
        if not data:
            return
        permissions = channel.permissions_for(guild.me)
        if not permissions.attach_files:
            log.warning(
                "TikTokWatch: missing Attach Files in #%s, not posting the clip",
                channel,
            )
            return
        filename = f"{handle}_{video.video_id}.mp4"
        try:
            await channel.send(
                file=discord.File(io.BytesIO(data), filename=filename),
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except discord.HTTPException:
            log.exception("TikTokWatch: could not upload the clip for %s", handle)

    @staticmethod
    def _remember(known: List[str], fresh: List[VideoInfo]) -> List[str]:
        """Keep the newest ids first and bounded."""
        merged: List[str] = []
        for video in fresh:
            if video.video_id not in merged:
                merged.append(video.video_id)
        for video_id in known:
            if video_id not in merged:
                merged.append(video_id)
        return merged[:SEEN_VIDEO_LIMIT]

    # -- alert actions ------------------------------------------------------ #
    def _resolve_channel(
        self, guild: discord.Guild, per_account: Any
    ) -> Optional[discord.TextChannel]:
        """The alert channel stored on the account entry.

        There is deliberately no guild-wide default: a channel is chosen per
        account with ``[p]tiktok add`` and can be changed with ``setchannel``.
        """
        identifier = _as_int(per_account)
        if not identifier:
            return None
        channel = guild.get_channel(identifier)
        if isinstance(channel, discord.TextChannel):
            return channel
        return None

    def _live_values(self, info: Optional[LiveInfo], handle: str) -> Dict[str, Any]:
        """Placeholder values for a custom message."""
        if info is None:
            return {
                "handle": f"@{handle}",
                "title": None,
                "url": PROFILE_URL.format(handle=handle),
                "viewers": None,
                "started": None,
            }
        return {
            "handle": f"@{handle}",
            "title": info.title,
            "url": info.url,
            "viewers": info.viewers,
            "started": info.started_at,
        }

    def _alert_text(
        self,
        state: Dict[str, Any],
        guild: discord.Guild,
        values: Dict[str, Any],
    ) -> Optional[str]:
        """Role ping plus optional custom message."""
        lines: List[str] = []
        mention = ping_mention(guild, state.get("ping_role_id"))
        if mention:
            lines.append(mention)
        template = state.get("custom_message")
        if template:
            lines.append(render_template(str(template), values))
        return "\n".join(lines) if lines else None

    async def _announce_live(
        self,
        guild: discord.Guild,
        handle: str,
        info: LiveInfo,
        settings: EmbedSettings,
        channel: discord.TextChannel,
        state: Dict[str, Any],
        data: Dict[str, Any],
    ) -> bool:
        """Post the live alert. ``False`` when the bot may not post."""
        if not self._can_post(guild, channel, handle):
            return False
        try:
            message = await channel.send(
                content=self._alert_text(state, guild, self._live_values(info, handle)),
                embed=build_live_embed(info, settings),
                allowed_mentions=discord.AllowedMentions(
                    roles=True,
                    everyone=pings_everyone(guild, state.get("ping_role_id")),
                ),
            )
        except discord.HTTPException:
            log.exception("TikTokWatch: could not post the live alert for %s", handle)
            return False
        await self._store_account(
            guild,
            handle,
            is_live=True,
            stream_id=info.stream_id,
            stream_key=info.stream_key,
            room_id=info.room_id,
            announced_at=time.time(),
            started_at=info.started_at.isoformat() if info.started_at else None,
            alert_message_id=message.id,
            last_edit=time.time(),
            last_title=info.title,
            peak_viewers=info.viewers or 0,
        )
        log.info("TikTokWatch: announced @%s live in guild %s", handle, guild.id)
        return True

    def _needs_refresh(
        self, state: Dict[str, Any], info: LiveInfo, seconds: int
    ) -> bool:
        """Whether the live embed is worth editing right now."""
        if not state.get("alert_message_id"):
            return False
        if state.get("last_title") != info.title:
            return True
        return (time.time() - float(state.get("last_edit") or 0)) >= seconds

    async def _refresh_live(
        self,
        guild: discord.Guild,
        handle: str,
        info: LiveInfo,
        settings: EmbedSettings,
        channel: discord.TextChannel,
        state: Dict[str, Any],
    ) -> bool:
        """Edit the existing live alert with fresh numbers."""
        message_id = state.get("alert_message_id")
        if not message_id:
            return False
        try:
            message = await channel.fetch_message(int(message_id))
            await message.edit(embed=build_live_embed(info, settings))
        except discord.NotFound:
            await self._store_account(
                guild, handle, alert_message_id=None, last_edit=None
            )
            return False
        except (discord.Forbidden, discord.HTTPException):
            log.debug("TikTokWatch: could not refresh the alert for %s", handle)
            return False
        peak = max(_as_int(state.get("peak_viewers")) or 0, info.viewers or 0)
        await self._store_account(
            guild,
            handle,
            last_edit=time.time(),
            last_title=info.title,
            peak_viewers=peak,
        )
        return True

    async def _end_alert(
        self,
        guild: discord.Guild,
        handle: str,
        info: LiveInfo,
        settings: EmbedSettings,
        channel: discord.TextChannel,
        state: Dict[str, Any],
        data: Dict[str, Any],
    ) -> None:
        """Finish a stream: post a summary, edit the alert or delete it."""
        message_id = state.get("alert_message_id")
        started = None
        if state.get("started_at"):
            with contextlib.suppress(ValueError):
                started = datetime.fromisoformat(str(state["started_at"]))
        if started is None and state.get("announced_at"):
            # Rooms without a startTime: the moment we announced is the closest
            # thing to a stream start that exists.
            with contextlib.suppress(TypeError, ValueError, OverflowError):
                started = datetime.fromtimestamp(
                    float(state["announced_at"]), tz=timezone.utc
                )
        duration = None
        if started is not None:
            duration = datetime.now(timezone.utc) - started
        mode = str(data.get("offline_post") or "new").lower()
        if mode not in ("new", "edit", "delete"):
            mode = "new"
        if mode == "new":
            if self._can_post(guild, channel, handle):
                with contextlib.suppress(discord.HTTPException):
                    await channel.send(
                        embed=build_offline_embed(info, settings, duration)
                    )
            if message_id:
                await self._delete_message(channel, message_id, handle)
        elif message_id:
            try:
                message = await channel.fetch_message(int(message_id))
                if mode == "delete":
                    await message.delete()
                else:
                    await message.edit(
                        content=None,
                        embed=build_offline_embed(info, settings, duration),
                    )
            except discord.NotFound:
                pass
            except (discord.Forbidden, discord.HTTPException):
                log.debug(
                    "TikTokWatch: could not update the ended alert for %s", handle
                )
        await self._store_account(
            guild,
            handle,
            is_live=False,
            stream_id=None,
            stream_key=None,
            announced_at=None,
            started_at=None,
            alert_message_id=None,
            last_edit=None,
            last_title=info.title,
            peak_viewers=0,
        )

    def _can_post(
        self, guild: discord.Guild, channel: discord.TextChannel, handle: str
    ) -> bool:
        """Whether the bot may post embeds in this channel."""
        permissions = channel.permissions_for(guild.me)
        if permissions.send_messages and permissions.embed_links:
            return True
        log.warning(
            "TikTokWatch: missing Send Messages/Embed Links in #%s (guild %s, @%s)",
            channel,
            guild.id,
            handle,
        )
        return False

    async def _delete_message(
        self, channel: discord.TextChannel, message_id: Any, handle: str
    ) -> None:
        """Delete a stored alert, ignoring one that is already gone."""
        try:
            message = await channel.fetch_message(int(message_id))
            await message.delete()
        except discord.NotFound:
            return
        except (discord.Forbidden, discord.HTTPException, TypeError, ValueError):
            log.debug("TikTokWatch: could not delete the alert for %s", handle)

    async def _store_account(
        self, guild: discord.Guild, handle: str, **values: Any
    ) -> None:
        """Merge new state into one account entry."""
        async with self.config.guild(guild).accounts() as accounts:
            entry = accounts.get(handle)
            if not isinstance(entry, dict):
                return
            entry.update(values)
            accounts[handle] = entry

    # -- commands ----------------------------------------------------------- #
    @commands.guild_only()
    @commands.group(name="tiktok", aliases=["ttwatch"], invoke_without_command=True)
    async def tiktok(self, ctx: commands.Context) -> None:
        """Watch TikTok creators for livestreams and new clips."""
        prefix = ctx.clean_prefix
        await ctx.send(
            "**TikTokWatch** - livestream and new-clip alerts.\n"
            f"`{prefix}tiktok add <handle> #channel` - watch a creator and pick its channel\n"
            f"`{prefix}tiktok list` - watched accounts and their state\n"
            f"`{prefix}tiktok check <handle>` - probe TikTok right now\n"
            f"`{prefix}tiktok settings` - current configuration\n"
            "Full manual: see the cog's README."
        )

    @tiktok.command(name="add")
    @checks.admin_or_permissions(manage_guild=True)
    async def tiktok_add(
        self,
        ctx: commands.Context,
        handle: str,
        channel: discord.TextChannel,
    ) -> None:
        """Watch a TikTok creator and post its alerts in this channel.

        Every account carries its own channel - there is no guild-wide default.
        """
        cleaned = clean_handle(handle)
        if not cleaned:
            await ctx.send(
                "That is not a TikTok handle. Use `@name` or the profile URL."
            )
            return
        accounts = await self.config.guild(ctx.guild).accounts()
        if cleaned in accounts:
            await ctx.send(f"`@{cleaned}` is already being watched.")
            return
        entry = dict(ACCOUNT_DEFAULTS)
        entry["channel_id"] = channel.id
        entry["seen_videos"] = []
        async with self.config.guild(ctx.guild).accounts() as stored:
            stored[cleaned] = entry

        # Prime the clip list so the first content pass does not announce the
        # whole profile, and read the live state once for the confirmation.
        note = ""
        page = ClipPage()
        try:
            page = await self.api.clip_page(cleaned)
            await self._store_account(
                ctx.guild,
                cleaned,
                primed=True,
                seen_videos=[video.video_id for video in page.videos][
                    :SEEN_VIDEO_LIMIT
                ],
                last_video_id=page.videos[0].video_id if page.videos else None,
            )
        except TikTokBlocked as exc:
            await self._register_block(exc)
            note = "\nTikTok is rate limiting right now; the clip list is read later."
        except TikTokError as exc:
            note = f"\nThe clip list could not be read yet ({exc})."
        status = "unknown"
        try:
            info = await self.api.live_status(cleaned)
        except (TikTokError, TikTokBlocked):
            info = None
        if info is not None:
            status = "live right now" if info.is_live else "offline"
            if info.is_live:
                note += f" **@{cleaned} is live right now.**"
        elif info is None:
            status = "handle not found by TikTok"
        await ctx.send(
            f"Now watching `@{cleaned}` ({status})."
            f"\nClips on the profile: {len(page.videos)}. Live polls every "
            f"{await self.config.guild(ctx.guild).check_interval()} s, clips every "
            f"{await self.config.guild(ctx.guild).content_interval()} s.{note}"
        )

    @tiktok.command(name="remove")
    @checks.admin_or_permissions(manage_guild=True)
    async def tiktok_remove(self, ctx: commands.Context, handle: str) -> None:
        """Stop watching a creator."""
        cleaned = clean_handle(handle)
        accounts = await self.config.guild(ctx.guild).accounts()
        if not cleaned or cleaned not in accounts:
            await ctx.send(f"`@{cleaned or handle}` is not being watched.")
            return
        async with self.config.guild(ctx.guild).accounts() as stored:
            stored.pop(cleaned, None)
        await ctx.send(f"Stopped watching `@{cleaned}`.")

    @tiktok.command(name="list")
    async def tiktok_list(self, ctx: commands.Context) -> None:
        """List the watched accounts and their last known state."""
        data = await self.config.guild(ctx.guild).all()
        accounts = data.get("accounts") or {}
        if not accounts:
            await ctx.send(
                f"No accounts yet - add one with `{ctx.clean_prefix}tiktok add <handle>`."
            )
            return
        lines: List[str] = []
        for handle, raw in accounts.items():
            entry = {**ACCOUNT_DEFAULTS, **(raw or {})}
            flags: List[str] = []
            if entry.get("muted"):
                flags.append("muted")
            if entry.get("missing"):
                flags.append("handle not found")
            if entry.get("is_live"):
                flags.append("**LIVE**")
            channel_id = entry.get("channel_id")
            where = (
                f"<#{channel_id}>" if channel_id else "**no channel - alerts are off**"
            )
            state = ", ".join(flags) if flags else "offline"
            lines.append(f"`@{handle}` - {state} - {where}")
        embed = discord.Embed(
            colour=discord.Colour(TIKTOK_COLOR),
            title=f"Watched TikTok accounts ({len(accounts)})",
            description="\n".join(lines)[:4000],
            timestamp=datetime.now(timezone.utc),
        )
        embed.set_footer(text="TikTokWatch")
        await ctx.send(embed=embed)

    @tiktok.command(name="toggle")
    @checks.admin_or_permissions(manage_guild=True)
    async def tiktok_toggle(self, ctx: commands.Context, handle: str) -> None:
        """Mute or unmute one account without removing it."""
        cleaned = clean_handle(handle)
        accounts = await self.config.guild(ctx.guild).accounts()
        if not cleaned or cleaned not in accounts:
            await ctx.send(f"`@{cleaned or handle}` is not being watched.")
            return
        current = bool((accounts.get(cleaned) or {}).get("muted"))
        await self._store_account(ctx.guild, cleaned, muted=not current)
        await ctx.send(f"`@{cleaned}` is now {'muted' if not current else 'active'}.")

    @tiktok.command(name="setchannel")
    @checks.admin_or_permissions(manage_guild=True)
    async def tiktok_setchannel(
        self,
        ctx: commands.Context,
        handle: str,
        channel: discord.TextChannel,
    ) -> None:
        """Change the alert channel of one watched account."""
        cleaned = clean_handle(handle)
        accounts = await self.config.guild(ctx.guild).accounts()
        if not cleaned or cleaned not in accounts:
            await ctx.send(f"`@{cleaned or handle}` is not being watched.")
            return
        await self._store_account(ctx.guild, cleaned, channel_id=channel.id)
        await ctx.send(f"Alerts for `@{cleaned}` now go to {channel.mention}.")

    @tiktok.command(name="setrole")
    @checks.admin_or_permissions(manage_guild=True)
    async def tiktok_setrole(
        self,
        ctx: commands.Context,
        handle: str,
        role: Optional[discord.Role] = None,
    ) -> None:
        """Set the role pinged for one account (omit the role to clear it)."""
        cleaned = clean_handle(handle)
        accounts = await self.config.guild(ctx.guild).accounts()
        if not cleaned or cleaned not in accounts:
            await ctx.send(f"`@{cleaned or handle}` is not being watched.")
            return
        await self._store_account(
            ctx.guild, cleaned, ping_role_id=role.id if role else None
        )
        if role is None:
            await ctx.send(f"`@{cleaned}` no longer pings a role.")
            return
        mention = ping_mention(ctx.guild, role.id) or role.name
        note = ""
        if pings_everyone(ctx.guild, role.id):
            permissions = ctx.channel.permissions_for(ctx.guild.me)
            if not getattr(permissions, "mention_everyone", False):
                note = (
                    "\nI lack **Mention @everyone** in this channel, so the "
                    "alert will be posted without a ping."
                )
        await ctx.send(f"`@{cleaned}` alerts will ping {mention}.{note}")

    @tiktok.command(name="message")
    @checks.admin_or_permissions(manage_guild=True)
    async def tiktok_message(
        self, ctx: commands.Context, handle: str, *, text: Optional[str] = None
    ) -> None:
        """Set a custom line above one account's alerts (omit to clear)."""
        cleaned = clean_handle(handle)
        accounts = await self.config.guild(ctx.guild).accounts()
        if not cleaned or cleaned not in accounts:
            await ctx.send(f"`@{cleaned or handle}` is not being watched.")
            return
        if text and text.lower() in ("clear", "none", "off"):
            text = None
        await self._store_account(ctx.guild, cleaned, custom_message=text)
        if text:
            unknown = [
                name
                for name in TEMPLATE_RE.findall(text)
                if name not in TEMPLATE_FIELDS
            ]
            note = (
                f"\nUnknown placeholders (left as text): {humanize_list(unknown)}"
                if unknown
                else ""
            )
            await ctx.send(
                f"Custom message for `@{cleaned}` saved.{note}\n"
                f"Placeholders: {humanize_list(list(TEMPLATE_FIELDS))}"
            )
        else:
            await ctx.send(f"Custom message for `@{cleaned}` cleared.")

    @tiktok.command(name="interval")
    @checks.admin_or_permissions(manage_guild=True)
    async def tiktok_interval(
        self, ctx: commands.Context, kind: str, seconds: int
    ) -> None:
        """Set the poll interval: `live` or `content`, in seconds."""
        kind = kind.lower()
        if kind in ("live", "livestream"):
            value = _clamp(seconds, MIN_LIVE_INTERVAL, MAX_LIVE_INTERVAL, 90)
            await self.config.guild(ctx.guild).check_interval.set(value)
        elif kind in ("content", "clips", "videos"):
            value = _clamp(seconds, MIN_CONTENT_INTERVAL, MAX_CONTENT_INTERVAL, 600)
            await self.config.guild(ctx.guild).content_interval.set(value)
        else:
            await ctx.send("Use `live` or `content` as the first argument.")
            return
        await ctx.send(f"The {kind} poll interval is now {value} s.")

    @tiktok.command(name="alerts")
    @checks.admin_or_permissions(manage_guild=True)
    async def tiktok_alerts(
        self, ctx: commands.Context, kind: str, state: bool
    ) -> None:
        """Turn `live` or `content` announcements on or off."""
        kind = kind.lower()
        if kind in ("live", "livestream"):
            await self.config.guild(ctx.guild).announce_live.set(state)
        elif kind in ("content", "clips", "videos"):
            await self.config.guild(ctx.guild).announce_content.set(state)
        elif kind in ("all", "everything"):
            await self.config.guild(ctx.guild).announce_live.set(state)
            await self.config.guild(ctx.guild).announce_content.set(state)
        else:
            await ctx.send("Use `live`, `content` or `all` as the first argument.")
            return
        await ctx.send(
            f"Announcements for **{kind}** are now {'on' if state else 'off'}."
        )

    @tiktok.command(name="videos")
    @checks.admin_or_permissions(manage_guild=True)
    async def tiktok_videos(self, ctx: commands.Context, state: bool) -> None:
        """Attach the MP4 of a new clip to its announcement."""
        await self.config.guild(ctx.guild).announce_videos.set(state)
        if state:
            await ctx.send(
                "New clips are posted as an MP4 as well (needs **Attach Files** "
                f"in the channel, and respects `{ctx.clean_prefix}tiktok videomax`)."
            )
        else:
            await ctx.send("New clips are announced as an embed only.")

    @tiktok.command(name="videomax")
    @checks.admin_or_permissions(manage_guild=True)
    async def tiktok_videomax(self, ctx: commands.Context, megabytes: int) -> None:
        """Largest clip file to attach, in MB (1-25)."""
        value = max(1, min(25, int(megabytes)))
        await self.config.guild(ctx.guild).video_max_mb.set(value)
        note = "" if value == megabytes else f" (clamped from {megabytes})"
        await ctx.send(f"Clips up to {value} MB are attached{note}.")

    @tiktok.command(name="offline")
    @checks.admin_or_permissions(manage_guild=True)
    async def tiktok_offline(self, ctx: commands.Context, mode: str) -> None:
        """What happens when a stream ends: `new`, `edit` or `delete`."""
        mode = mode.lower()
        if mode not in ("new", "edit", "delete"):
            await ctx.send("Use `new`, `edit` or `delete`.")
            return
        await self.config.guild(ctx.guild).offline_post.set(mode)
        explained = {
            "new": "post a stream-ended embed and remove the live alert",
            "edit": "turn the live alert into the stream-ended embed",
            "delete": "delete the live alert silently",
        }[mode]
        await ctx.send(f"When a stream ends I will {explained}.")

    @tiktok.command(name="settings")
    async def tiktok_settings(self, ctx: commands.Context) -> None:
        """Show the current configuration."""
        data = await self.config.guild(ctx.guild).all()
        accounts = data.get("accounts") or {}
        without_channel = [
            handle
            for handle, raw in accounts.items()
            if not (raw or {}).get("channel_id")
        ]
        with_ping = []
        for handle, raw in accounts.items():
            mention = ping_mention(ctx.guild, (raw or {}).get("ping_role_id"))
            if mention:
                with_ping.append(f"`@{handle}` -> {mention}")
        lines = [
            f"**Enabled:** {bool(data.get('enabled', True))}",
            f"**Accounts:** {len(accounts)}",
            f"**Ping roles:** {', '.join(with_ping) if with_ping else 'none'}",
            "**Channel:** one per account (no guild-wide default)",
            f"**Accounts without a channel:** "
            f"{humanize_list(without_channel) if without_channel else 'none'}",
            f"**Live alerts:** {'on' if data.get('announce_live', True) else 'off'} "
            f"(every {data.get('check_interval')} s)",
            f"**Clip alerts:** {'on' if data.get('announce_content', True) else 'off'} "
            f"(every {data.get('content_interval')} s)",
            f"**Existing clips on add:** "
            f"{'announced' if data.get('announce_existing') else 'silent'}",
            f"**Viewer count shown:** {bool(data.get('show_viewers', True))}",
            f"**Live embed refresh:** "
            f"{'on' if data.get('edit_live_alerts', True) else 'off'} "
            f"(every {data.get('live_update_seconds')} s)",
            f"**Clip files (MP4):** "
            f"{'on' if data.get('announce_videos', True) else 'off'} "
            f"(max {data.get('video_max_mb')} MB)",
            f"**Stream end:** {data.get('offline_post')}",
            f"**Timezone offset:** UTC{int(data.get('timezone_offset') or 0):+d}",
        ]
        blocked = self._block_remaining()
        if blocked:
            lines.append(
                f"**Rate limited by TikTok:** {int(blocked)} s left "
                f"({self._block_reason or 'HTTP 403/429'})"
            )
        embed = discord.Embed(
            colour=discord.Colour(TIKTOK_COLOR),
            title="TikTokWatch settings",
            description="\n".join(lines),
            timestamp=datetime.now(timezone.utc),
        )
        embed.set_footer(text=f"TikTokWatch {self.__version__}")
        await ctx.send(embed=embed)

    @tiktok.command(name="check")
    async def tiktok_check(self, ctx: commands.Context, handle: str) -> None:
        """Probe TikTok for one handle right now."""
        cleaned = clean_handle(handle)
        if not cleaned:
            await ctx.send("That is not a TikTok handle.")
            return
        async with ctx.typing():
            try:
                info = await self.api.live_status(cleaned)
            except TikTokBlocked as exc:
                await self._register_block(exc)
                await ctx.send(
                    f"TikTok is refusing requests right now ({exc}). "
                    f"Next attempt in about {int(self._block_remaining())} s."
                )
                return
            except TikTokError as exc:
                await ctx.send(f"The live lookup failed: {exc}")
                return
            page: Optional[ClipPage] = None
            try:
                page = await self.api.clip_page(cleaned)
            except TikTokError:
                page = None
        settings = EmbedSettings.from_guild_config(
            await self.config.guild(ctx.guild).all()
        )
        await ctx.send(embed=build_status_embed(cleaned, info, page, settings))

    @tiktok.command(name="test")
    @checks.admin_or_permissions(manage_guild=True)
    async def tiktok_test(self, ctx: commands.Context, handle: str) -> None:
        """Post a sample live alert for an account (nothing is stored)."""
        cleaned = clean_handle(handle)
        if not cleaned:
            await ctx.send("That is not a TikTok handle.")
            return
        data = await self.config.guild(ctx.guild).all()
        settings = EmbedSettings.from_guild_config(data)
        accounts = data.get("accounts") or {}
        state = {**ACCOUNT_DEFAULTS, **(accounts.get(cleaned) or {})}
        channel = self._resolve_channel(
            ctx.guild, state.get("channel_id") or ctx.channel.id
        )
        if channel is None:
            await ctx.send("No usable channel for the test alert.")
            return
        try:
            info = await self.api.live_status(cleaned)
        except TikTokBlocked as exc:
            await self._register_block(exc)
            await ctx.send(f"TikTok is refusing requests right now ({exc}).")
            return
        except TikTokError as exc:
            await ctx.send(f"The live lookup failed: {exc}")
            return
        if info is None:
            info = LiveInfo(handle=cleaned, is_live=False)
        await ctx.send(
            content=self._alert_text(
                state, ctx.guild, self._live_values(info, cleaned)
            ),
            embed=build_live_embed(fake_live(info), settings),
            allowed_mentions=discord.AllowedMentions(
                roles=True,
                everyone=pings_everyone(ctx.guild, state.get("ping_role_id")),
            ),
        )

    @tiktok.command(name="debug")
    @checks.is_owner()
    async def tiktok_debug(self, ctx: commands.Context, handle: str) -> None:
        """Show what both endpoints answer for a handle (owner only)."""
        cleaned = clean_handle(handle)
        if not cleaned:
            await ctx.send("That is not a TikTok handle.")
            return
        info: Optional[LiveInfo] = None
        page: Optional[ClipPage] = None
        try:
            info = await self.api.live_status(cleaned)
            page = await self.api.clip_page(cleaned)
        except TikTokBlocked as exc:
            await self._register_block(exc)
            await ctx.send(f"Blocked: {exc}")
            return
        except TikTokError as exc:
            await ctx.send(f"Endpoint error: {exc}")
            return
        await ctx.send(embed=build_debug_embed(cleaned, info, page))

    @tiktok.command(name="force")
    @checks.is_owner()
    async def tiktok_force(self, ctx: commands.Context) -> None:
        """Run one live and one content pass immediately (owner only)."""
        async with ctx.typing():
            live = await self._sweep_live()
            content = await self._sweep_content()
        self._last_live_poll = datetime.now(timezone.utc)
        self._last_content_poll = self._last_live_poll
        await ctx.send(
            f"Live pass: {live['accounts']} accounts, {live['live']} live, "
            f"{live['alerts']} alerts.\n"
            f"Content pass: {content['accounts']} accounts, {content['new']} new clips."
        )

    # -- listeners ---------------------------------------------------------- #
    @commands.Cog.listener()
    async def on_guild_channel_delete(self, channel: discord.abc.GuildChannel) -> None:
        """Forget alert channels and message ids that no longer exist."""
        guild = getattr(channel, "guild", None)
        if guild is None:
            return
        data = await self.config.guild(guild).all()
        accounts = data.get("accounts") or {}
        for handle, raw in accounts.items():
            entry = {**ACCOUNT_DEFAULTS, **(raw or {})}
            if entry.get("channel_id") == channel.id:
                await self._store_account(guild, handle, channel_id=None)


async def setup(bot: Red) -> None:
    """Load the cog."""
    await bot.add_cog(TikTokWatch(bot))
