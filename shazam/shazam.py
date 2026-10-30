"""shazam - a clean, single-file rewrite of the old shazam cog.

Why a rewrite instead of another patch:

* the old cog reached into shazamio's pydantic models, so a slightly different
  payload from Shazam ended as a traceback in the log instead of a message
* it only ever sent the first 10 seconds, and only the attachment path caught
  a "no match" answer
* a Discord CDN link that had expired was handed to ffmpeg as if it were audio

Credits: the command surface comes from the `shazam` cog in Seina-Cogs by
japandotorg (MIT), which this replaces; recognition is done by shazamio
(https://github.com/shazamio/ShazamIO, MIT), which talks to Shazam's own API.

This version keeps everything in one file, reads the raw Shazam payload (no
model layer in between), tries three 12 s windows (the length the Shazam apps
themselves send), and turns every failure into a sentence the user can act on.
"""

from __future__ import annotations

import io
import logging
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as pkg_version
from typing import Any, Dict, List, Optional, Tuple

import aiohttp
import discord
from pydub import AudioSegment
from redbot.core import app_commands, commands
from redbot.core.bot import Red
from shazamio import Shazam as ShazamLib
from shazamio.api import SearchParams

log = logging.getLogger("red.freak_cogs.shazam")


def _lib_version() -> str:
    try:
        return pkg_version("shazamio")
    except PackageNotFoundError:  # pragma: no cover - only without the package
        return "?"

MAX_BYTES = 25 * 1024 * 1024
AUDIO_TYPES = ("audio/", "video/")
MEDIA_CTYPES = {
    "application/octet-stream",
    "binary/octet-stream",
    "application/mp4",
    "application/ogg",
    "application/x-mpegurl",
}


class NotMedia(Exception):
    """The input was not audio or video at all."""


class NoMatch(Exception):
    """Shazam looked at the audio and found nothing."""


class Recognition:
    """Core recognition logic, independent of Discord."""

    # 12 s is the window the Shazam apps themselves send; shazamio 0.9.0 warns
    # explicitly when you deviate from it.
    def __init__(self, session: aiohttp.ClientSession, window: int = 12, attempts: int = 3):
        self.session = session
        self.window = window
        self.max_attempts = attempts
        self.lib = ShazamLib()

    # -- audio plumbing ----------------------------------------------------
    @staticmethod
    def duration(data: bytes) -> float:
        return float(AudioSegment.from_file(io.BytesIO(data)).duration_seconds)

    @staticmethod
    def cut(data: bytes, start: float, length: float) -> bytes:
        audio = AudioSegment.from_file(io.BytesIO(data))
        chunk = audio[int(start * 1000) : int((start + length) * 1000)]
        buffer = io.BytesIO()
        chunk.export(buffer, format="wav")
        return buffer.getvalue()

    def windows(self, total: float) -> List[Tuple[str, float, int]]:
        """(label, start, seconds) - the same length every time, other offsets.

        Measured with music buried under noise: a 10 s window matched, 15 s and
        30 s did not (more noise per signature). A different window helps when
        the music only starts later in the clip.
        """
        span = max(1, min(int(total) if total else self.window, self.window))
        # a shorter clip is used whole, anything longer is cut into 12 s windows
        plan: List[Tuple[str, float, int]] = [("start", 0.0, span)]
        if total >= span + 5:
            middle = int(total / 3)
            plan.append(("middle", float(middle), span))
            tail = int(total) - span
            if tail > middle + 1:
                plan.append(("end", float(tail), span))
        return plan[: self.max_attempts]

    async def fetch(self, url: str) -> bytes:
        async with self.session.get(url, timeout=aiohttp.ClientTimeout(total=120)) as response:
            if response.status != 200:
                raise NotMedia(f"that url answered with HTTP {response.status}")
            ctype = (response.headers.get("Content-Type") or "").split(";")[0].strip().lower()
            if ctype and not (
                ctype.startswith(AUDIO_TYPES) or ctype in MEDIA_CTYPES
            ):
                raise NotMedia(
                    "that url returned {} instead of audio or video - Discord "
                    "attachment links expire after a while".format(ctype or "no content type")
                )
            return await response.read()

    async def recognize(self, data: bytes, *, debug: bool = False) -> Tuple[Dict[str, Any], List[str]]:
        """Return (track, trace). Raises NoMatch / NotMedia."""
        if not data:
            raise NotMedia("the file is empty")

        notes: List[str] = []
        try:
            total = self.duration(data)
            notes.append("clip {:.1f}s".format(total))
        except Exception as error:  # noqa: BLE001 - unreadable: one blind attempt
            raise NotMedia("I could not read any audio out of that file") from error

        for label, start, seconds in self.windows(total):
            chunk = data if not start else self.cut(data, start, seconds)
            try:
                answer = await self.lib.recognize(
                    chunk, options=SearchParams(segment_duration_seconds=seconds)
                )
            except Exception as error:  # noqa: BLE001 - Rust-Seite, z.B. kein Audio
                notes.append("{}: {}".format(label, type(error).__name__))
                if label == "start":
                    raise NotMedia("I could not read any audio out of that file") from error
                continue

            track = answer.get("track") if isinstance(answer, dict) else None
            if track:
                notes.append("{}: match".format(label))
                return track, notes if debug else []
            notes.append("{}: no match".format(label))

        raise NoMatch(" | ".join(notes))


def build_embed(track: Dict[str, Any], notes: Optional[List[str]] = None) -> discord.Embed:
    title = track.get("title") or "unknown title"
    artist = track.get("subtitle") or "unknown artist"
    share = track.get("share") or {}
    url = share.get("href") or track.get("url") or "https://www.shazam.com"
    cover = (track.get("images") or {}).get("coverart")

    lines: List[str] = []
    sections = track.get("sections") or []
    if sections:
        for entry in (sections[0].get("metadata") or [])[:4]:
            key, value = entry.get("title"), entry.get("text")
            if key and value:
                lines.append("- **{}**: {}".format(key, value))
    genre = (track.get("genres") or {}).get("primary")
    if genre:
        lines.append("- **Genre**: {}".format(genre))

    embed = discord.Embed(
        title="{} - {}".format(title, artist),
        url=url,
        description="\n".join(lines) or None,
        colour=discord.Colour.from_str("#39ff88"),
    )
    if cover:
        embed.set_thumbnail(url=cover)
    if notes:
        embed.set_footer(text=" · ".join(notes), icon_url="https://i.imgur.com/USbgv50h.jpg")
    else:
        embed.set_footer(text="shazam.com", icon_url="https://i.imgur.com/USbgv50h.jpg")
    return embed


class Shazam(commands.Cog):
    """Recognize a song from an uploaded file, a replied message or a direct link."""

    __author__ = "cens0r3d"
    __version__ = "0.1.0"

    def __init__(self, bot: Red) -> None:
        self.bot = bot
        self.session = aiohttp.ClientSession()
        self.recognition = Recognition(self.session)

    async def cog_unload(self) -> None:
        await self.session.close()

    # -- Quellen -----------------------------------------------------------
    async def _attachment(self, ctx: commands.Context) -> Optional[discord.Attachment]:
        if ctx.message.attachments:
            return ctx.message.attachments[0]
        reference = ctx.message.reference
        if not reference:
            return None
        message = reference.resolved
        if message is None and reference.message_id:
            try:
                message = await ctx.channel.fetch_message(reference.message_id)
            except (discord.HTTPException, AttributeError):
                message = None
        if message is None or isinstance(message, discord.DeletedReferencedMessage):
            return None
        if getattr(message, "attachments", None):
            return message.attachments[0]
        return None

    async def _collect(self, ctx: commands.Context, url: Optional[str]) -> bytes:
        if url:
            return await self.recognition.fetch(url)
        attachment = await self._attachment(ctx)
        if attachment is None:
            raise NotMedia(
                "give me a file, reply to a message with a file, or pass a direct "
                "media url"
            )
        if attachment.size > 25 * 1024 * 1024:
            raise NotMedia("that file is larger than 25 MB")
        return await attachment.read()

    async def _run(
        self, data: bytes, debug: bool
    ) -> Tuple[Optional[Dict[str, Any]], List[str], Optional[str]]:
        """(track, notes, problem) - problem is a sentence ready for the user."""
        try:
            track, notes = await self.recognition.recognize(data, debug=debug)
            return track, notes, None
        except NotMedia as error:
            return None, [], "That is nothing I can listen to: {}.".format(error)
        except NoMatch as error:
            return None, [], "I could not recognize any song. Tried: {}.".format(
                str(error).replace(" | ", ", ")
            )
        except Exception as error:  # noqa: BLE001 - never a traceback in chat
            log.exception("shazam failed")
            return None, [], "Recognition failed ({}). Try again in a moment.".format(
                type(error).__name__
            )

    # -- Befehle -----------------------------------------------------------
    @commands.command(name="shazam", aliases=["sht"])
    @commands.guild_only()
    async def shazam(self, ctx: commands.Context, *, url: Optional[str] = None) -> None:
        """Recognize the song in an audio or video file.

        Attach a file, reply to a message with one, or pass a direct media url.
        Add `debug` as the last word to see which windows were tried.
        """
        debug = False
        if url and url.strip().lower().endswith("debug"):
            debug = True
            url = url[: -len("debug")].strip() or None

        async with ctx.typing():
            try:
                data = await self._collect(ctx, url)
            except NotMedia as error:
                await ctx.reply(
                    "That is nothing I can listen to: {}.".format(error), mention_author=False
                )
                return
            track, notes, problem = await self._run(data, debug)

        if problem:
            await ctx.reply(problem, mention_author=False)
            return
        await ctx.reply(embed=build_embed(track, notes), mention_author=False)

    @app_commands.command(
        name="shazam", description="Recognize the song in an audio or video file."
    )
    @app_commands.describe(
        attachment="Audio or video file to listen to",
        url="Direct link to an audio or video file",
        debug="Show which windows were tried",
    )
    async def shazam_slash(
        self,
        interaction: discord.Interaction,
        attachment: Optional[discord.Attachment] = None,
        url: Optional[str] = None,
        debug: bool = False,
    ) -> None:
        await interaction.response.defer(thinking=True)

        try:
            if attachment is not None:
                if attachment.size > MAX_BYTES:
                    raise NotMedia("that file is larger than 25 MB")
                data = await attachment.read()
            elif url:
                data = await self.recognition.fetch(url)
            else:
                await interaction.followup.send(
                    "Attach a file or pass a url - I cannot listen to nothing.",
                    ephemeral=True,
                )
                return
        except NotMedia as error:
            await interaction.followup.send(
                "That is nothing I can listen to: {}.".format(error), ephemeral=True
            )
            return

        track, notes, problem = await self._run(data, debug)
        if problem:
            await interaction.followup.send(problem, ephemeral=True)
            return
        await interaction.followup.send(embed=build_embed(track, notes))

    @commands.command(name="shazamver")
    async def shazamver(self, ctx: commands.Context) -> None:
        """Show which version of shazam is loaded."""
        await ctx.send("shazam {} (shazamio {})".format(self.__version__, _lib_version()))
