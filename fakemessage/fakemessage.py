"""fakemessage - render any member's message as a Discord screenshot.

The whole point is that the result looks like a normal chat message, so the
cog fetches what the client would show instead of inventing it: the member's
avatar, their top role colour, the BOT tag, and Twemoji pictures for emoji.
Markdown that people actually type (bold, italic, underline, strike, inline
code, mentions, links, spoilers) is rendered the way the client renders it.

Drawing lives in render.py and the emoji cache in emoji.py, so this file is
only transport: fetch the assets, hand them to the renderer, send the PNG.
"""

from __future__ import annotations

import asyncio
import functools
import io
import logging
from datetime import datetime
from typing import Any, Dict, Optional

import aiohttp
import discord
from redbot.core import app_commands, checks, commands
from redbot.core.bot import Red
from redbot.core.data_manager import cog_data_path

from . import render
from .emoji import EmojiCache

log = logging.getLogger("red.freak_cogs.fakemessage")

MAX_CHARS = 2000
AVATAR_TIMEOUT = aiohttp.ClientTimeout(total=10)


class FakeMessage(commands.Cog):
    """Turn a member and a sentence into a Discord screenshot."""

    def __init__(self, bot: Red):
        self.bot = bot
        self._emoji: Optional[EmojiCache] = None
        self._session: Optional[aiohttp.ClientSession] = None

    def cog_unload(self) -> None:
        session, self._session = self._session, None
        if session is None or session.closed:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        loop.create_task(session.close())

    @property
    def session(self) -> aiohttp.ClientSession:
        """Own session: discord.py 2.7 no longer exposes ``bot.session``."""
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(timeout=AVATAR_TIMEOUT)
        return self._session

    # -- assets ----------------------------------------------------------
    @property
    def emoji_cache(self) -> EmojiCache:
        if self._emoji is None:
            self._emoji = EmojiCache(str(cog_data_path(self) / "emoji"))
        return self._emoji

    async def _avatar(self, member: discord.Member) -> Optional[bytes]:
        try:
            asset = member.display_avatar.replace(size=128, format="png")
            async with self.session.get(asset.url) as response:
                if response.status != 200:
                    return None
                return await response.read()
        except (aiohttp.ClientError, OSError, TimeoutError):
            return None

    async def _build(
        self,
        member: discord.Member,
        message: str,
        *,
        name: Optional[str] = None,
        timestamp: Optional[str] = None,
        bot: bool = False,
    ) -> bytes:
        text = message.strip()
        clusters = [match.group(0) for match in render.UNICODE_EMOJI_RE.finditer(text)]
        custom_ids = [match.group(3) for match in render.CUSTOM_EMOJI_RE.finditer(text)]

        session = self.session
        emoji_images: Dict[str, Any] = {}
        if clusters:
            emoji_images.update(await self.emoji_cache.load_unicode(session, clusters))
        if custom_ids:
            emoji_images.update(await self.emoji_cache.load_custom(session, custom_ids))

        avatar = await self._avatar(member)
        colour = member.colour.to_rgb() if member.colour.value else None
        shown_name = (name or member.display_name).strip() or member.display_name
        stamp = (timestamp or "").strip() or "Today at {}".format(datetime.now().strftime("%H:%M"))

        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(
            None,
            functools.partial(
                render.render,
                name=shown_name,
                message=text,
                timestamp=stamp,
                avatar=avatar,
                bot=bot or member.bot,
                name_color=colour,
                emoji_images=emoji_images,
            ),
        )

    async def _deliver(self, ctx: commands.Context, member: discord.Member, message: str) -> None:
        if not message.strip():
            await ctx.send("There is no message to draw.")
            return
        if len(message) > MAX_CHARS:
            await ctx.send(
                "That message is {} characters long - {} is the limit.".format(
                    len(message), MAX_CHARS
                )
            )
            return
        async with ctx.typing():
            try:
                data = await self._build(member, message)
            except Exception:
                log.exception("fakemessage render failed")
                await ctx.send("I could not draw that message - check the bot log.")
                return
        await ctx.send(file=discord.File(io.BytesIO(data), filename="message.png"))

    # -- commands --------------------------------------------------------
    @commands.command(name="fakemessage", aliases=["fakemsg"])
    @commands.guild_only()
    @checks.bot_has_permissions(attach_files=True)
    @commands.cooldown(5, 60, commands.BucketType.user)
    async def fakemessage(self, ctx: commands.Context, member: discord.Member, *, message: str):
        """Render a message from a member as a Discord screenshot.

        Example: `[p]fakemessage @someone i never said that`.
        The slash command takes the same thing plus optional overrides.
        """
        await self._deliver(ctx, member, message)

    @app_commands.command(name="fakemessage", description="Create a fake Discord message image.")
    @app_commands.describe(
        member="Who the message appears to be from",
        message="What the message says - markdown, mentions and emoji work",
        name="Show a different username than the member's",
        timestamp="Replace the time, e.g. 'Yesterday at 23:12'",
        bot_tag="Show the BOT tag next to the name",
    )
    async def fakemessage_slash(
        self,
        interaction: discord.Interaction,
        member: discord.Member,
        message: str,
        name: Optional[str] = None,
        timestamp: Optional[str] = None,
        bot_tag: bool = False,
    ) -> None:
        await interaction.response.defer(thinking=True)
        if not message.strip() or len(message) > MAX_CHARS:
            await interaction.followup.send(
                "The message has to be between 1 and {} characters.".format(MAX_CHARS),
                ephemeral=True,
            )
            return
        try:
            data = await self._build(
                member, message, name=name, timestamp=timestamp, bot=bot_tag
            )
        except Exception:
            log.exception("fakemessage render failed")
            await interaction.followup.send(
                "I could not draw that message - check the bot log.", ephemeral=True
            )
            return
        await interaction.followup.send(
            file=discord.File(io.BytesIO(data), filename="message.png")
        )
