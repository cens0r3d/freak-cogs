"""VCStatus — Automatic voice channel status on member join."""

import logging
from typing import Optional

import discord
from discord.http import Route
from redbot.core import Config, commands
from redbot.core.bot import Red
from redbot.core.utils.chat_formatting import bold, inline

log = logging.getLogger("red.vcstatus")


class VCStatus(commands.Cog):

    def __init__(self, bot: Red) -> None:
        self.bot = bot
        self._status_cache: dict[int, Optional[str]] = {}
        self.config = Config.get_conf(
            self, identifier=23_593_420_582_31, force_registration=True
        )
        self.config.register_guild(
            enabled=False,
            default_message="{count} connected",
            channels={},
        )

    @commands.group(name="vcstatus")
    @commands.guild_only()
    @commands.admin_or_permissions(manage_channels=True)
    async def vcstatus(self, ctx: commands.Context) -> None:
        """Manage the automatic voice-channel status feature."""

    @vcstatus.command(name="toggle")
    async def toggle(self, ctx: commands.Context) -> None:
        current = await self.config.guild(ctx.guild).enabled()
        new_state = not current
        await self.config.guild(ctx.guild).enabled.set(new_state)
        await ctx.send(
            f"Auto voice status is now "
            f"{bold('enabled') if new_state else bold('disabled')}."
        )
        if new_state:
            await self._apply_status_to_all(ctx.guild)

    @vcstatus.command(name="default")
    async def set_default(self, ctx: commands.Context, *, message: str) -> None:
        if len(message) > 500:
            await ctx.send(f"Message too long ({len(message)}/500).")
            return
        await self.config.guild(ctx.guild).default_message.set(message)
        await ctx.send(f"Default set to: {inline(message)}")
        if await self.config.guild(ctx.guild).enabled():
            await self._apply_status_to_all(ctx.guild)

    @vcstatus.command(name="channel")
    async def set_channel(
        self,
        ctx: commands.Context,
        channel: discord.VoiceChannel,
        *,
        message: str,
    ) -> None:
        if channel.guild != ctx.guild:
            await ctx.send("Not in this server.")
            return
        if len(message) > 500:
            await ctx.send(f"Message too long ({len(message)}/500).")
            return
        async with self.config.guild(ctx.guild).channels() as channels:
            channels[str(channel.id)] = message
        await ctx.send(f"Status for {channel.mention} set to: {inline(message)}")
        if await self.config.guild(ctx.guild).enabled():
            await self._apply_status_to_all(ctx.guild)

    @vcstatus.command(name="remove", aliases=["clear", "reset"])
    async def remove_message(
        self,
        ctx: commands.Context,
        channel: Optional[discord.VoiceChannel] = None,
    ) -> None:
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
            await self.config.guild(ctx.guild).enabled.set(False)
            await self.config.guild(ctx.guild).channels.set({})
            self._status_cache.clear()
            await ctx.send("All reset.")

    @vcstatus.command(name="show")
    async def show(self, ctx: commands.Context) -> None:
        cfg = await self.config.guild(ctx.guild).all()
        lines = [
            f"**Status:** {'Enabled' if cfg['enabled'] else 'Disabled'}",
            f"**Default:** {inline(cfg['default_message'])}",
        ]
        if cfg["channels"]:
            for ch_id, msg in cfg["channels"].items():
                ch = ctx.guild.get_channel(int(ch_id))
                label = ch.mention if ch else f"deleted-channel-{ch_id}"
                lines.append(f"  - {label} -> {inline(msg)}")
        await ctx.send("\n".join(lines))

    @commands.Cog.listener()
    async def on_voice_state_update(
        self,
        member: discord.Member,
        before: discord.VoiceState,
        after: discord.VoiceState,
    ) -> None:
        if member.bot:
            return
        left, joined = before.channel, after.channel
        if left == joined:
            return
        if left and isinstance(left, discord.VoiceChannel):
            await self._update_status(left)
        if joined and isinstance(joined, discord.VoiceChannel):
            await self._update_status(joined)

    @commands.Cog.listener()
    async def on_guild_channel_delete(self, channel: discord.abc.GuildChannel) -> None:
        self._status_cache.pop(channel.id, None)

    async def _update_status(self, channel: discord.VoiceChannel) -> None:
        guild = channel.guild
        cfg = await self.config.guild(guild).all()
        if not cfg["enabled"]:
            return
        msg = cfg["channels"].get(str(channel.id)) or cfg["default_message"]
        resolved = msg.replace(
            "{count}",
            str(sum(1 for m in channel.members if not m.bot)),
        )
        if not channel.permissions_for(guild.me).manage_channels:
            return
        cached = self._status_cache.get(channel.id)
        if cached == resolved:
            return
        await self._set_vc_status(channel.id, resolved)
        self._status_cache[channel.id] = resolved

    async def _apply_status_to_all(self, guild: discord.Guild) -> None:
        cfg = await self.config.guild(guild).all()
        if not cfg["enabled"]:
            return
        for channel in guild.voice_channels:
            if not channel.permissions_for(guild.me).manage_channels:
                continue
            msg = cfg["channels"].get(str(channel.id)) or cfg["default_message"]
            resolved = msg.replace(
                "{count}",
                str(sum(1 for m in channel.members if not m.bot)),
            )
            cached = self._status_cache.get(channel.id)
            if cached == resolved:
                continue
            await self._set_vc_status(channel.id, resolved)
            self._status_cache[channel.id] = resolved

    async def _set_vc_status(self, channel_id: int, status: str) -> None:
        route = Route(
            "PUT",
            "/channels/{channel_id}/voice-status",
            channel_id=channel_id,
        )
        await self.bot.http.request(route, json={"status": status})
