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
from typing import Any, Callable, Dict, Optional, Tuple

import aiohttp
import discord
from redbot.core import app_commands, checks, commands
from redbot.core.bot import Red
from redbot.core.data_manager import cog_data_path

from . import cards, render
from .emoji import EmojiCache

log = logging.getLogger("red.freak_cogs.fakemessage")

MAX_CHARS = 2000
AVATAR_TIMEOUT = aiohttp.ClientTimeout(total=10)
BADGES = {"none": (), "mod": ("mod",), "sub": ("sub",), "vip": ("vip",), "founder": ("founder",)}


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


    # -- other platforms -------------------------------------------------
    async def _emoji_images(self, text: str) -> Dict[str, Any]:
        clusters = sorted({match.group(0) for match in render.UNICODE_EMOJI_RE.finditer(text)})
        if not clusters:
            return {}
        return await self.emoji_cache.load_unicode(self.session, clusters)

    async def _card(
        self,
        renderer: Callable[..., bytes],
        *,
        member: Optional[discord.Member],
        name: Optional[str],
        message: str,
        **kwargs,
    ) -> bytes:
        shown = (name or (member.display_name if member else "")).strip() or "someone"
        avatar = await self._avatar(member) if member is not None else None
        images = await self._emoji_images(message)
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(
            None,
            functools.partial(
                renderer, name=shown, message=message, avatar=avatar, images=images, **kwargs
            ),
        )

    @staticmethod
    def _stamp(time: Optional[str]) -> str:
        return (time or "").strip() or datetime.now().strftime("%H:%M")

    async def _reply(self, interaction: discord.Interaction, data: bytes) -> None:
        await interaction.followup.send(file=discord.File(io.BytesIO(data), filename="message.png"))

    async def _slash_card(
        self, interaction: discord.Interaction, builder, **kwargs
    ) -> None:
        text = (kwargs.pop("message", "") or "").strip()
        await interaction.response.defer(thinking=True)
        if not text or len(text) > MAX_CHARS:
            await interaction.followup.send(
                "The message has to be between 1 and {} characters.".format(MAX_CHARS),
                ephemeral=True,
            )
            return
        try:
            data = await builder(message=text, **kwargs)
        except Exception:
            log.exception("fakemessage card render failed")
            await interaction.followup.send(
                "I could not draw that - check the bot log.", ephemeral=True
            )
            return
        await self._reply(interaction, data)

    # WhatsApp
    async def _whatsapp(
        self,
        member: Optional[discord.Member],
        name: Optional[str],
        message: str,
        time: Optional[str] = None,
        outgoing: bool = False,
    ) -> bytes:
        return await self._card(
            cards.whatsapp,
            member=member,
            name=name,
            message=message,
            time=self._stamp(time),
            outgoing=outgoing,
        )

    @app_commands.command(name="fakewhatsapp", description="Create a fake WhatsApp chat image.")
    @app_commands.describe(
        message="What the message says - WhatsApp markdown (*bold*, _italic_, ~strike~) works",
        member="Whose name and avatar appear in the header",
        name="Show a different name than the member's",
        time="Timestamp inside the bubble, e.g. 21:07",
        outgoing="Green bubble on the right, with read ticks",
    )
    async def fakewhatsapp_slash(
        self,
        interaction: discord.Interaction,
        message: str,
        member: Optional[discord.Member] = None,
        name: Optional[str] = None,
        time: Optional[str] = None,
        outgoing: bool = False,
    ) -> None:
        await self._slash_card(
            interaction,
            self._whatsapp,
            message=message,
            member=member,
            name=name,
            time=time,
            outgoing=outgoing,
        )

    @commands.command(name="fakewa", aliases=["fakewhatsapp"])
    @commands.guild_only()
    @checks.bot_has_permissions(attach_files=True)
    @commands.cooldown(5, 60, commands.BucketType.user)
    async def fakewa(self, ctx: commands.Context, *, message: str):
        """Fake a WhatsApp message from you.

        The slash command takes a member, name, time and an outgoing flag.
        """
        await self._deliver_card(ctx, self._whatsapp, message)

    # Telegram
    async def _telegram(
        self,
        member: Optional[discord.Member],
        name: Optional[str],
        message: str,
        time: Optional[str] = None,
        outgoing: bool = False,
    ) -> bytes:
        return await self._card(
            cards.telegram,
            member=member,
            name=name,
            message=message,
            time=self._stamp(time),
            outgoing=outgoing,
        )

    @app_commands.command(name="faketelegram", description="Create a fake Telegram chat image.")
    @app_commands.describe(
        message="What the message says - Telegram markdown (**bold**, __italic__, ~~strike~~) works",
        member="Whose name and avatar appear in the header",
        name="Show a different name than the member's",
        time="Timestamp inside the bubble, e.g. 21:07",
        outgoing="Blue bubble on the right, with check marks",
    )
    async def faketelegram_slash(
        self,
        interaction: discord.Interaction,
        message: str,
        member: Optional[discord.Member] = None,
        name: Optional[str] = None,
        time: Optional[str] = None,
        outgoing: bool = False,
    ) -> None:
        await self._slash_card(
            interaction,
            self._telegram,
            message=message,
            member=member,
            name=name,
            time=time,
            outgoing=outgoing,
        )

    @commands.command(name="faketg", aliases=["faketelegram"])
    @commands.guild_only()
    @checks.bot_has_permissions(attach_files=True)
    @commands.cooldown(5, 60, commands.BucketType.user)
    async def faketg(self, ctx: commands.Context, *, message: str):
        """Fake a Telegram message from you."""
        await self._deliver_card(ctx, self._telegram, message)

    # X / Twitter
    async def _tweet(
        self,
        member: Optional[discord.Member],
        name: Optional[str],
        message: str,
        handle: Optional[str] = None,
        verified: bool = False,
        time: Optional[str] = None,
        date: Optional[str] = None,
        replies: Optional[str] = None,
        retweets: Optional[str] = None,
        likes: Optional[str] = None,
        views: Optional[str] = None,
    ) -> bytes:
        shown = (name or (member.display_name if member else "")).strip() or "someone"
        nick = (handle or (member.name if member else shown)).strip().lstrip("@")
        kwargs: Dict[str, Any] = {
            "handle": nick,
            "verified": verified,
            "time": self._stamp(time),
            "date": (date or "").strip(),
        }
        for key, value in (("replies", replies), ("retweets", retweets), ("likes", likes), ("views", views)):
            if value:
                kwargs[key] = value.strip()
        return await self._card(cards.tweet, member=member, name=shown, message=message, **kwargs)

    @app_commands.command(name="faketweet", description="Create a fake X post image.")
    @app_commands.describe(
        message="The post text - links are coloured, text is not markdown",
        member="Whose avatar to use",
        name="Display name on the post",
        handle="Handle without the @",
        verified="Show the blue check",
        time="Time on the post, e.g. 9:41 PM",
        date="Date on the post, e.g. 25. Nov. 2026",
        replies="Reply count",
        retweets="Repost count",
        likes="Like count",
        views="View count",
    )
    async def faketweet_slash(
        self,
        interaction: discord.Interaction,
        message: str,
        member: Optional[discord.Member] = None,
        name: Optional[str] = None,
        handle: Optional[str] = None,
        verified: bool = False,
        time: Optional[str] = None,
        date: Optional[str] = None,
        replies: Optional[str] = None,
        retweets: Optional[str] = None,
        likes: Optional[str] = None,
        views: Optional[str] = None,
    ) -> None:
        await self._slash_card(
            interaction,
            self._tweet,
            message=message,
            member=member,
            name=name,
            handle=handle,
            verified=verified,
            time=time,
            date=date,
            replies=replies,
            retweets=retweets,
            likes=likes,
            views=views,
        )

    @commands.command(name="faketweet", aliases=["faketw"])
    @commands.guild_only()
    @checks.bot_has_permissions(attach_files=True)
    @commands.cooldown(5, 60, commands.BucketType.user)
    async def faketweet(self, ctx: commands.Context, *, message: str):
        """Fake an X post from you."""
        await self._deliver_card(ctx, self._tweet, message)

    # Twitch / Kick
    @staticmethod
    def _colour(value: Optional[str]) -> Optional[Tuple[int, int, int]]:
        raw = (value or "").strip().lstrip("#")
        if len(raw) != 6:
            return None
        try:
            return tuple(int(raw[index : index + 2], 16) for index in (0, 2, 4))  # type: ignore[return-value]
        except ValueError:
            return None

    async def _chat(
        self,
        member: Optional[discord.Member],
        name: Optional[str],
        message: str,
        platform: str,
        colour: Optional[str] = None,
        badge: Optional[str] = None,
    ) -> bytes:
        return await self._card(
            cards.chat,
            member=member,
            name=name,
            message=message,
            platform=platform,
            colour=self._colour(colour),
            badges=BADGES.get((badge or "none").lower(), ()),
        )

    @app_commands.command(name="faketwitch", description="Create a fake Twitch chat snippet.")
    @app_commands.describe(
        message="One chat line per line break (max 6)",
        member="Whose name and avatar to use",
        name="Username in the chat line",
        colour="Username colour as #rrggbb",
        badge="Badge in front of the name",
    )
    @app_commands.choices(
        badge=[
            app_commands.Choice(name="none", value="none"),
            app_commands.Choice(name="mod", value="mod"),
            app_commands.Choice(name="sub", value="sub"),
            app_commands.Choice(name="vip", value="vip"),
            app_commands.Choice(name="founder", value="founder"),
        ]
    )
    async def faketwitch_slash(
        self,
        interaction: discord.Interaction,
        message: str,
        member: Optional[discord.Member] = None,
        name: Optional[str] = None,
        colour: Optional[str] = None,
        badge: Optional[app_commands.Choice[str]] = None,
    ) -> None:
        await self._slash_card(
            interaction,
            self._chat,
            message=message,
            member=member,
            name=name,
            platform="twitch",
            colour=colour,
            badge=badge.value if badge else None,
        )

    @commands.command(name="faketwitch")
    @commands.guild_only()
    @checks.bot_has_permissions(attach_files=True)
    @commands.cooldown(5, 60, commands.BucketType.user)
    async def faketwitch(self, ctx: commands.Context, *, message: str):
        """Fake a Twitch chat line from you."""
        await self._deliver_card(ctx, self._chat, message, platform="twitch")

    @app_commands.command(name="fakekick", description="Create a fake Kick chat snippet.")
    @app_commands.describe(
        message="One chat line per line break (max 6)",
        member="Whose name and avatar to use",
        name="Username in the chat line",
        colour="Username colour as #rrggbb",
        badge="Badge in front of the name",
    )
    @app_commands.choices(
        badge=[
            app_commands.Choice(name="none", value="none"),
            app_commands.Choice(name="mod", value="mod"),
            app_commands.Choice(name="sub", value="sub"),
            app_commands.Choice(name="vip", value="vip"),
            app_commands.Choice(name="founder", value="founder"),
        ]
    )
    async def fakekick_slash(
        self,
        interaction: discord.Interaction,
        message: str,
        member: Optional[discord.Member] = None,
        name: Optional[str] = None,
        colour: Optional[str] = None,
        badge: Optional[app_commands.Choice[str]] = None,
    ) -> None:
        await self._slash_card(
            interaction,
            self._chat,
            message=message,
            member=member,
            name=name,
            platform="kick",
            colour=colour,
            badge=badge.value if badge else None,
        )

    @commands.command(name="fakekick")
    @commands.guild_only()
    @checks.bot_has_permissions(attach_files=True)
    @commands.cooldown(5, 60, commands.BucketType.user)
    async def fakekick(self, ctx: commands.Context, *, message: str):
        """Fake a Kick chat line from you."""
        await self._deliver_card(ctx, self._chat, message, platform="kick")

    async def _deliver_card(self, ctx: commands.Context, builder, message: str, **kwargs) -> None:
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
                data = await builder(ctx.author, None, message, **kwargs)
            except Exception:
                log.exception("fakemessage card render failed")
                await ctx.send("I could not draw that - check the bot log.")
                return
        await ctx.send(file=discord.File(io.BytesIO(data), filename="message.png"))

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
