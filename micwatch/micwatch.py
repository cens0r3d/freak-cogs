"""MicWatch — move members that keep their microphone open (or talk) for too long.

Two detection modes
-------------------
``mic``
    Uses the regular gateway voice state (``self_mute`` / server mute).  Voice state updates
    are delivered for **every** voice channel of a guild, so this mode works without the bot
    ever joining a voice channel.  It measures "microphone is switched on", not "is talking".

``speak``
    Uses the voice gateway ``Speaking`` events (opcode 5).  Discord only relays those to
    clients that are connected to the *same* voice channel, so in this mode the bot has to
    sit in the watched channel.  This measures real voice activity (the green ring).

Both modes move the member to a configured target channel once the threshold is reached.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from dataclasses import dataclass
from typing import Any, Dict, Optional, Set, Union

import discord
from discord.ext import tasks
from redbot.core import Config, commands
from redbot.core.bot import Red
from redbot.core.utils.chat_formatting import bold, humanize_list, inline

try:  # voice transport encryption, needed to create a VoiceClient at all
    import nacl  # noqa: F401

    HAVE_NACL = True
except Exception:  # pragma: no cover - depends on the environment
    HAVE_NACL = False

try:  # discord.py 2.6+ also requires davey (DAVE/E2EE voice session)
    import davey  # noqa: F401

    HAVE_DAVEY = True
except Exception:  # pragma: no cover - depends on the environment
    HAVE_DAVEY = False

# Discord.py raises "…library needed in order to use voice" for either one missing, and the flags
# are frozen at import time - so a bot that installed them later needs a full restart, not a reload.
VOICE_READY = HAVE_NACL and HAVE_DAVEY

# discord.py advertises davey.DAVE_PROTOCOL_VERSION when it opens a voice connection, and Discord
# then runs the session end-to-end encrypted. Bots have been reported to stay connected but receive
# no media at all in such sessions, so `[p]micwatch dave` can advertise version 0 instead.
_DAVE_ORIGINAL = None
VOICE_HINT = 'pip install -U "Red-DiscordBot[voice]"  (or: [p]pipinstall pynacl>=1.5.0,<1.6 davey)'

log = logging.getLogger("red.freak_cogs.micwatch")

TICK_SECONDS = 2.0
MIN_THRESHOLD = 3.0
MAX_THRESHOLD = 3600.0

# Voice connection handling. Discord's UDP + websocket handshake happens *inside* the timeout
# passed to ``connect()``, so a too-small value force-disconnects a handshake that is still
# progressing ("Voice handshake complete" is logged before the socket part). discord.py's own
# default is 30 s; a failed attempt then gets a cool-down instead of a retry every tick.
# A deafened client receives no audio stream from Discord, and the relayed "speaking" events are
# tied to that stream, so the bot joins muted (never a speaker) but *not* deafened.
VOICE_SELF_DEAF = False
VOICE_SELF_MUTE = True
VOICE_CONNECT_TIMEOUT = 30.0
VOICE_RETRY_COOLDOWN = 15.0
VOICE_LINGER = 60.0  # stay connected while no watched channel has members
VOICE_DEAD_GRACE = 90.0  # a client that is not connected this long gets recreated
VOICE_FRAME_SILENCE = (
    30.0  # connected this long without a single voice frame = warn once
)

DEFAULT_MESSAGE = "{user} kept their microphone open for {seconds}s and was moved."

DEFAULT_GUILD = {
    "enabled": False,
    "mode": "mic",  # "mic" | "speak"
    "threshold": 30.0,  # seconds
    "grace": 2.0,  # "speak" mode: tolerated silence before the counter resets
    "rearm": 60.0,  # seconds before the same member can be moved again
    "reset_on_mute": True,  # "mic" mode: mute resets instead of pausing the counter
    "watch_channels": [],  # [] = all voice channels (only possible in "mic" mode)
    "target_channel": None,
    "immune_roles": [],
    "immune_users": [],
    "ignore_bots": True,
    "notify": True,
    "dave": True,
    "message": DEFAULT_MESSAGE,
    "reason": "Kept microphone open for too long",
}

CLEAR_WORDS = {"off", "none", "clear", "reset", "disable", "aus"}


@dataclass
class _Track:
    """Continuous microphone-on time of a member (``mic`` mode)."""

    channel_id: int
    since: Optional[float] = None
    acc: float = 0.0

    def elapsed(self, now: float) -> float:
        return self.acc + (now - self.since if self.since is not None else 0.0)


class MicWatch(commands.Cog):
    """Move members that keep their microphone open for too long."""

    __version__ = "1.0.0"

    def __init__(self, bot: Red) -> None:
        self.bot = bot
        self.config = Config.get_conf(
            self, identifier=73_445_902_118_05, force_registration=True
        )
        self.config.register_guild(**DEFAULT_GUILD)

        # guild_id -> member_id -> _Track            ("mic" mode)
        self._track: Dict[int, Dict[int, _Track]] = {}
        # guild_id -> member_id -> {"start", "end", "talking"}   ("speak" mode)
        self._speak: Dict[int, Dict[int, Dict[str, Any]]] = {}
        # guild_id -> member_id -> monotonic timestamp until the member is ignored again
        self._cooldown: Dict[int, Dict[int, float]] = {}
        self._grace_cache: Dict[int, float] = {}
        # guilds whose voice connection we opened ourselves
        self._joined: Set[int] = set()
        # guild_id -> channel_id that staff pinned with [p]micwatch join
        self._manual: Dict[int, int] = {}
        self._warned: Set[int] = set()
        # voice connection bookkeeping
        self._voice_tasks: Dict[int, asyncio.Task] = {}
        self._next_attempt: Dict[int, float] = {}
        self._last_voice_error: Dict[int, str] = {}
        self._empty_since: Dict[int, float] = {}
        self._dead_since: Dict[int, float] = {}
        # diagnostics for `[p]micwatch status`
        self._hook_frames: Dict[int, int] = (
            {}
        )  # every voice websocket frame our hook saw
        self._frames_by_op: Dict[int, Dict[int, int]] = {}  # guild -> opcode -> count
        self._frames: Dict[int, int] = {}  # relayed voice frames with opcode 5
        self._frames_with_user: Dict[int, int] = {}
        self._last_frame: Dict[int, float] = {}
        self._no_frame_since: Dict[int, float] = {}
        self._no_frame_warned: Set[int] = set()

        self._ticker.start()

    # ------------------------------------------------------------------ lifecycle

    def cog_unload(self) -> None:
        self._ticker.cancel()
        for task in self._voice_tasks.values():
            task.cancel()
        self._voice_tasks.clear()
        for guild_id in list(self._joined):
            guild = self.bot.get_guild(guild_id)
            vc = guild.voice_client if guild else None
            if vc is not None:
                self.bot.loop.create_task(vc.disconnect(force=True))
        self._joined.clear()
        self._track.clear()
        self._speak.clear()

    async def red_delete_data_for_user(self, *, requester=None, user_id: int) -> None:
        """Remove a user id from all immunity lists (end user data request)."""
        for guild in await self.config.all_guilds():
            data = await self.config.guild_from_id(guild).all()
            users = list(data.get("immune_users", []))
            if user_id in users:
                users.remove(user_id)
                await self.config.guild_from_id(guild).immune_users.set(users)

    # ------------------------------------------------------------------ helpers

    @staticmethod
    def _watched(conf: dict) -> Set[int]:
        return {int(c) for c in conf.get("watch_channels") or []}

    def _skip_reason(
        self, member: discord.Member, conf: dict, watched: Set[int]
    ) -> Optional[str]:
        """Why this member is not tracked right now, or None if nothing blocks them."""
        guild = member.guild
        if member.id == guild.me.id:
            return "me"
        if conf["ignore_bots"] and member.bot:
            return "bot"
        if member.id in (conf.get("immune_users") or []):
            return "immune (user)"
        if {r.id for r in member.roles} & {
            int(r) for r in (conf.get("immune_roles") or [])
        }:
            return "immune (role)"
        if member.id in self._cooldown.get(guild.id, {}):
            return "rearm cooldown"
        voice = member.voice
        if voice is None or voice.channel is None:
            return "not in voice"
        if watched and voice.channel.id not in watched:
            return "channel not watched"
        if member.top_role > guild.me.top_role:
            # Discord refuses moves for members whose highest role is *above* the bot's.
            # Equal positions (e.g. both only @everyone) are allowed, so compare strictly.
            return "higher role than mine"
        return None

    def _eligible(self, member: discord.Member, conf: dict, watched: Set[int]) -> bool:
        """Can/will this member be moved right now?"""
        return self._skip_reason(member, conf, watched) is None

    def _forget(self, guild_id: int, member_id: int) -> None:
        self._track.get(guild_id, {}).pop(member_id, None)
        self._speak.get(guild_id, {}).pop(member_id, None)

    def _purge(self, guild_id: int, now: float) -> None:
        cooldowns = self._cooldown.get(guild_id)
        if not cooldowns:
            return
        for member_id, until in list(cooldowns.items()):
            if until <= now:
                cooldowns.pop(member_id, None)

    async def _move(
        self,
        member: discord.Member,
        target: discord.VoiceChannel,
        conf: dict,
        elapsed: float,
        now: float,
    ) -> bool:
        guild = member.guild
        voice = member.voice
        if voice is None or voice.channel is None or voice.channel == target:
            self._forget(guild.id, member.id)
            return False
        try:
            await member.move_to(target, reason=conf["reason"])
        except discord.Forbidden:
            if guild.id not in self._warned:
                self._warned.add(guild.id)
                log.warning(
                    "MicWatch: Discord refused the move in guild %s (%s) - either the bot is "
                    "missing the 'Move Members' permission, or the member's highest role is not "
                    "below the bot's highest role.",
                    guild.id,
                    guild.name,
                )
            return False
        except (discord.HTTPException, asyncio.TimeoutError) as exc:
            log.warning("MicWatch: could not move %s in %s: %s", member, guild, exc)
            return False

        self._cooldown.setdefault(guild.id, {})[member.id] = now + float(conf["rearm"])
        self._forget(guild.id, member.id)
        log.info(
            "MicWatch: moved %s (%s) from %s to %s after %.0fs.",
            member,
            member.id,
            voice.channel,
            target,
            elapsed,
        )
        if conf["notify"]:
            text = (
                str(conf["message"])
                .replace("{user}", member.display_name)
                .replace("{mention}", member.mention)
                .replace("{seconds}", str(int(elapsed)))
                .replace("{channel}", voice.channel.name)
            )
            with contextlib.suppress(discord.HTTPException):
                await target.send(text)
        return True

    # ------------------------------------------------------------------ voice gateway hook ("speak" mode)

    def _record_speaking(self, guild_id: int, user_id: int, talking: bool) -> None:
        """Feed one relayed opcode 5 event into the burst tracker (sync, runs in the voice ws task)."""
        now = time.monotonic()
        guild_map = self._speak.setdefault(guild_id, {})
        burst = guild_map.get(user_id)
        if talking:
            grace = self._grace_cache.get(guild_id, 2.0)
            if burst is None or (
                burst["end"] is not None and now - burst["end"] > grace
            ):
                guild_map[user_id] = {"start": now, "end": None, "talking": True}
            else:
                burst["talking"] = True
                burst["end"] = None
        elif burst is not None:
            burst["talking"] = False
            burst["end"] = now

    def _handle_voice_payload(self, ws: Any, msg: Any) -> None:
        try:
            if not isinstance(msg, dict):
                return
            state = getattr(ws, "_connection", None)
            voice_client = getattr(state, "voice_client", None)
            guild = getattr(voice_client, "guild", None)
            if guild is None:
                return
            # Count every frame: heartbeat acks arrive regularly, so a non-zero total proves the
            # hook is alive, while op-5 staying at 0 means Discord relays no speaking events.
            self._hook_frames[guild.id] = self._hook_frames.get(guild.id, 0) + 1
            op = msg.get("op")
            by_op = self._frames_by_op.setdefault(guild.id, {})
            by_op[op] = by_op.get(op, 0) + 1
            if op != 5:
                return
            data = msg.get("d") or {}
            user_id = data.get("user_id")
            self._frames[guild.id] = self._frames.get(guild.id, 0) + 1
            self._last_frame[guild.id] = time.monotonic()
            if user_id is None:
                # Discord did not name the speaker: without a user id there is nothing to time
                return
            self._frames_with_user[guild.id] = (
                self._frames_with_user.get(guild.id, 0) + 1
            )
            speaking = int(data.get("speaking") or 0)
            self._record_speaking(guild.id, int(user_id), bool(speaking & 0b1))
        except Exception:
            log.exception("MicWatch: failed to handle a voice gateway payload.")

    async def _install_hook(self, guild: discord.Guild) -> bool:
        """Wrap the voice websocket hook so we see the relayed Speaking events.

        discord.py hands every voice gateway frame to ``DiscordVoiceWebSocket._hook``, and it
        does not handle opcode 5 itself, so wrapping that attribute is safe.
        """
        vc = guild.voice_client
        if vc is None:
            return False
        ws = getattr(vc, "ws", None)
        if ws is None:
            return False
        if getattr(ws, "_micwatch_hooked", False):
            return True
        original = getattr(ws, "_hook", None)

        async def _hook(ws_self, *args, **kwargs):
            try:
                if args:
                    self._handle_voice_payload(ws_self, args[0])
            except Exception:
                log.exception("MicWatch: voice hook failed.")
            if original is not None:
                await original(*args, **kwargs)

        ws._hook = _hook
        ws._micwatch_hooked = True
        _hook._micwatch = True
        try:
            # Registers our SSRC with the voice gateway with a "not speaking" frame. discord.py
            # does the same when playback ends, and the gateway expects at least one such frame.
            await ws.speak(discord.SpeakingState.none)
        except Exception:
            log.debug(
                "MicWatch: could not send the initial speaking frame.", exc_info=True
            )
        return True

    # ------------------------------------------------------------------ background work

    @tasks.loop(seconds=TICK_SECONDS)
    async def _ticker(self) -> None:
        if not self.bot.is_ready():
            return
        for guild in list(self.bot.guilds):
            try:
                await self._tick_guild(guild)
            except Exception:
                log.exception(
                    "MicWatch: tick failed for guild %s (%s).", guild.id, guild.name
                )

    @_ticker.before_loop
    async def _before_ticker(self) -> None:
        await self.bot.wait_until_ready()

    async def _tick_guild(self, guild: discord.Guild) -> None:
        conf = await self.config.guild(guild).all()
        self._grace_cache[guild.id] = float(conf["grace"])
        now = time.monotonic()

        if not conf["enabled"]:
            self._track.pop(guild.id, None)
            self._speak.pop(guild.id, None)
            task = self._voice_tasks.pop(guild.id, None)
            if task is not None:
                task.cancel()
            if guild.id in self._joined:
                await self._leave_voice(guild, reason="disabled")
            return

        self._purge(guild.id, now)
        watched = self._watched(conf)

        target = (
            guild.get_channel(conf["target_channel"])
            if conf["target_channel"]
            else None
        )

        if conf["mode"] == "speak":
            self._track.pop(guild.id, None)
            await self._sync_voice_connection(guild, conf, watched)
            self._check_frames(guild, now)
        else:
            self._speak.pop(guild.id, None)
            task = self._voice_tasks.pop(guild.id, None)
            if task is not None:
                task.cancel()
            if guild.id in self._joined:  # left over from a mode switch
                await self._leave_voice(guild, reason="mic mode needs no connection")
            self._sync_mic(guild, conf, watched, now)

        if target is None:
            return
        await self._enforce(guild, conf, watched, target, now)

    def _check_frames(self, guild: discord.Guild, now: float) -> None:
        """Warn once when the bot is connected but Discord relays nothing to it at all."""
        if guild.id not in self._joined or self._hook_frames.get(guild.id, 0):
            self._no_frame_since.pop(guild.id, None)
            return
        since = self._no_frame_since.setdefault(guild.id, now)
        if now - since >= VOICE_FRAME_SILENCE and guild.id not in self._no_frame_warned:
            self._no_frame_warned.add(guild.id)
            self._no_frame_since.pop(guild.id, None)
            log.warning(
                "MicWatch: connected to voice in %s for 30s but not one voice frame arrived - "
                "Discord relays nothing to this bot. Check that it sits in the channel where people "
                "talk (a pinned `micwatch join` channel wins over the watch list) and that it is not "
                "deafened; `mic` mode needs none of this.",
                guild,
            )

    def _sync_mic(
        self, guild: discord.Guild, conf: dict, watched: Set[int], now: float
    ) -> None:
        """Reconcile the tracked members with the live voice states (event independent)."""
        tracks = self._track.setdefault(guild.id, {})
        for channel in guild.voice_channels:
            if watched and channel.id not in watched:
                continue
            for member in channel.members:
                if not self._eligible(member, conf, watched):
                    tracks.pop(member.id, None)
                    continue
                voice = member.voice
                muted = bool(voice and (voice.self_mute or voice.mute))
                track = tracks.get(member.id)
                if muted:
                    if track is not None:
                        if conf["reset_on_mute"]:
                            track.acc = 0.0
                        elif track.since is not None:
                            track.acc += now - track.since
                        track.since = None
                    continue
                if track is None or track.channel_id != channel.id:
                    track = _Track(channel_id=channel.id)
                    tracks[member.id] = track
                if track.since is None:
                    track.since = now

        for member_id in list(tracks):
            member = guild.get_member(member_id)
            voice = member.voice if member is not None else None
            if member is None or voice is None or voice.channel is None:
                tracks.pop(member_id, None)
            elif watched and voice.channel.id not in watched:
                tracks.pop(member_id, None)

    async def _enforce(
        self,
        guild: discord.Guild,
        conf: dict,
        watched: Set[int],
        target: discord.VoiceChannel,
        now: float,
    ) -> None:
        threshold = float(conf["threshold"])
        if conf["mode"] == "mic":
            for member_id, track in list(self._track.get(guild.id, {}).items()):
                member = guild.get_member(member_id)
                if member is None or not self._eligible(member, conf, watched):
                    continue
                if track.since is None:
                    continue
                elapsed = track.elapsed(now)
                if elapsed >= threshold:
                    await self._move(member, target, conf, elapsed, now)
        else:
            speaks = self._speak.get(guild.id, {})
            grace = float(conf["grace"])
            for member_id, burst in list(speaks.items()):
                member = guild.get_member(member_id)
                if member is None or not self._eligible(member, conf, watched):
                    speaks.pop(member_id, None)
                    continue
                end = burst["end"]
                if not burst["talking"] and (end is None or now - end > grace):
                    speaks.pop(member_id, None)
                    continue
                elapsed = (now if burst["talking"] else end) - burst["start"]
                if elapsed >= threshold:
                    await self._move(member, target, conf, elapsed, now)

    # ------------------------------------------------------------------ voice connection management

    def _wanted_channel(
        self, guild: discord.Guild, conf: dict, watched: Set[int]
    ) -> Optional[discord.VoiceChannel]:
        manual = self._manual.get(guild.id)
        if manual:
            channel = guild.get_channel(manual)
            if isinstance(channel, discord.VoiceChannel):
                return channel
            self._manual.pop(guild.id, None)
        if not watched:
            return None
        best: Optional[discord.VoiceChannel] = None
        best_count = 0
        for channel in guild.voice_channels:
            if channel.id not in watched:
                continue
            count = sum(1 for m in channel.members if not m.bot and m.id != guild.me.id)
            if count > best_count:
                best, best_count = channel, count
        return best

    def _apply_dave_setting(self, wanted: bool) -> None:
        """Advertise (or not) the DAVE/E2EE voice session for connections opened from here on."""
        global _DAVE_ORIGINAL
        try:
            import davey
        except Exception:
            return
        version = getattr(davey, "DAVE_PROTOCOL_VERSION", None)
        if version is None:
            return
        if wanted:
            if _DAVE_ORIGINAL is not None:
                davey.DAVE_PROTOCOL_VERSION = _DAVE_ORIGINAL
                _DAVE_ORIGINAL = None
            return
        if _DAVE_ORIGINAL is None:
            _DAVE_ORIGINAL = version
        davey.DAVE_PROTOCOL_VERSION = 0

    async def _connect_voice(
        self, guild: discord.Guild, channel: discord.VoiceChannel, conf: dict
    ) -> bool:
        """Connect once, and remember the failure plus a cool-down instead of retrying every tick."""
        now = time.monotonic()
        self._apply_dave_setting(bool(conf.get("dave", True)))
        try:
            await channel.connect(
                timeout=VOICE_CONNECT_TIMEOUT,
                reconnect=True,
                self_deaf=VOICE_SELF_DEAF,
                self_mute=VOICE_SELF_MUTE,
            )
        except discord.ClientException as exc:  # another client got there first
            log.debug("MicWatch: %s already has a voice client: %s", guild, exc)
            return True
        except asyncio.TimeoutError:
            self._last_voice_error[guild.id] = (
                f"connect timed out after {VOICE_CONNECT_TIMEOUT:.0f}s"
            )
            self._next_attempt[guild.id] = now + VOICE_RETRY_COOLDOWN
            log.warning(
                "MicWatch: voice connect to %s in %s timed out after %.0fs (Discord's UDP and "
                "websocket handshake are inside that timeout) - next try in %.0fs.",
                channel,
                guild,
                VOICE_CONNECT_TIMEOUT,
                VOICE_RETRY_COOLDOWN,
            )
            return False
        except Exception as exc:
            self._last_voice_error[guild.id] = f"{type(exc).__name__}: {exc}"
            self._next_attempt[guild.id] = now + VOICE_RETRY_COOLDOWN
            log.warning(
                "MicWatch: could not join %s in %s: %s - next try in %.0fs.",
                channel,
                guild,
                exc,
                VOICE_RETRY_COOLDOWN,
            )
            return False
        self._joined.add(guild.id)
        self._no_frame_since[guild.id] = (
            now  # start of the "is anything arriving?" window
        )
        self._no_frame_warned.discard(guild.id)
        self._last_voice_error.pop(guild.id, None)
        self._next_attempt.pop(guild.id, None)
        self._empty_since.pop(guild.id, None)
        self._dead_since.pop(guild.id, None)
        log.info("MicWatch: joined %s in %s.", channel, guild)
        return True

    async def _leave_voice(self, guild: discord.Guild, *, reason: str) -> None:
        """Disconnect a connection we opened ourselves."""
        self._joined.discard(guild.id)
        self._speak.pop(guild.id, None)
        self._empty_since.pop(guild.id, None)
        self._dead_since.pop(guild.id, None)
        self._no_frame_since.pop(guild.id, None)
        vc = guild.voice_client
        if vc is None:
            return
        with contextlib.suppress(Exception):
            await vc.disconnect(force=True)
        log.info("MicWatch: left voice in %s (%s).", guild, reason)

    async def _sync_voice_connection(
        self, guild: discord.Guild, conf: dict, watched: Set[int]
    ) -> None:
        now = time.monotonic()
        task = self._voice_tasks.get(guild.id)
        if task is not None and task.done():
            self._voice_tasks.pop(guild.id, None)
            with contextlib.suppress(asyncio.CancelledError, Exception):
                task.result()
        connecting = guild.id in self._voice_tasks

        wanted = self._wanted_channel(guild, conf, watched)
        vc = guild.voice_client

        if wanted is None:
            if connecting:
                return
            empty_since = self._empty_since.setdefault(guild.id, now)
            if vc is not None and now - empty_since < VOICE_LINGER:
                # keep the connection for a moment: people hop between channels all the time
                if vc.is_connected():
                    await self._install_hook(guild)
                return
            if vc is not None and guild.id in self._joined:
                await self._leave_voice(guild, reason="no watched channel has members")
            else:
                self._speak.pop(guild.id, None)
                self._empty_since.pop(guild.id, None)
            return

        self._empty_since.pop(guild.id, None)
        if not VOICE_READY:
            return

        if vc is not None:
            # discord.py registers a client *before* the handshake finishes, so an existing
            # client may still be connecting: never start a second one (that raises
            # ClientException) and never tear one down - its own reconnect flow owns the socket.
            if not vc.is_connected():
                dead_since = self._dead_since.setdefault(guild.id, now)
                if now - dead_since > VOICE_DEAD_GRACE:
                    log.warning(
                        "MicWatch: voice client in %s has not been connected for %.0fs - "
                        "recreating it.",
                        guild,
                        now - dead_since,
                    )
                    await self._leave_voice(guild, reason="stale client")
                    self._next_attempt[guild.id] = now + 5.0
                return
            self._dead_since.pop(guild.id, None)
            if vc.channel != wanted:
                try:
                    await vc.move_to(wanted)
                except Exception as exc:
                    log.warning(
                        "MicWatch: could not move to %s in %s: %s", wanted, guild, exc
                    )
                    return
            await self._install_hook(guild)
            return

        if connecting or now < self._next_attempt.get(guild.id, 0.0):
            return
        self._voice_tasks[guild.id] = self.bot.loop.create_task(
            self._connect_voice(guild, wanted, conf),
            name=f"micwatch-voice-{guild.id}",
        )

    # ------------------------------------------------------------------ listeners

    @commands.Cog.listener()
    async def on_voice_state_update(
        self,
        member: discord.Member,
        before: discord.VoiceState,
        after: discord.VoiceState,
    ) -> None:
        """React to joins/leaves and mute changes; the ticker does the counting."""
        guild = member.guild
        if guild is None:
            return
        conf = await self.config.guild(guild).all()
        if not conf["enabled"] or after.channel is None:
            self._forget(guild.id, member.id)
            return
        watched = self._watched(conf)

        if conf["mode"] == "speak":
            if before.channel != after.channel:
                self._forget(guild.id, member.id)
            return

        if not self._eligible(member, conf, watched):
            self._forget(guild.id, member.id)
            return

        now = time.monotonic()
        if before.channel != after.channel:
            self._forget(guild.id, member.id)
        tracks = self._track.setdefault(guild.id, {})
        track = tracks.get(member.id)
        if track is None or track.channel_id != after.channel.id:
            track = _Track(channel_id=after.channel.id)
            tracks[member.id] = track

        muted = bool(after.self_mute or after.mute)
        if muted:
            if conf["reset_on_mute"]:
                track.acc = 0.0
            elif track.since is not None:
                track.acc += now - track.since
            track.since = None
        elif track.since is None:
            track.since = now

    # ------------------------------------------------------------------ commands

    @commands.group(name="micwatch", aliases=["micmon", "mwatch"])
    @commands.guild_only()
    @commands.admin_or_permissions(move_members=True)
    async def micwatch(self, ctx: commands.Context) -> None:
        """Move members that keep their mic open for too long to another channel (use `settings`)."""

    @micwatch.command(name="toggle")
    async def mw_toggle(self, ctx: commands.Context) -> None:
        """Enable or disable the whole thing."""
        conf = await self.config.guild(ctx.guild).all()
        new = not conf["enabled"]
        await self.config.guild(ctx.guild).enabled.set(new)
        if not new:
            self._track.pop(ctx.guild.id, None)
            self._speak.pop(ctx.guild.id, None)
        problems = self._problems(conf, enabling=new)
        message = f"MicWatch is now {bold('enabled' if new else 'disabled')}."
        if problems:
            message += "\n" + "\n".join(f"- {p}" for p in problems)
        await ctx.send(message)

    @micwatch.command(name="mode")
    async def mw_mode(self, ctx: commands.Context, mode: Optional[str] = None) -> None:
        """Show/set the detection mode: `mic` (mic switched on) or `speak` (really talking)."""
        conf = await self.config.guild(ctx.guild).all()
        if mode is None:
            await ctx.send(f"Detection mode: {inline(conf['mode'])}")
            return
        mode = mode.lower().strip()
        if mode not in ("mic", "speak"):
            await ctx.send("Mode must be `mic` or `speak`.")
            return
        await self.config.guild(ctx.guild).mode.set(mode)
        conf["mode"] = mode
        notes = []
        if mode == "speak":
            if not conf["watch_channels"]:
                notes.append(
                    "`speak` mode watches specific channels only — add them with `watch add`."
                )
            if not VOICE_READY:
                notes.append(f"Voice libraries missing — {VOICE_HINT}.")
        await ctx.send(
            f"Mode set to {inline(mode)}."
            + ("\n" + "\n".join(f"- {n}" for n in notes) if notes else "")
        )

    @micwatch.command(name="threshold")
    async def mw_threshold(
        self, ctx: commands.Context, seconds: Optional[float] = None
    ) -> None:
        """Seconds the mic may stay open before the member gets moved (default 30)."""
        if seconds is None:
            current = await self.config.guild(ctx.guild).threshold()
            await ctx.send(f"Threshold: {inline(f'{current:g}s')}")
            return
        if not MIN_THRESHOLD <= seconds <= MAX_THRESHOLD:
            await ctx.send(
                f"Give a value between {MIN_THRESHOLD:g} and {MAX_THRESHOLD:g} seconds."
            )
            return
        await self.config.guild(ctx.guild).threshold.set(float(seconds))
        await ctx.send(f"Threshold set to {inline(f'{seconds:g}s')}.")

    @micwatch.command(name="grace")
    async def mw_grace(
        self, ctx: commands.Context, seconds: Optional[float] = None
    ) -> None:
        """`speak` mode: silence tolerated before the counter resets (default 2)."""
        if seconds is None:
            current = await self.config.guild(ctx.guild).grace()
            await ctx.send(f"Grace: {inline(f'{current:g}s')}")
            return
        if not 0 <= seconds <= 60:
            await ctx.send("Give a value between 0 and 60 seconds.")
            return
        await self.config.guild(ctx.guild).grace.set(float(seconds))
        self._grace_cache[ctx.guild.id] = float(seconds)
        await ctx.send(f"Grace set to {inline(f'{seconds:g}s')}.")

    @micwatch.command(name="rearm")
    async def mw_rearm(
        self, ctx: commands.Context, seconds: Optional[float] = None
    ) -> None:
        """Seconds a member is left alone after being moved (default 60)."""
        if seconds is None:
            current = await self.config.guild(ctx.guild).rearm()
            await ctx.send(f"Rearm time: {inline(f'{current:g}s')}")
            return
        if not 0 <= seconds <= 3600:
            await ctx.send("Give a value between 0 and 3600 seconds.")
            return
        await self.config.guild(ctx.guild).rearm.set(float(seconds))
        await ctx.send(f"Rearm time set to {inline(f'{seconds:g}s')}.")

    @micwatch.command(name="target")
    async def mw_target(
        self, ctx: commands.Context, *, value: Optional[str] = None
    ) -> None:
        """Set/show the channel members are moved to (`target off` to clear it)."""
        current = await self.config.guild(ctx.guild).target_channel()
        if value is None:
            channel = ctx.guild.get_channel(current) if current else None
            await ctx.send(
                f"Target channel: {channel.mention if channel else bold('not set')}"
            )
            return
        if value.lower().strip() in CLEAR_WORDS:
            await self.config.guild(ctx.guild).target_channel.set(None)
            await ctx.send("Target channel cleared.")
            return
        try:
            channel = await commands.VoiceChannelConverter().convert(ctx, value)
        except commands.BadArgument:
            await ctx.send(
                "That is not a voice channel of this server (or `off` to clear)."
            )
            return
        await self.config.guild(ctx.guild).target_channel.set(channel.id)
        await ctx.send(f"Target channel set to {channel.mention}.")

    @micwatch.group(name="watch", invoke_without_command=True)
    async def mw_watch(self, ctx: commands.Context) -> None:
        """Channels that are monitored (empty = all, `mic` mode only)."""
        await ctx.send_help(ctx.command)

    @mw_watch.command(name="add")
    async def mw_watch_add(
        self, ctx: commands.Context, channel: discord.VoiceChannel
    ) -> None:
        """Add a voice channel to the watch list."""
        async with self.config.guild(ctx.guild).watch_channels() as channels:
            if channel.id in channels:
                await ctx.send(f"{channel.mention} is already watched.")
                return
            channels.append(channel.id)
        await ctx.send(f"Now watching {channel.mention}.")

    @mw_watch.command(name="remove")
    async def mw_watch_remove(
        self, ctx: commands.Context, channel: discord.VoiceChannel
    ) -> None:
        """Remove a voice channel from the watch list."""
        async with self.config.guild(ctx.guild).watch_channels() as channels:
            if channel.id not in channels:
                await ctx.send(f"{channel.mention} is not watched.")
                return
            channels.remove(channel.id)
        self._forget_channel(ctx.guild, channel.id)
        await ctx.send(f"No longer watching {channel.mention}.")

    @mw_watch.command(name="list")
    async def mw_watch_list(self, ctx: commands.Context) -> None:
        """List the watched voice channels."""
        channels = await self.config.guild(ctx.guild).watch_channels()
        if not channels:
            await ctx.send(
                "Watch list is empty — every voice channel counts (`mic` mode)."
            )
            return
        names = []
        for channel_id in channels:
            channel = ctx.guild.get_channel(channel_id)
            names.append(
                channel.mention if channel else f"deleted channel ({channel_id})"
            )
        await ctx.send("Watched channels: " + humanize_list(names))

    @mw_watch.command(name="clear")
    async def mw_watch_clear(self, ctx: commands.Context) -> None:
        """Monitor every voice channel again."""
        await self.config.guild(ctx.guild).watch_channels.set([])
        self._track.pop(ctx.guild.id, None)
        await ctx.send("Watch list cleared.")

    @micwatch.group(name="immune", invoke_without_command=True)
    async def mw_immune(self, ctx: commands.Context) -> None:
        """Roles/members that are never moved."""
        await ctx.send_help(ctx.command)

    @mw_immune.command(name="add")
    async def mw_immune_add(
        self, ctx: commands.Context, role_or_member: Union[discord.Role, discord.Member]
    ) -> None:
        """Never move this role or member."""
        if isinstance(role_or_member, discord.Role):
            async with self.config.guild(ctx.guild).immune_roles() as roles:
                if role_or_member.id in roles:
                    await ctx.send(f"{role_or_member.mention} is already immune.")
                    return
                roles.append(role_or_member.id)
            await ctx.send(f"{role_or_member.mention} is now immune.")
        else:
            async with self.config.guild(ctx.guild).immune_users() as users:
                if role_or_member.id in users:
                    await ctx.send(f"{role_or_member.mention} is already immune.")
                    return
                users.append(role_or_member.id)
            self._forget(ctx.guild.id, role_or_member.id)
            await ctx.send(f"{role_or_member.mention} is now immune.")

    @mw_immune.command(name="remove")
    async def mw_immune_remove(
        self, ctx: commands.Context, role_or_member: Union[discord.Role, discord.Member]
    ) -> None:
        """Remove a role or member from the immunity list."""
        if isinstance(role_or_member, discord.Role):
            async with self.config.guild(ctx.guild).immune_roles() as roles:
                if role_or_member.id not in roles:
                    await ctx.send(f"{role_or_member.mention} is not immune.")
                    return
                roles.remove(role_or_member.id)
            await ctx.send(f"{role_or_member.mention} is no longer immune.")
        else:
            async with self.config.guild(ctx.guild).immune_users() as users:
                if role_or_member.id not in users:
                    await ctx.send(f"{role_or_member.mention} is not immune.")
                    return
                users.remove(role_or_member.id)
            await ctx.send(f"{role_or_member.mention} is no longer immune.")

    @mw_immune.command(name="list")
    async def mw_immune_list(self, ctx: commands.Context) -> None:
        """List immune roles and members."""
        roles = await self.config.guild(ctx.guild).immune_roles()
        users = await self.config.guild(ctx.guild).immune_users()
        role_names = [r.mention for r in ctx.guild.roles if r.id in roles] or ["none"]
        user_names = []
        for user_id in users:
            member = ctx.guild.get_member(user_id)
            user_names.append(member.mention if member else f"left member ({user_id})")
        await ctx.send(
            f"Immune roles: {humanize_list(role_names)}\nImmune members: {humanize_list(user_names) or 'none'}"
        )

    @mw_immune.command(name="clear")
    async def mw_immune_clear(self, ctx: commands.Context) -> None:
        """Clear both immunity lists."""
        await self.config.guild(ctx.guild).immune_roles.set([])
        await self.config.guild(ctx.guild).immune_users.set([])
        await ctx.send("Immunity lists cleared.")

    @micwatch.command(name="notify")
    async def mw_notify(self, ctx: commands.Context) -> None:
        """Toggle the announcement in the target channel."""
        new = not await self.config.guild(ctx.guild).notify()
        await self.config.guild(ctx.guild).notify.set(new)
        await ctx.send(f"Announcements are now {bold('on' if new else 'off')}.")

    @micwatch.command(name="resetmute")
    async def mw_reset_on_mute(self, ctx: commands.Context) -> None:
        """`mic` mode: toggle whether muting resets (on) or pauses (off) the counter."""
        new = not await self.config.guild(ctx.guild).reset_on_mute()
        await self.config.guild(ctx.guild).reset_on_mute.set(new)
        await ctx.send(
            f"Muting now {'resets' if new else 'pauses'} the counter (reset_on_mute = {bold(str(new))})."
        )

    @micwatch.command(name="ignorebots", aliases=["bots"])
    async def mw_ignore_bots(self, ctx: commands.Context) -> None:
        """Toggle whether other bots are ignored (default: on)."""
        new = not await self.config.guild(ctx.guild).ignore_bots()
        await self.config.guild(ctx.guild).ignore_bots.set(new)
        await ctx.send(f"Bots are now {bold('ignored' if new else 'tracked')}.")

    @micwatch.command(name="message")
    async def mw_message(
        self, ctx: commands.Context, *, text: Optional[str] = None
    ) -> None:
        """Text sent to the target channel. Placeholders: {user} {mention} {seconds} {channel}."""
        if text is None:
            current = await self.config.guild(ctx.guild).message()
            await ctx.send(f"Current message: {inline(current)}")
            return
        if len(text) > 500:
            await ctx.send(f"Too long ({len(text)}/500).")
            return
        await self.config.guild(ctx.guild).message.set(text)
        await ctx.send(f"Message set to: {inline(text)}")

    @micwatch.command(name="join")
    async def mw_join(
        self, ctx: commands.Context, channel: Optional[discord.VoiceChannel] = None
    ) -> None:
        """Make the bot sit in a channel (required for `speak` mode)."""
        if not VOICE_READY:
            missing = ", ".join(
                name
                for name, ok in (("pynacl", HAVE_NACL), ("davey", HAVE_DAVEY))
                if not ok
            )
            await ctx.send(
                f"Voice libraries missing ({missing}) — {VOICE_HINT}. Afterwards **restart** the "
                "bot: discord.py decides at import time whether voice is available."
            )
            return
        channel = channel or (ctx.author.voice.channel if ctx.author.voice else None)
        if channel is None:
            await ctx.send("Give a voice channel or join one yourself.")
            return
        self._manual[ctx.guild.id] = channel.id
        conf = await self.config.guild(ctx.guild).all()
        current = ctx.guild.voice_client
        if current is not None:
            if not current.is_connected():
                await ctx.send(
                    "I still have a voice client for this server that is connecting/dead — "
                    "`leave` first, or wait for the cool-down, I retry on my own."
                )
                return
            if current.channel != channel:
                try:
                    await current.move_to(channel)
                except Exception as exc:
                    await ctx.send(f"Could not move there: {exc}")
                    return
            await self._install_hook(ctx.guild)
            await ctx.send(
                f"Sitting in {channel.mention} — pinned until `{ctx.clean_prefix}micwatch leave`."
            )
            return
        task = self._voice_tasks.pop(ctx.guild.id, None)
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        if await self._connect_voice(ctx.guild, channel, conf):
            await self._install_hook(ctx.guild)
            await ctx.send(
                f"Joined {channel.mention} — pinned until `{ctx.clean_prefix}micwatch leave`."
            )
            return
        reason = self._last_voice_error.get(ctx.guild.id, "unknown error")
        await ctx.send(
            f"Could not join {channel.mention}: {reason}. I retry every "
            f"{VOICE_RETRY_COOLDOWN:.0f}s while `speak` mode is on — details: "
            f"`{ctx.clean_prefix}micwatch status`."
        )

    @micwatch.command(name="leave")
    async def mw_leave(self, ctx: commands.Context) -> None:
        """Disconnect the bot and drop the manual channel pin."""
        self._manual.pop(ctx.guild.id, None)
        task = self._voice_tasks.pop(ctx.guild.id, None)
        if task is not None:
            task.cancel()
        if ctx.guild.voice_client is None:
            await ctx.send("I am not in a voice channel.")
            return
        await self._leave_voice(ctx.guild, reason="asked to leave")
        await ctx.send("Disconnected.")

    @micwatch.command(name="dave")
    async def mw_dave(self, ctx: commands.Context) -> None:
        """Experimental: toggle the DAVE/E2EE voice session (needs a fresh voice connection)."""
        new = not await self.config.guild(ctx.guild).dave()
        await self.config.guild(ctx.guild).dave.set(new)
        if new:
            self._apply_dave_setting(True)
            await ctx.send(
                "DAVE/E2EE sessions are **on** again (discord.py default). "
                f"Rejoin so it takes effect: `{ctx.clean_prefix}micwatch leave` then `join`."
            )
            return
        self._apply_dave_setting(False)
        await ctx.send(
            "DAVE/E2EE sessions are now **off** for voice connections opened from here on — this is "
            "a diagnostic: some bots stay connected in E2EE sessions but receive no media or "
            "speaking events at all. It affects the whole process (other cogs' voice connections "
            "too), and only new connections: "
            f"`{ctx.clean_prefix}micwatch leave` then `{ctx.clean_prefix}micwatch join`."
        )

    @micwatch.command(name="settings", aliases=["show", "config"])
    async def mw_settings(self, ctx: commands.Context) -> None:
        """Show the current configuration."""
        conf = await self.config.guild(ctx.guild).all()
        target = (
            ctx.guild.get_channel(conf["target_channel"])
            if conf["target_channel"]
            else None
        )
        watched = self._watched(conf)
        watched_text = (
            humanize_list(
                [
                    (
                        ctx.guild.get_channel(c).mention
                        if ctx.guild.get_channel(c)
                        else f"deleted ({c})"
                    )
                    for c in sorted(watched)
                ]
            )
            if watched
            else "all voice channels"
        )
        embed = discord.Embed(title="MicWatch", color=await ctx.embed_color())
        embed.add_field(name="Enabled", value=str(conf["enabled"]))
        embed.add_field(name="Mode", value=conf["mode"])
        embed.add_field(name="Threshold", value=f"{conf['threshold']:g}s")
        embed.add_field(name="Grace", value=f"{conf['grace']:g}s")
        embed.add_field(name="Rearm", value=f"{conf['rearm']:g}s")
        embed.add_field(name="Target", value=target.mention if target else "not set")
        embed.add_field(name="Watched", value=watched_text, inline=False)
        embed.add_field(name="Reset on mute", value=str(conf["reset_on_mute"]))
        embed.add_field(name="Ignore bots", value=str(conf["ignore_bots"]))
        embed.add_field(name="Notify", value=str(conf["notify"]))
        embed.add_field(name="Message", value=conf["message"], inline=False)
        problems = self._problems(conf, enabling=conf["enabled"])
        if problems:
            embed.add_field(
                name="Issues", value="\n".join(f"- {p}" for p in problems), inline=False
            )
        await ctx.send(embed=embed)

    @micwatch.command(name="status", aliases=["debug"])
    async def mw_status(self, ctx: commands.Context) -> None:
        """Show what is being tracked right now."""
        conf = await self.config.guild(ctx.guild).all()
        now = time.monotonic()
        lines = []
        if conf["mode"] == "mic":
            for member_id, track in sorted(
                self._track.get(ctx.guild.id, {}).items(),
                key=lambda kv: -kv[1].elapsed(now),
            ):
                member = ctx.guild.get_member(member_id)
                if member is None:
                    continue
                state = "muted/paused" if track.since is None else "counting"
                lines.append(f"{member} — {track.elapsed(now):.1f}s ({state})")
        else:
            for member_id, burst in sorted(
                self._speak.get(ctx.guild.id, {}).items(),
                key=lambda kv: -((now - kv[1]["start"])),
            ):
                member = ctx.guild.get_member(member_id)
                if member is None:
                    continue
                end = burst["end"]
                state = "talking" if burst["talking"] else "silent"
                seconds = (now if burst["talking"] else end) - burst["start"]
                lines.append(f"{member} — {seconds:.1f}s ({state})")

        vc = ctx.guild.voice_client
        embed = discord.Embed(
            title="MicWatch status",
            description="\n".join(lines) or "nothing tracked",
            color=await ctx.embed_color(),
        )
        embed.add_field(name="Mode", value=conf["mode"])
        embed.add_field(
            name="Voice connection",
            value=(
                f"{vc.channel.mention} ({'mine' if ctx.guild.id in self._joined else 'foreign'})"
                if vc
                else "none"
            ),
        )
        embed.add_field(
            name="Move permission",
            value=str(ctx.guild.me.guild_permissions.move_members),
        )
        embed.add_field(
            name="Voice libraries",
            value=f"pynacl: {HAVE_NACL} | davey: {HAVE_DAVEY}",
        )
        embed.add_field(name="DAVE/E2EE", value=str(conf.get("dave", True)))

        total = self._hook_frames.get(ctx.guild.id, 0)
        frames = self._frames.get(ctx.guild.id, 0)
        named = self._frames_with_user.get(ctx.guild.id, 0)
        last = self._last_frame.get(ctx.guild.id)
        embed.add_field(
            name="Voice frames",
            value=(
                f"{total} total, {frames} speaking ({named} with a user id)"
                + (f", last one {now - last:.0f}s ago" if last else "")
            ),
        )
        ws = getattr(vc, "ws", None) if vc is not None else None
        hook = getattr(ws, "_hook", None)
        hooked = bool(getattr(hook, "_micwatch", False))
        embed.add_field(
            name="Voice hook",
            value=(
                "mine" if hooked else ("foreign" if ws is not None else "no websocket")
            ),
        )
        by_op = self._frames_by_op.get(ctx.guild.id, {})
        if by_op:
            embed.add_field(
                name="Frames by opcode",
                value=", ".join(f"op{k}: {v}" for k, v in sorted(by_op.items())),
                inline=False,
            )
        own = ctx.guild.me.voice
        if own is not None:
            embed.add_field(
                name="My voice state",
                value=f"self_mute: {own.self_mute} | self_deaf: {own.self_deaf}",
            )
        if ctx.guild.id in self._next_attempt:
            embed.add_field(
                name="Voice retry",
                value=f"in {max(0.0, self._next_attempt[ctx.guild.id] - now):.0f}s",
            )
        if ctx.guild.id in self._last_voice_error:
            embed.add_field(
                name="Last voice error",
                value=self._last_voice_error[ctx.guild.id],
                inline=False,
            )

        watched = self._watched(conf)
        here = getattr(vc, "channel", None) if vc is not None else None
        overview = []
        for channel in ctx.guild.voice_channels:
            if watched and channel.id not in watched:
                continue
            humans = sum(1 for member in channel.members if not member.bot)
            overview.append(
                f"{channel.mention}: {humans} human(s)"
                + (" ← bot" if channel == here else "")
            )
        if overview:
            embed.add_field(
                name="Watched channels", value="\n".join(overview)[:1024], inline=False
            )
        blocked = []
        for channel in ctx.guild.voice_channels:
            if watched and channel.id not in watched:
                continue
            for member in channel.members:
                reason = self._skip_reason(member, conf, watched)
                if reason:
                    blocked.append(f"{member} — {reason}")
        if blocked:
            embed.add_field(
                name="Not monitored",
                value="\n".join(blocked[:10])[:1024],
                inline=False,
            )
        await ctx.send(embed=embed)

    @micwatch.command(name="reset")
    async def mw_reset(self, ctx: commands.Context) -> None:
        """Reset every setting of this server to the defaults."""
        self._apply_dave_setting(True)
        await self.config.guild(ctx.guild).clear()
        self._track.pop(ctx.guild.id, None)
        self._speak.pop(ctx.guild.id, None)
        self._cooldown.pop(ctx.guild.id, None)
        self._manual.pop(ctx.guild.id, None)
        await ctx.send("Settings reset to defaults.")

    # ------------------------------------------------------------------ misc helpers for commands

    def _forget_channel(self, guild: discord.Guild, channel_id: int) -> None:
        for member_id, track in list(self._track.get(guild.id, {}).items()):
            if track.channel_id == channel_id:
                self._track[guild.id].pop(member_id, None)

    def _problems(self, conf: dict, *, enabling: bool) -> list:
        problems = []
        if not enabling:
            return problems
        if not conf["target_channel"]:
            problems.append("No target channel set (`target #channel`).")
        elif conf["target_channel"] in (conf["watch_channels"] or []):
            problems.append(
                "The target channel is also watched — that causes an endless loop."
            )
        if conf["mode"] == "speak":
            if not conf["watch_channels"]:
                problems.append(
                    "`speak` mode needs an explicit watch list (`watch add #channel`)."
                )
            if not VOICE_READY:
                problems.append(f"Voice libraries missing — {VOICE_HINT}.")
        return problems
