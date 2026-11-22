"""Disk usage of the machine the bot runs on."""

from __future__ import annotations

import asyncio
import functools
import io
from datetime import datetime, timezone

import discord
from redbot.core import Config, checks, commands

from . import chart, diskspace

BAR_CELLS = 12
WARN_PERCENT = 85
CRIT_PERCENT = 92


class DiskInfo(commands.Cog):
    """Free space on the server that hosts the bot."""

    def __init__(self, bot):
        self.bot = bot
        self.config = Config.get_conf(self, identifier="diskinfo", force_registration=True)
        self.config.register_guild(hidden=[], min_gib=1.0, image=False)

    # ------------------------------------------------------------- helpers
    @staticmethod
    def _bar(percent: float, cells: int = BAR_CELLS) -> str:
        filled = int(round(min(max(percent, 0.0), 100.0) / 100 * cells))
        filled = min(max(filled, 0), cells)
        return "█" * filled + "░" * (cells - filled)

    def _build_embed(self, host: str, filesystems, colour, prefix: str) -> discord.Embed:
        embed = discord.Embed(
            title="Disk usage",
            colour=colour,
            timestamp=datetime.now(timezone.utc),
        )
        embed.set_footer(text=host)

        if not filesystems:
            embed.description = (
                f"Nothing to show - every filesystem is filtered out. "
                f"`{prefix}df minsize 0` lowers the size cut-off, `{prefix}df hidden` lists "
                f"the mounts you filtered out."
            )
            return embed

        total = sum(fs.total for fs in filesystems)
        free = sum(fs.free for fs in filesystems)
        used_percent = (total - free) / total * 100 if total else 0.0
        embed.description = (
            f"**{host}** · {len(filesystems)} filesystem"
            f"{'s' if len(filesystems) != 1 else ''} · "
            f"{diskspace.human_size(free)} free of {diskspace.human_size(total)} "
            f"({used_percent:.0f}% in use)"
        )

        tight = [fs for fs in filesystems if fs.percent >= CRIT_PERCENT]
        if tight:
            names = ", ".join(f"`{fs.mountpoint}`" for fs in tight)
            embed.description += f"\n**{names} almost full**"

        for fs in filesystems:
            percent = fs.percent
            value = (
                f"`{self._bar(percent)}` **{percent:.0f}%** used\n"
                f"{diskspace.human_size(fs.used)} used · "
                f"{diskspace.human_size(fs.free)} free · "
                f"{diskspace.human_size(fs.total)} total\n"
                f"{fs.fstype}"
            )
            inode_percent = fs.inode_percent
            if inode_percent is not None and inode_percent >= WARN_PERCENT:
                value += f" · inodes {inode_percent:.0f}% used"
            embed.add_field(name=f"`{fs.mountpoint}`", value=value, inline=False)
        return embed

    async def _collect(self, guild, hidden=None, min_gib=None):
        loop = asyncio.get_running_loop()
        config = self.config.guild(guild)
        if hidden is None:
            hidden = tuple(await config.hidden())
        if min_gib is None:
            min_gib = await config.min_gib()
        return await loop.run_in_executor(
            None,
            functools.partial(
                diskspace.collect, int(float(min_gib) * diskspace.GIB), tuple(hidden)
            ),
        )

    async def _send_report(self, ctx, with_image: bool):
        filesystems = await self._collect(ctx.guild)
        host = diskspace.hostname()
        colour = await ctx.embed_color()
        embed = self._build_embed(host, filesystems, colour, ctx.clean_prefix)

        file = None
        if with_image:
            if not chart.available():
                embed.add_field(
                    name="Chart unavailable",
                    value=(
                        "Pillow is not in the bot's environment - "
                        f"`{ctx.clean_prefix}pipinstall pillow` installs it."
                    ),
                    inline=False,
                )
            else:
                loop = asyncio.get_running_loop()
                png = await loop.run_in_executor(
                    None,
                    functools.partial(
                        chart.render, host, filesystems, colour.to_rgb()
                    ),
                )
                file = discord.File(io.BytesIO(png), filename="disk-usage.png")

        if file is not None:
            await ctx.send(embed=embed, file=file)
        else:
            await ctx.send(embed=embed)

    # ------------------------------------------------------------ commands
    @commands.guild_only()
    @commands.group(name="df", invoke_without_command=True, aliases=["diskfree"])
    @checks.bot_has_permissions(embed_links=True)
    async def df(self, ctx: commands.Context):
        """Disk usage of the server hosting the bot.

        Every real filesystem with a bar, used/free/total size and mountpoint; pseudo filesystems, bind mounts and tiny partitions are filtered out. `[p]df chart` adds the PNG card once, `[p]df image` attaches it to every report, `[p]df hide` drops mounts you never want to see.
        """
        if ctx.invoked_subcommand is not None:
            return
        with_image = await self.config.guild(ctx.guild).image()
        await self._send_report(ctx, with_image=with_image)

    @df.command(name="chart", aliases=["png"])
    @checks.bot_has_permissions(embed_links=True, attach_files=True)
    async def df_chart(self, ctx: commands.Context):
        """Same report, with the PNG bar card attached.

        Good for a screenshot; the numbers are identical to the embed.
        """
        await self._send_report(ctx, with_image=True)

    @df.command(name="hide")
    @checks.mod_or_permissions(manage_messages=True)
    async def df_hide(self, ctx: commands.Context, mountpoint: str):
        """Hide one filesystem from the report.

        Takes a mountpoint as the report prints it, for example `/boot`. `[p]df show` brings it back.
        """
        mounted = await self._collect(ctx.guild, hidden=(), min_gib=0)
        known = {fs.mountpoint for fs in mounted}
        target = mountpoint.rstrip("/") or "/"
        if target not in known:
            listing = ", ".join(f"`{name}`" for name in sorted(known)) or "none"
            await ctx.send(f"No filesystem at `{mountpoint}`. Mounts on this host: {listing}")
            return
        async with self.config.guild(ctx.guild).hidden() as hidden:
            if target not in hidden:
                hidden.append(target)
        await ctx.send(f"`{target}` is hidden from the disk report now.")

    @df.command(name="show")
    @checks.mod_or_permissions(manage_messages=True)
    async def df_show(self, ctx: commands.Context, mountpoint: str):
        """Bring a hidden filesystem back into the report.

        Takes the mountpoint, not the device - `/boot` rather than `/dev/sda2`.
        """
        target = mountpoint.rstrip("/") or "/"
        async with self.config.guild(ctx.guild).hidden() as hidden:
            was_hidden = target in hidden
            while target in hidden:
                hidden.remove(target)
        if was_hidden:
            await ctx.send(f"`{target}` is back in the disk report.")
        else:
            await ctx.send(f"`{target}` was not hidden.")

    @df.command(name="hidden", aliases=["ignored"])
    async def df_hidden(self, ctx: commands.Context):
        """List the filesystems you filtered out.

        Only your guild's list; every server keeps its own.
        """
        hidden = await self.config.guild(ctx.guild).hidden()
        if not hidden:
            await ctx.send(
                f"No filesystem is hidden. "
                f"`{ctx.clean_prefix}df hide <mountpoint>` adds one."
            )
            return
        lines = [f"`{name}`" for name in hidden]
        await ctx.send(
            f"Hidden from the disk report: {', '.join(lines)}\n"
            f"`{ctx.clean_prefix}df show <mountpoint>` brings one back."
        )

    @df.command(name="minsize")
    @checks.admin_or_permissions(manage_guild=True)
    async def df_minsize(self, ctx: commands.Context, gib: float):
        """Ignore filesystems smaller than this many GiB.

        Default is 1, which drops EFI and boot partitions. `0` shows every real filesystem, up to 4096 GiB.
        """
        if gib < 0 or gib > 4096:
            await ctx.send("Give a size between 0 and 4096 GiB.")
            return
        await self.config.guild(ctx.guild).min_gib.set(float(gib))
        if gib == 0:
            await ctx.send("Size cut-off off - every real filesystem is shown.")
        else:
            await ctx.send(f"Filesystems below {gib:g} GiB are ignored now.")

    @df.command(name="image")
    @checks.admin_or_permissions(manage_guild=True)
    async def df_image(self, ctx: commands.Context, enabled: bool = None):
        """Attach the PNG card to every report.

        Without an argument it shows the current setting; `[p]df image true` makes plain `[p]df` send the picture too.
        """
        config = self.config.guild(ctx.guild)
        if enabled is None:
            current = await config.image()
            state = "on" if current else "off"
            await ctx.send(
                f"The chart is **{state}**. `{ctx.clean_prefix}df image true|false` changes it; "
                f"`{ctx.clean_prefix}df chart` always attaches it once."
            )
            return
        await config.image.set(enabled)
        if enabled:
            await ctx.send("Every disk report now comes with the chart.")
        else:
            await ctx.send("Disk reports are embed-only again. `df chart` still sends the picture.")
