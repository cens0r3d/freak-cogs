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
from typing import Any, Callable, Dict, Optional, Sequence, Tuple

import aiohttp
import discord
from redbot.core import app_commands, checks, commands
from redbot.core.bot import Red
from redbot.core.data_manager import cog_data_path

from . import cards, render
from .emoji import EmojiCache

log = logging.getLogger("red.freak_cogs.fakemessage")

MAX_CHARS = 2000
MAX_IMAGE_BYTES = 8 * 1024 * 1024
AVATAR_TIMEOUT = aiohttp.ClientTimeout(total=10)
BADGES = {"none": (), "mod": ("mod",), "sub": ("sub",), "vip": ("vip",), "founder": ("founder",)}
SERVICES = {
    "discord": "discord",
    "dc": "discord",
    "whatsapp": "whatsapp",
    "wa": "whatsapp",
    "telegram": "telegram",
    "tg": "telegram",
    "x": "x",
    "twitter": "x",
    "tweet": "x",
    "tw": "x",
    "twitch": "twitch",
    "kick": "kick",
    "facebook": "facebook",
    "fb": "facebook",
    "reddit": "reddit",
    "rd": "reddit",
    "instagram": "instagram",
    "ig": "instagram",
    "insta": "instagram",
    "snapchat": "snapchat",
    "sc": "snapchat",
    "snap": "snapchat",
    "imessage": "imessage",
    "im": "imessage",
    "imsg": "imessage",
    "youtube": "youtube",
    "yt": "youtube",
}


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
        attachment: Optional[bytes] = None,
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
                attachment=attachment,
            ),
        )


    # -- other platforms -------------------------------------------------
    async def _emoji_images(self, text: str, extra: Sequence[str] = ()) -> Dict[str, Any]:
        clusters = sorted(
            {match.group(0) for match in render.UNICODE_EMOJI_RE.finditer(text)} | set(extra)
        )
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
        extra_emoji: Sequence[str] = (),
        image: Optional[bytes] = None,
        **kwargs,
    ) -> bytes:
        shown = (name or (member.display_name if member else "")).strip() or "someone"
        avatar = await self._avatar(member) if member is not None else None
        images = await self._emoji_images(message, extra_emoji)
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(
            None,
            functools.partial(
                renderer,
                name=shown,
                message=message,
                avatar=avatar,
                images=images,
                image=image,
                **kwargs,
            ),
        )

    async def _attachment(self, attachment) -> Optional[bytes]:
        """Read an uploaded picture, with the size and type the platforms accept."""
        if attachment is None:
            return None
        ctype = (attachment.content_type or "").lower()
        if ctype and not ctype.startswith("image/"):
            raise ValueError("That attachment is not an image.")
        if attachment.size > MAX_IMAGE_BYTES:
            raise ValueError(
                "That image is {:.1f} MB - {} MB is the limit.".format(
                    attachment.size / 1048576, MAX_IMAGE_BYTES // 1048576
                )
            )
        return await attachment.read()

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

    # -- per-service transport ------------------------------------------
    async def _whatsapp(
        self,
        member: Optional[discord.Member],
        name: Optional[str],
        message: str,
        time: Optional[str] = None,
        image: Optional[bytes] = None,
        outgoing: bool = False,
    ) -> bytes:
        return await self._card(
            cards.whatsapp,
            member=member,
            name=name,
            message=message,
            time=self._stamp(time),
            outgoing=outgoing,
            image=image,
        )

    async def _telegram(
        self,
        member: Optional[discord.Member],
        name: Optional[str],
        message: str,
        time: Optional[str] = None,
        image: Optional[bytes] = None,
        outgoing: bool = False,
    ) -> bytes:
        return await self._card(
            cards.telegram,
            member=member,
            name=name,
            message=message,
            time=self._stamp(time),
            outgoing=outgoing,
            image=image,
        )

    async def _tweet(
        self,
        member: Optional[discord.Member],
        name: Optional[str],
        message: str,
        handle: Optional[str] = None,
        verified: bool = False,
        time: Optional[str] = None,
        image: Optional[bytes] = None,
    ) -> bytes:
        shown = (name or (member.display_name if member else "")).strip() or "someone"
        nick = (handle or (member.name if member else shown)).strip().lstrip("@")
        return await self._card(
            cards.tweet,
            member=member,
            name=shown,
            message=message,
            handle=nick,
            verified=verified,
            time=self._stamp(time),
            image=image,
        )

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
        platform: str = "twitch",
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


    async def _facebook(
        self,
        member: Optional[discord.Member],
        name: Optional[str],
        message: str,
        time: Optional[str] = None,
        image: Optional[bytes] = None,
    ) -> bytes:
        return await self._card(
            cards.facebook,
            member=member,
            name=name,
            message=message,
            time=(time or "").strip() or "Just now",
            extra_emoji=("\U0001F44D", "\u2764\ufe0f"),
            image=image,
        )

    async def _reddit(
        self,
        member: Optional[discord.Member],
        name: Optional[str],
        message: str,
        time: Optional[str] = None,
        image: Optional[bytes] = None,
    ) -> bytes:
        return await self._card(
            cards.reddit,
            member=member,
            name=name,
            message=message,
            time=(time or "").strip() or "5h ago",
            image=image,
        )

    async def _instagram(
        self,
        member: Optional[discord.Member],
        name: Optional[str],
        message: str,
        time: Optional[str] = None,
        outgoing: bool = False,
        image: Optional[bytes] = None,
    ) -> bytes:
        return await self._card(
            cards.instagram,
            member=member,
            name=name,
            message=message,
            time=self._stamp(time),
            outgoing=outgoing,
            image=image,
        )

    async def _snapchat(
        self,
        member: Optional[discord.Member],
        name: Optional[str],
        message: str,
        time: Optional[str] = None,
        outgoing: bool = False,
        image: Optional[bytes] = None,
    ) -> bytes:
        return await self._card(
            cards.snapchat,
            member=member,
            name=name,
            message=message,
            time=self._stamp(time),
            outgoing=outgoing,
            image=image,
        )

    async def _imessage(
        self,
        member: Optional[discord.Member],
        name: Optional[str],
        message: str,
        time: Optional[str] = None,
        outgoing: bool = False,
        image: Optional[bytes] = None,
    ) -> bytes:
        return await self._card(
            cards.imessage,
            member=member,
            name=name,
            message=message,
            time=self._stamp(time),
            outgoing=outgoing,
            image=image,
        )

    async def _youtube(
        self,
        member: Optional[discord.Member],
        name: Optional[str],
        message: str,
        time: Optional[str] = None,
        image: Optional[bytes] = None,
    ) -> bytes:
        return await self._card(
            cards.youtube,
            member=member,
            name=name,
            message=message,
            time=(time or "").strip() or "1 day ago",
            image=image,
        )

    async def _dispatch(
        self,
        member: Optional[discord.Member] = None,
        name: Optional[str] = None,
        message: str = "",
        *,
        service: str = "discord",
        timestamp: Optional[str] = None,
        outgoing: bool = False,
        handle: Optional[str] = None,
        verified: bool = False,
        colour: Optional[str] = None,
        badge: Optional[str] = None,
        image: Optional[bytes] = None,
    ) -> bytes:
        """One entry point: pick the renderer from the service name."""
        if service == "discord":
            if member is None:
                raise ValueError("the discord card needs a member")
            return await self._build(
                member, message, name=name, timestamp=timestamp, attachment=image
            )
        if service == "whatsapp":
            return await self._whatsapp(
                member, name, message, time=timestamp, outgoing=outgoing, image=image
            )
        if service == "telegram":
            return await self._telegram(
                member, name, message, time=timestamp, outgoing=outgoing, image=image
            )
        if service == "x":
            return await self._tweet(
                member,
                name,
                message,
                handle=handle,
                verified=verified,
                time=timestamp,
                image=image,
            )
        if service == "facebook":
            return await self._facebook(member, name, message, time=timestamp, image=image)
        if service == "reddit":
            return await self._reddit(member, name, message, time=timestamp, image=image)
        if service == "instagram":
            return await self._instagram(
                member, name, message, time=timestamp, outgoing=outgoing, image=image
            )
        if service == "snapchat":
            return await self._snapchat(
                member, name, message, time=timestamp, outgoing=outgoing, image=image
            )
        if service == "imessage":
            return await self._imessage(
                member, name, message, time=timestamp, outgoing=outgoing, image=image
            )
        if service == "youtube":
            return await self._youtube(member, name, message, time=timestamp, image=image)
        return await self._chat(
            member, name, message, platform=service, colour=colour, badge=badge
        )

    # -- commands --------------------------------------------------------
    async def _deliver_card(
        self, ctx: commands.Context, builder, message: str, member=None, **kwargs
    ) -> None:
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
                data = await builder(member or ctx.author, None, message, **kwargs)
            except Exception:
                log.exception("fakemessage render failed")
                await ctx.send("I could not draw that - check the bot log.")
                return
        await ctx.send(file=discord.File(io.BytesIO(data), filename="message.png"))

    @commands.command(name="fakemessage", aliases=["fake"])
    @commands.guild_only()
    @checks.bot_has_permissions(attach_files=True)
    @commands.cooldown(5, 60, commands.BucketType.user)
    async def fakemessage(
        self, ctx: commands.Context, service: Optional[str] = None, *, rest: Optional[str] = None
    ):
        """Fake a screenshot of a chat or post, from you.

        `[p]fakemessage <service> <text>` - services: discord, whatsapp,
        telegram, x, twitch, kick.
        `[p]fakemessage discord @member <text>` - the Discord card needs a
        member; the other services use you, with `member` and `name` overrides
        on the slash command.
        """
        if not service:
            await ctx.send_help(ctx.command)
            return
        key = SERVICES.get(service.lower().strip("<>@!&"))
        if key is None:
            await ctx.send(
                "I do not know `{}` - pick one of: {}.".format(
                    service, ", ".join(sorted(set(SERVICES.values())))
                )
            )
            return
        text = (rest or "").strip()
        member = ctx.author
        if key == "discord":
            first, _, remainder = text.partition(" ")
            try:
                member = await commands.MemberConverter().convert(ctx, first)
            except commands.BadArgument:
                await ctx.send(
                    "For the Discord card I need a member: `{}fakemessage discord @someone <text>`.".format(
                        ctx.clean_prefix
                    )
                )
                return
            text = remainder.strip()
        upload = ctx.message.attachments[0] if getattr(ctx.message, "attachments", None) else None
        try:
            picture = await self._attachment(upload)
        except ValueError as error:
            await ctx.send(str(error))
            return
        await self._deliver_card(
            ctx, self._dispatch, text, member=member, service=key, image=picture
        )

    @app_commands.command(name="fakemessage", description="Create a fake chat or post screenshot.")
    @app_commands.describe(
        service="Which app the screenshot should look like",
        message="What the message says - line breaks become chat lines on Twitch and Kick",
        member="Whose avatar and name appear (required for the discord card)",
        name="Show a different name than the member's",
        timestamp="Time in the bubble, next to the name, or instead of 'Today at ...' / 'Just now' / '5h ago'",
        outgoing="WhatsApp/Telegram: green or blue bubble on the right, with ticks",
        handle="X: handle without the @",
        verified="X: show the blue check",
        colour="Twitch/Kick: username colour as #rrggbb",
        badge="Twitch/Kick: badge in front of the name",
        image="Optional picture to attach to the message (ignored by Twitch and Kick)",
    )
    @app_commands.choices(
        service=[
            app_commands.Choice(name="discord", value="discord"),
            app_commands.Choice(name="whatsapp", value="whatsapp"),
            app_commands.Choice(name="telegram", value="telegram"),
            app_commands.Choice(name="x", value="x"),
            app_commands.Choice(name="twitch", value="twitch"),
            app_commands.Choice(name="kick", value="kick"),
            app_commands.Choice(name="facebook", value="facebook"),
            app_commands.Choice(name="reddit", value="reddit"),
            app_commands.Choice(name="instagram", value="instagram"),
            app_commands.Choice(name="snapchat", value="snapchat"),
            app_commands.Choice(name="imessage", value="imessage"),
            app_commands.Choice(name="youtube", value="youtube"),
        ],
        badge=[
            app_commands.Choice(name="none", value="none"),
            app_commands.Choice(name="mod", value="mod"),
            app_commands.Choice(name="sub", value="sub"),
            app_commands.Choice(name="vip", value="vip"),
            app_commands.Choice(name="founder", value="founder"),
        ],
    )
    async def fakemessage_slash(
        self,
        interaction: discord.Interaction,
        service: app_commands.Choice[str],
        message: str,
        member: Optional[discord.Member] = None,
        name: Optional[str] = None,
        timestamp: Optional[str] = None,
        outgoing: bool = False,
        handle: Optional[str] = None,
        verified: bool = False,
        colour: Optional[str] = None,
        badge: Optional[app_commands.Choice[str]] = None,
        image: Optional[discord.Attachment] = None,
    ) -> None:
        if service.value == "discord" and member is None:
            await interaction.response.send_message(
                "The discord card needs a member to copy the name and avatar from.",
                ephemeral=True,
            )
            return
        try:
            picture = await self._attachment(image)
        except ValueError as error:
            await interaction.response.send_message(str(error), ephemeral=True)
            return
        await self._slash_card(
            interaction,
            self._dispatch,
            message=message,
            service=service.value,
            member=member,
            name=name,
            timestamp=timestamp,
            outgoing=outgoing,
            handle=handle,
            verified=verified,
            colour=colour,
            badge=badge.value if badge else None,
            image=picture,
        )
