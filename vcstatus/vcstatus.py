"""VCStatus — Automatic voice channel status on member join.

Facts this cog is built around:

* The status route is ``PUT /channels/{id}/voice-status`` and its ``status``
  field is nullable, so ``null`` clears the status.  discord.py has no
  ``voice_status`` attribute and Discord offers no way to *read* a status back,
  so the bot's own cache is the only record of what it set.
* The route is rate limited per route, so a burst of joins/leaves/moves is
  coalesced instead of firing one request each.
* It needs ``SET_VOICE_CHANNEL_STATUS``, plus ``MANAGE_CHANNELS`` while the bot
  is not connected to the channel.
"""

import asyncio
import logging
from typing import Optional

import discord
from discord.http import Route
from redbot.core import Config, commands
from redbot.core.bot import Red
from redbot.core.utils.chat_formatting import bold, inline

log = logging.getLogger("red.vcstatus")

# Wait this long for further changes before pushing an update.  Coalesces
# bursts and lets the voice state settle first.
DEBOUNCE_SECONDS = 2.0
# Spacing between requests when re-pushing every channel at once.
BULK_DELAY_SECONDS = 0.25

# Distinguishes "we never set anything here" from "we set it to nothing".
UNKNOWN = object()


class VCStatus(commands.Cog):

    def __init__(self, bot: Red) -> None:
        self.bot = bot
        # Last status we successfully pushed, per channel id.  A value of None
        # means "pushed a clear".
        self._status_cache: dict[int, Optional[str]] = {}
        # In-flight debounce tasks, per channel id.
        self._pending: dict[int, asyncio.Task] = {}
        self.config = Config.get_conf(
            self, identifier=23_593_420_582_31, force_registration=True
        )
        self.config.register_guild(
            enabled=False,
            default_message="{count} connected",
            clear_when_empty=False,
            channels={},
        )

    async def cog_unload(self) -> None:
        """Cancel queued updates so nothing fires after an unload."""
        for task in self._pending.values():
            task.cancel()
        self._pending.clear()

    # ------------------------------------------------------------------
    # Commands
    # ------------------------------------------------------------------

    @commands.group(name="vcstatus")
    @commands.guild_only()
    @commands.admin_or_permissions(manage_channels=True)
    async def vcstatus(self, ctx: commands.Context) -> None:
        """Manage the automatic voice-channel status feature."""

    @vcstatus.command(name="toggle")
    async def toggle(self, ctx: commands.Context) -> None:
        """Enable or disable automatic status updates."""
        current = await self.config.guild(ctx.guild).enabled()
        new_state = not current
        await self.config.guild(ctx.guild).enabled.set(new_state)

        await ctx.send(
            f"Auto voice status is now "
            f"{bold('enabled') if new_state else bold('disabled')}."
        )

        if new_state:
            await self._apply_status_to_all(ctx.guild)

    @vcstatus.command(name="empty", aliases=["clearempty"])
    async def toggle_empty(self, ctx: commands.Context) -> None:
        """Toggle clearing the status while a voice channel is empty.

        Enabled: a channel with nobody in it gets no status at all.
        Disabled: it shows the configured message with a count of 0.
        """
        current = await self.config.guild(ctx.guild).clear_when_empty()
        new_state = not current
        await self.config.guild(ctx.guild).clear_when_empty.set(new_state)

        await ctx.send(
            "Empty voice channels will now "
            + (
                "have their status cleared."
                if new_state
                else "keep showing the message."
            )
        )

        if await self.config.guild(ctx.guild).enabled():
            await self._apply_status_to_all(ctx.guild, force=True)

    @vcstatus.command(name="resync")
    async def resync(self, ctx: commands.Context) -> None:
        """Force a re-push of the status to every voice channel.

        Clears the internal cache first, so the request is actually sent even
        if the bot thinks the status is already correct.
        """
        if not await self.config.guild(ctx.guild).enabled():
            await ctx.send("Auto voice status is disabled, nothing to resync.")
            return

        async with ctx.typing():
            await self._apply_status_to_all(ctx.guild, force=True)

        await ctx.send("Re-pushed the status to every voice channel.")

    @vcstatus.command(name="default")
    async def set_default(self, ctx: commands.Context, *, message: str) -> None:
        """Set the default status message.

        Use ``{count}`` as a placeholder for the member count.
        Maximum **500 characters**.

        Example: ``[p]vcstatus default {count} people here``
        """
        if len(message) > 500:
            await ctx.send(f"Message too long ({len(message)}/500).")
            return

        await self.config.guild(ctx.guild).default_message.set(message)
        await ctx.send(f"Default status message set to:\n{inline(message)}")

        if await self.config.guild(ctx.guild).enabled():
            await self._apply_status_to_all(ctx.guild, force=True)

    @vcstatus.command(name="channel")
    async def set_channel(
        self,
        ctx: commands.Context,
        channel: discord.VoiceChannel,
        *,
        message: str,
    ) -> None:
        """Set a custom status message for a specific voice channel.

        Use ``{count}`` as a placeholder for the member count.
        Maximum **500 characters**.

        Example: ``[p]vcstatus channel #general {count} playing``
        """
        if channel.guild != ctx.guild:
            await ctx.send("That channel is not in this server.")
            return

        if len(message) > 500:
            await ctx.send(f"Message too long ({len(message)}/500).")
            return

        async with self.config.guild(ctx.guild).channels() as channels:
            channels[str(channel.id)] = message

        await ctx.send(f"Status for {channel.mention} set to:\n{inline(message)}")

        if await self.config.guild(ctx.guild).enabled():
            await self._apply_status_to_all(ctx.guild, force=True)

    @vcstatus.command(name="remove", aliases=["clear", "reset"])
    async def remove_message(
        self,
        ctx: commands.Context,
        channel: Optional[discord.VoiceChannel] = None,
    ) -> None:
        """Remove a per-channel message override, or reset everything."""
        if channel is not None:
            async with self.config.guild(ctx.guild).channels() as channels:
                key = str(channel.id)
                if key in channels:
                    del channels[key]
                    self._status_cache.pop(channel.id, None)
                    await ctx.send(f"Removed override for {channel.mention}.")
                else:
                    await ctx.send(f"No override for {channel.mention}.")
        else:
            await self.config.guild(ctx.guild).default_message.set("{count} connected")
            await self.config.guild(ctx.guild).clear_when_empty.set(False)
            await self.config.guild(ctx.guild).enabled.set(False)
            await self.config.guild(ctx.guild).channels.set({})
            self._status_cache.clear()
            await ctx.send("All reset and auto-status disabled.")

    @vcstatus.command(name="show")
    async def show(self, ctx: commands.Context) -> None:
        """Show the current configuration for this server."""
        cfg = await self.config.guild(ctx.guild).all()

        lines = [
            f"**Status:** {'Enabled' if cfg['enabled'] else 'Disabled'}",
            f"**Default:** {inline(cfg['default_message'])}",
            f"**Empty channels:** "
            f"{'cleared' if cfg['clear_when_empty'] else 'keep the message'}",
        ]

        if cfg["channels"]:
            for ch_id, msg in cfg["channels"].items():
                ch = ctx.guild.get_channel(int(ch_id))
                label = ch.mention if ch else f"deleted-channel-{ch_id}"
                lines.append(f"  - {label} -> {inline(msg)}")

        await ctx.send("\n".join(lines))

    # ------------------------------------------------------------------
    # Listeners
    # ------------------------------------------------------------------

    @commands.Cog.listener()
    async def on_voice_state_update(
        self,
        member: discord.Member,
        before: discord.VoiceState,
        after: discord.VoiceState,
    ) -> None:
        """Queue a status update when a human joins, leaves or moves."""
        if member.bot:
            return

        left, joined = before.channel, after.channel
        if left == joined:
            return  # mute / deaf / server-mute, etc.

        # Queue instead of awaiting: a slow request must not stall the listener,
        # and a move updates both channels.
        if left and isinstance(left, discord.VoiceChannel):
            self._queue_update(left)
        if joined and isinstance(joined, discord.VoiceChannel):
            self._queue_update(joined)

    @commands.Cog.listener()
    async def on_guild_channel_delete(self, channel: discord.abc.GuildChannel) -> None:
        """Drop cached state for a deleted channel."""
        self._status_cache.pop(channel.id, None)
        task = self._pending.pop(channel.id, None)
        if task is not None:
            task.cancel()

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _queue_update(self, channel: discord.VoiceChannel) -> None:
        """Debounce a status update for this channel.

        Repeated events within the window collapse into a single request, which
        keeps the bot clear of the per-route rate limit on a voice channel that
        sees a lot of coming and going.
        """
        existing = self._pending.pop(channel.id, None)
        if existing is not None:
            existing.cancel()
        self._pending[channel.id] = asyncio.create_task(self._debounced_update(channel))

    async def _debounced_update(self, channel: discord.VoiceChannel) -> None:
        try:
            await asyncio.sleep(DEBOUNCE_SECONDS)
        except asyncio.CancelledError:
            return

        # A newer event may have replaced this task while we slept.
        if self._pending.get(channel.id) is not asyncio.current_task():
            return
        self._pending.pop(channel.id, None)

        await self._update_status(channel)

    async def _update_status(self, channel: discord.VoiceChannel) -> None:
        """Recompute and push the status for a single channel."""
        guild = channel.guild
        cfg = await self.config.guild(guild).all()
        if not cfg["enabled"]:
            return

        count = sum(1 for m in channel.members if not m.bot)

        # ``status`` is nullable on this route: null clears the status.
        if count == 0 and cfg["clear_when_empty"]:
            resolved: Optional[str] = None
        else:
            msg = cfg["channels"].get(str(channel.id)) or cfg["default_message"]
            resolved = msg.replace("{count}", str(count))

        if not channel.permissions_for(guild.me).manage_channels:
            log.debug(
                "No manage_channels in %s (%s) - skipping", channel.name, channel.id
            )
            return

        cached = self._status_cache.get(channel.id, UNKNOWN)
        if cached is not UNKNOWN and cached == resolved:
            log.debug(
                "Status for %s (%s) already %r", channel.name, channel.id, resolved
            )
            return

        if await self._set_vc_status(channel, resolved):
            self._status_cache[channel.id] = resolved

    async def _apply_status_to_all(
        self, guild: discord.Guild, force: bool = False
    ) -> None:
        """Push the configured status to every voice channel."""
        cfg = await self.config.guild(guild).all()
        if not cfg["enabled"]:
            return

        channels = [
            ch
            for ch in guild.voice_channels
            if ch.permissions_for(guild.me).manage_channels
        ]

        for index, channel in enumerate(channels):
            if force:
                # Without this the cache would suppress every request.
                self._status_cache.pop(channel.id, None)
            if index:
                await asyncio.sleep(BULK_DELAY_SECONDS)
            await self._update_status(channel)

    async def _set_vc_status(
        self, channel: discord.VoiceChannel, status: Optional[str]
    ) -> bool:
        """Push (or clear) a voice channel status via its dedicated endpoint.

        ``channel.edit(voice_status=...)`` does not exist in discord.py; the
        status has its own route and expects PUT.  Passing ``None`` clears it.

        Never raises: a failure here must not abort the caller, otherwise a
        failed update on one channel would silently skip the other channel of
        a move (and the exception would surface as "it only works sometimes").
        """
        route = Route(
            "PUT",
            "/channels/{channel_id}/voice-status",
            channel_id=channel.id,
        )
        try:
            await self.bot.http.request(route, json={"status": status})
        except discord.HTTPException as exc:
            if exc.status == 403:
                log.warning(
                    "Could not set voice status in %s (%s): HTTP 403. The bot "
                    "needs SET_VOICE_CHANNEL_STATUS, plus MANAGE_CHANNELS while "
                    "it is not connected to that channel.",
                    channel.name,
                    channel.id,
                )
            else:
                log.warning(
                    "Could not set voice status in %s (%s): HTTP %s %s",
                    channel.name,
                    channel.id,
                    exc.status,
                    exc.text,
                )
            return False
        except Exception:
            log.exception(
                "Unexpected error setting voice status in %s (%s)",
                channel.name,
                channel.id,
            )
            return False

        log.debug("Set status in %s (%s) to %r", channel.name, channel.id, status)
        return True
