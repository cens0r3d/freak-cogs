"""quoter - turn a message into an inspirational quote card.

The equivalent of Equicord's Quoter plugin for Red: reply to a message (or
right-click it and pick Apps -> Quote) and the bot answers with a square
card - the author's avatar in a coloured ring, the text in a serif face, the
attribution below and an optional watermark.

Drawing and text cleanup live in card.py, which imports neither discord nor
redbot, so a card can be rendered and inspected without a bot. This file is
transport: resolve the message, fetch the avatar, hand everything to the
renderer, send the PNG.
"""

from __future__ import annotations

import asyncio
import functools
import io
import logging
import re
from typing import Any, Dict, Optional

import aiohttp
import discord
from redbot.core import Config, app_commands, checks, commands
from redbot.core.bot import Red

from . import card

log = logging.getLogger("red.freak_cogs.quoter")

__red_end_user_data_statement__ = "This cog does not store any user data."

FONTS = tuple(card.QUOTE_FONTS)
THEMES = card.THEMES
MAX_WATERMARK = 40
AVATAR_TIMEOUT = aiohttp.ClientTimeout(total=10)
AVATAR_BYTES = 4 * 1024 * 1024

USAGE = (
    "`[p]quote` - quote the message you replied to\n"
    "`[p]quote <message link or id>` - quote any message the bot can read\n\n"
    "Options: `--light`, `--dark`, `--gray`, `--colour`, `--watermark`, "
    "`--no-watermark`, `--font serif|sans|light|mono`, `--accent #5865f2`\n"
    "Right-click a message -> Apps -> **Quote** does the same."
)


class QuoteError(Exception):
    """An expected failure that is reported to the user instead of raised."""


def _as_bool(value: str) -> Optional[bool]:
    text = value.strip().lower()
    if text in {"on", "true", "yes", "y", "1", "an", "ein"}:
        return True
    if text in {"off", "false", "no", "n", "0", "aus"}:
        return False
    return None


def _slug(name: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9._-]+", "-", name).strip("-")
    return (slug or "quote")[:40]


class Quoter(commands.Cog):
    """Create an inspirational quote card from a message."""

    def __init__(self, bot: Red):
        self.bot = bot
        self.config = Config.get_conf(
            self, identifier="quoter", force_registration=True
        )
        self.config.register_guild(
            font="serif",
            theme="dark",
            grayscale=False,
            watermark=False,
            watermark_text="",
            accent=None,
        )
        self._session: Optional[aiohttp.ClientSession] = None
        self.quote_menu = app_commands.ContextMenu(
            name="Quote", callback=self._quote_menu
        )

    def cog_unload(self) -> None:
        try:
            self.bot.tree.remove_command(
                self.quote_menu.name, type=self.quote_menu.type
            )
        except Exception:  # pragma: no cover - the tree is gone during shutdown
            log.debug("Could not remove the Quote context menu", exc_info=True)

        session, self._session = self._session, None
        if session is None or session.closed:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        loop.create_task(session.close())

    async def cog_load(self) -> None:
        try:
            self.bot.tree.add_command(self.quote_menu)
        except app_commands.CommandAlreadyRegistered:  # pragma: no cover - reload edge
            log.debug("The Quote context menu was already registered")

    @property
    def session(self) -> aiohttp.ClientSession:
        """Own session: discord.py 2.7 no longer exposes ``bot.session``."""
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(timeout=AVATAR_TIMEOUT)
        return self._session

    # ------------------------------------------------------------------
    # commands
    # ------------------------------------------------------------------

    @commands.group(
        name="quote",
        aliases=["quoter"],
        invoke_without_command=True,
        usage="[message] [--options]",
    )
    @commands.guild_only()
    @checks.bot_has_permissions(attach_files=True)
    @commands.cooldown(4, 30, commands.BucketType.user)
    async def quote(self, ctx: commands.Context, *, raw: str = "") -> None:
        """Turn a message into an inspirational quote card.

        Reply to a message and run the command without arguments, or pass the
        link or ID of any message the bot can read.

        **Options**
        `--light` / `--dark` - card theme (overrides the server default)
        `--gray` / `--colour` - desaturated avatar and background
        `--watermark` / `--no-watermark` - stamp a small line at the bottom
        `--font serif|sans|light|mono` - the face of the quote
        `--accent #5865f2` - use a fixed accent colour instead of the avatar's
        """
        if not card.available():
            await self._send_error(
                ctx,
                "Pillow is missing on the bot host. Run `[p]pipinstall pillow`, "
                "then `[p]reload quoter`.",
            )
            return

        target, options, error = card.parse_invocation(raw, FONTS)
        if error:
            await self._send_error(ctx, f"{error}\n\n{USAGE}")
            return

        try:
            source = await self._resolve(ctx, target)
            png = await self._compose(source, ctx.guild, options)
        except QuoteError as exc:
            await self._send_error(ctx, str(exc))
            return

        await self._deliver(ctx, source, png, reply=True)

    @quote.group(name="set", invoke_without_command=True)
    @commands.guild_only()
    @commands.admin_or_permissions(manage_guild=True)
    async def quote_set(self, ctx: commands.Context) -> None:
        """Show or change what a quote card looks like on this server."""
        await self._show(ctx)

    @quote_set.command(name="font")
    @commands.guild_only()
    @commands.admin_or_permissions(manage_guild=True)
    async def quote_set_font(self, ctx: commands.Context, value: str) -> None:
        """Pick the face the quote is set in: serif, sans, light or mono."""
        font = value.strip().lower()
        if font not in FONTS:
            await self._send_error(
                ctx, f"Unknown font `{font}` - pick one of {self._font_list()}."
            )
            return
        await self.config.guild(ctx.guild).font.set(font)
        await ctx.send(f"Quote font set to **{font}**.")

    @quote_set.command(name="theme")
    @commands.guild_only()
    @commands.admin_or_permissions(manage_guild=True)
    async def quote_set_theme(self, ctx: commands.Context, value: str) -> None:
        """Pick the card theme: dark or light."""
        theme = value.strip().lower()
        if theme not in THEMES:
            await self._send_error(
                ctx, f"Unknown theme `{theme}` - pick `dark` or `light`."
            )
            return
        await self.config.guild(ctx.guild).theme.set(theme)
        await ctx.send(f"Quote theme set to **{theme}**.")

    @quote_set.command(name="grayscale")
    @commands.guild_only()
    @commands.admin_or_permissions(manage_guild=True)
    async def quote_set_grayscale(self, ctx: commands.Context, value: str) -> None:
        """Draw cards grayscale by default: on or off."""
        state = _as_bool(value)
        if state is None:
            await self._send_error(ctx, "Give me `on` or `off`.")
            return
        await self.config.guild(ctx.guild).grayscale.set(state)
        await ctx.send(f"Grayscale by default: **{'on' if state else 'off'}**.")

    @quote_set.command(name="watermark")
    @commands.guild_only()
    @commands.admin_or_permissions(manage_guild=True)
    async def quote_set_watermark(self, ctx: commands.Context, value: str) -> None:
        """Show a watermark by default: on or off."""
        state = _as_bool(value)
        if state is None:
            await self._send_error(ctx, "Give me `on` or `off`.")
            return
        await self.config.guild(ctx.guild).watermark.set(state)
        if state:
            await ctx.send(
                "Watermark **on**. Without custom text the card shows the bot's name; "
                "set one with `[p]quote set watermarktext <text>`."
            )
        else:
            await ctx.send("Watermark **off**.")

    @quote_set.command(name="watermarktext")
    @commands.guild_only()
    @commands.admin_or_permissions(manage_guild=True)
    async def quote_set_watermark_text(
        self, ctx: commands.Context, *, text: str = ""
    ) -> None:
        """Set the watermark text, or clear it by leaving the text out."""
        cleaned = " ".join(text.split())[:MAX_WATERMARK]
        await self.config.guild(ctx.guild).watermark_text.set(cleaned)
        if cleaned:
            await ctx.send(f"Watermark text set to **{cleaned}**.")
        else:
            await ctx.send("Watermark text cleared - the bot's name is used instead.")

    @quote_set.command(name="accent")
    @commands.guild_only()
    @commands.admin_or_permissions(manage_guild=True)
    async def quote_set_accent(self, ctx: commands.Context, value: str) -> None:
        """Fix the accent colour (`#5865f2`) or use `auto` for the avatar's colour."""
        accent = value.strip().lstrip("#").lower()
        if accent in {"auto", "avatar", "none", "off"}:
            await self.config.guild(ctx.guild).accent.set(None)
            await ctx.send("Accent colour follows the avatar again.")
            return
        if not re.fullmatch(r"[0-9a-f]{6}", accent):
            await self._send_error(
                ctx, f"`{value}` is not a hex colour like `#5865f2`."
            )
            return
        await self.config.guild(ctx.guild).accent.set(accent)
        await ctx.send(f"Accent colour fixed to `#{accent}`.")

    @quote_set.command(name="show")
    @commands.guild_only()
    @commands.admin_or_permissions(manage_guild=True)
    async def quote_set_show(self, ctx: commands.Context) -> None:
        """Show the current quote defaults."""
        await self._show(ctx)

    # ------------------------------------------------------------------
    # context menu
    # ------------------------------------------------------------------

    async def _quote_menu(
        self, interaction: discord.Interaction, message: discord.Message
    ) -> None:
        guild = interaction.guild
        if guild is None or interaction.channel is None:
            await interaction.response.send_message(
                "Quote cards only work in a server.", ephemeral=True
            )
            return
        if not card.available():
            await interaction.response.send_message(
                "Pillow is missing on the bot host - run `[p]pipinstall pillow`.",
                ephemeral=True,
            )
            return
        if not interaction.channel.permissions_for(guild.me).attach_files:
            await interaction.response.send_message(
                "I need the **Attach Files** permission in this channel.",
                ephemeral=True,
            )
            return

        await interaction.response.defer(thinking=True)
        try:
            png = await self._compose(message, guild, {})
        except QuoteError as exc:
            await interaction.followup.send(
                embed=discord.Embed(
                    description=str(exc),
                    colour=await self.bot.get_embed_color(interaction.channel),
                )
            )
            return
        except Exception:
            log.exception("Rendering a quote card from the context menu failed")
            await interaction.followup.send(
                embed=discord.Embed(
                    description="The card could not be drawn. The traceback is in the bot log.",
                    colour=await self.bot.get_embed_color(interaction.channel),
                )
            )
            return

        await interaction.followup.send(
            file=discord.File(
                io.BytesIO(png), filename=f"quote-{_slug(message.author.name)}.png"
            )
        )

    async def cog_app_command_error(
        self, interaction: discord.Interaction, error: Exception
    ) -> None:
        if isinstance(error, app_commands.CommandOnCooldown):
            text = f"Slow down - try again in {error.retry_after:.0f} s."
        else:
            log.exception("Quote context menu failed", exc_info=error)
            text = "Something went wrong while drawing the card. The traceback is in the bot log."
        try:
            if interaction.response.is_done():
                await interaction.followup.send(text, ephemeral=True)
            else:
                await interaction.response.send_message(text, ephemeral=True)
        except discord.HTTPException:  # pragma: no cover - expired interaction
            log.debug("Could not answer a failed Quote interaction")

    # ------------------------------------------------------------------
    # shared work
    # ------------------------------------------------------------------

    async def _resolve(
        self, ctx: commands.Context, target: Optional[str]
    ) -> discord.Message:
        if target:
            try:
                return await commands.MessageConverter().convert(ctx, target)
            except commands.BadArgument:
                raise QuoteError(
                    "I could not find that message. Pass a message link or an ID from "
                    "this channel, or reply to the message instead."
                ) from None

        reference = ctx.message.reference
        if reference is None:
            raise QuoteError("Reply to a message or pass its link or ID.\n\n" + USAGE)

        resolved = reference.resolved
        if isinstance(resolved, discord.Message):
            return resolved
        try:
            return await ctx.channel.fetch_message(reference.message_id)
        except discord.NotFound:
            raise QuoteError("The message you replied to is gone.") from None
        except discord.Forbidden:
            raise QuoteError("I cannot read the message you replied to.") from None

    async def _compose(
        self,
        message: discord.Message,
        guild: Optional[discord.Guild],
        options: Dict[str, Any],
    ) -> bytes:
        text = card.clean_message(message.content, resolver=self._resolver(guild))
        if not text:
            raise QuoteError(
                "That message has no text to quote - attachments, stickers and embeds "
                "are not drawn."
            )

        settings = await self._settings(guild)
        theme = options.get("theme") or settings["theme"]
        font = options.get("font") or settings["font"]
        if "grayscale" in options:
            grayscale = bool(options["grayscale"])
        else:
            grayscale = bool(settings["grayscale"])
        if "watermark" in options:
            watermark_on = bool(options["watermark"])
        else:
            watermark_on = bool(settings["watermark"])
        accent = options.get("accent") or settings["accent"]

        watermark = None
        if watermark_on:
            watermark = settings["watermark_text"] or self._bot_name(guild)

        avatar = await self._avatar(message.author)
        author = message.author.display_name

        loop = asyncio.get_running_loop()
        try:
            return await loop.run_in_executor(
                None,
                functools.partial(
                    card.render,
                    text=text,
                    author=author,
                    avatar=avatar,
                    theme=theme,
                    font=font,
                    grayscale=grayscale,
                    watermark=watermark,
                    accent=accent,
                ),
            )
        except Exception as exc:
            log.exception("Drawing a quote card failed")
            raise QuoteError(
                "The card could not be drawn. The traceback is in the bot log."
            ) from exc

    async def _avatar(self, user: discord.abc.User) -> Optional[bytes]:
        try:
            asset = user.display_avatar.replace(size=256, static_format="png")
            async with self.session.get(asset.url) as response:
                if response.status != 200:
                    log.debug(
                        "Avatar fetch returned %s for %s", response.status, user.id
                    )
                    return None
                if response.content_length and response.content_length > AVATAR_BYTES:
                    return None
                data = await response.read()
        except (aiohttp.ClientError, asyncio.TimeoutError):
            log.debug("Avatar fetch failed for %s", user.id, exc_info=True)
            return None
        return data if len(data) <= AVATAR_BYTES else None

    async def _deliver(
        self, ctx: commands.Context, source: discord.Message, png: bytes, *, reply: bool
    ) -> None:
        filename = f"quote-{_slug(source.author.name)}.png"
        reference = source.to_reference(fail_if_not_exists=False) if reply else None
        try:
            await ctx.send(
                file=discord.File(io.BytesIO(png), filename=filename),
                reference=reference,
                mention_author=False,
            )
        except discord.HTTPException:
            log.debug(
                "Replying to the source message failed, sending without a reference"
            )
            await ctx.send(file=discord.File(io.BytesIO(png), filename=filename))

    async def _settings(self, guild: Optional[discord.Guild]) -> Dict[str, Any]:
        if guild is None:
            return {
                "font": "serif",
                "theme": "dark",
                "grayscale": False,
                "watermark": False,
                "watermark_text": "",
                "accent": None,
            }
        stored = await self.config.guild(guild).all()
        return {
            "font": stored.get("font") if stored.get("font") in FONTS else "serif",
            "theme": stored.get("theme") if stored.get("theme") in THEMES else "dark",
            "grayscale": bool(stored.get("grayscale")),
            "watermark": bool(stored.get("watermark")),
            "watermark_text": stored.get("watermark_text") or "",
            "accent": stored.get("accent"),
        }

    def _bot_name(self, guild: Optional[discord.Guild]) -> str:
        if guild is not None and guild.me is not None:
            return guild.me.display_name
        return self.bot.user.display_name if self.bot.user else "quoter"

    def _resolver(self, guild: Optional[discord.Guild]):
        def resolve(kind: str, snowflake: str) -> Optional[str]:
            if guild is None:
                return None
            try:
                identifier = int(snowflake)
            except ValueError:
                return None
            if kind == "user":
                member = guild.get_member(identifier)
                return member.display_name if member else None
            if kind == "role":
                role = guild.get_role(identifier)
                return role.name if role else None
            if kind == "channel":
                channel = guild.get_channel(identifier)
                return channel.name if channel else None
            return None

        return resolve

    async def _show(self, ctx: commands.Context) -> None:
        settings = await self._settings(ctx.guild)
        watermark = settings["watermark_text"] or (
            f"the bot's name ({self._bot_name(ctx.guild)})"
        )
        embed = discord.Embed(
            title="Quote cards",
            colour=await ctx.embed_color(),
            description=(
                f"Font: **{settings['font']}**\n"
                f"Theme: **{settings['theme']}**\n"
                f"Grayscale: **{'on' if settings['grayscale'] else 'off'}**\n"
                f"Watermark: **{'on' if settings['watermark'] else 'off'}** - {watermark}\n"
                f"Accent: **{'#' + settings['accent'] if settings['accent'] else 'follows the avatar'}**"
            ),
        )
        embed.set_footer(
            text="quote set font | theme | grayscale | watermark | watermarktext | accent"
        )
        await ctx.send(embed=embed)

    async def _send_error(self, ctx: commands.Context, text: str) -> None:
        embed = discord.Embed(description=text, colour=await ctx.embed_color())
        try:
            await ctx.send(embed=embed)
        except discord.Forbidden:
            await ctx.send(text)

    def _font_list(self) -> str:
        return ", ".join(f"`{name}`" for name in FONTS)
