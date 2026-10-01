import asyncio
import base64
import io
import json
import logging
import struct
from typing import Optional, Union

import discord
from redbot.core import commands

log = logging.getLogger("red.freak_cogs.voicesay")


class VoiceSay(commands.Cog):
    """Send attached audio files as native Discord voice messages."""

    def __init__(self, bot):
        self.bot = bot

    async def red_delete_data_for_user(self, *, requester, user_id):
        pass

    @commands.command(name="voicesay")
    @commands.bot_has_permissions(send_messages=True, attach_files=True)
    async def voicesay(
        self,
        ctx: commands.Context,
        destination: Union[discord.TextChannel, discord.User, discord.Member, int],
        *,
        text: Optional[str] = None,
    ):
        """
        Send an attached audio file as a native voice message.

        Usage:
            [p]voicesay #channel   (with audio attached)
            [p]voicesay @user      (with audio attached)
            [p]voicesay 123456789  (with audio attached)

        Optional text can be added after the destination.
        """
        if not ctx.message.attachments:
            await ctx.send("You need to attach an audio file.")
            return

        attachment = ctx.message.attachments[0]
        if attachment.content_type is None or not attachment.content_type.startswith(
            "audio/"
        ):
            await ctx.send("The attachment must be an audio file.")
            return

        # Resolve raw IDs to a channel or user.
        if isinstance(destination, int):
            resolved = self.bot.get_channel(destination) or self.bot.get_user(
                destination
            )
            if resolved is None:
                try:
                    resolved = await self.bot.fetch_user(destination)
                except discord.NotFound:
                    await ctx.send("Could not find a channel or user with that ID.")
                    return
            destination = resolved

        # Open a DM if a user was passed.
        if isinstance(destination, discord.abc.User):
            try:
                destination = await destination.create_dm()
            except discord.HTTPException as exc:
                await ctx.send(f"Could not open a DM: {exc}")
                return

        # From here on destination must be a real messageable channel.
        if not isinstance(destination, discord.abc.Messageable):
            await ctx.send("Destination is not a valid text channel or DM.")
            return

        # Permission check for guild channels.
        if isinstance(destination, discord.TextChannel):
            perms = destination.permissions_for(destination.guild.me)
            if not perms.send_messages or not perms.attach_files:
                await ctx.send(
                    "I don't have permission to send messages or attachments in that channel."
                )
                return

        async with ctx.typing():
            try:
                audio_bytes = await attachment.read()
                ogg_bytes = await self._ensure_ogg_opus(
                    audio_bytes, attachment.filename
                )
                duration = await self._get_duration(ogg_bytes)
                waveform = await self._generate_waveform(ogg_bytes)
                await self._send_voice_message(
                    destination, ogg_bytes, duration, waveform, text
                )
            except commands.UserFeedbackCheckFailure as exc:
                await ctx.send(str(exc))
                return
            except Exception:
                log.exception("Failed to send voice message")
                await ctx.send("Failed to send the voice message.")
                return

        dest_name = getattr(destination, "mention", str(destination))
        await ctx.send(f"Voice message sent to {dest_name}.")

    async def _ensure_ogg_opus(self, audio_bytes: bytes, filename: str) -> bytes:
        """
        Convert the uploaded audio to OGG Opus.

        Discord voice messages must be OGG Opus. If ffmpeg is missing and the
        file is already .ogg/.opus, it is sent as-is.
        """
        try:
            proc = await asyncio.create_subprocess_exec(
                "ffmpeg",
                "-hide_banner",
                "-loglevel",
                "error",
                "-i",
                "pipe:0",
                "-c:a",
                "libopus",
                "-b:a",
                "32k",
                "-vbr",
                "on",
                "-compression_level",
                "10",
                "-f",
                "ogg",
                "pipe:1",
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await proc.communicate(audio_bytes)
            if proc.returncode == 0 and stdout:
                return stdout
            log.warning("ffmpeg conversion failed: %s", stderr.decode())
        except FileNotFoundError:
            pass
        except Exception:
            log.exception("ffmpeg conversion error")

        if filename.lower().endswith((".ogg", ".opus")):
            return audio_bytes

        raise commands.UserFeedbackCheckFailure(
            "ffmpeg is required to convert this audio file. "
            "Please install ffmpeg or upload an .ogg/.opus file."
        )

    async def _get_duration(self, ogg_bytes: bytes) -> float:
        """Return the audio duration in seconds."""
        try:
            proc = await asyncio.create_subprocess_exec(
                "ffprobe",
                "-v",
                "error",
                "-show_entries",
                "format=duration",
                "-of",
                "default=noprint_wrappers=1:nokey=1",
                "-i",
                "pipe:0",
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, _ = await proc.communicate(ogg_bytes)
            if proc.returncode == 0:
                return round(float(stdout.decode().strip()), 2)
        except FileNotFoundError:
            pass
        except Exception:
            log.exception("ffprobe duration error")

        # Fallback: parse the last Ogg page granule position.
        try:
            return self._parse_ogg_duration(ogg_bytes)
        except Exception:
            pass

        return 1.0

    def _parse_ogg_duration(self, data: bytes) -> float:
        """Rough duration parser for OGG Opus files."""
        granule = 0
        i = 0
        while i < len(data) - 27:
            if data[i : i + 4] != b"OggS":
                i += 1
                continue
            granule = struct.unpack_from("<q", data, i + 6)[0]
            segments = data[i + 26]
            segment_table = data[i + 27 : i + 27 + segments]
            body_size = sum(segment_table)
            i += 27 + segments + body_size

        if granule > 0:
            return round(granule / 48000.0, 2)
        return 1.0

    async def _generate_waveform(self, ogg_bytes: bytes, samples: int = 100) -> str:
        """
        Generate a Discord waveform from the audio amplitude.

        Returns a base64-encoded byte array of amplitude values (0-255).
        """
        try:
            proc = await asyncio.create_subprocess_exec(
                "ffmpeg",
                "-hide_banner",
                "-loglevel",
                "error",
                "-i",
                "pipe:0",
                "-ac",
                "1",
                "-ar",
                "1000",
                "-f",
                "s16le",
                "pipe:1",
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, _ = await proc.communicate(ogg_bytes)
            if proc.returncode == 0 and stdout:
                return self._pcm_to_waveform(stdout, samples)
        except FileNotFoundError:
            pass
        except Exception:
            log.exception("ffmpeg waveform error")

        # Placeholder waveform if ffmpeg is unavailable.
        return "FzYACgAAAAAAACQAAAAAAAA="

    def _pcm_to_waveform(self, pcm_bytes: bytes, samples: int) -> str:
        """Convert 16-bit mono PCM data to a Discord waveform byte string."""
        if len(pcm_bytes) < 2:
            return "FzYACgAAAAAAACQAAAAAAAA="

        sample_count = len(pcm_bytes) // 2
        step = max(1, sample_count // samples)

        waveform = bytearray()
        for i in range(0, sample_count, step):
            if len(waveform) >= samples:
                break
            sample = struct.unpack_from("<h", pcm_bytes, i * 2)[0]
            amplitude = int((abs(sample) / 32768.0) * 255)
            waveform.append(amplitude)

        return base64.b64encode(waveform).decode("ascii")

    async def _send_voice_message(
        self,
        channel: discord.abc.Messageable,
        audio_bytes: bytes,
        duration: float,
        waveform: str,
        text: Optional[str],
    ):
        """Send audio as a native Discord voice message using the internal hack."""
        buffer = io.BytesIO(audio_bytes)
        buffer.seek(0)
        file = discord.File(buffer, filename="voice-message.ogg")

        params = discord.http.handle_message_parameters(file=file)

        payload = {
            "flags": 8192,
            "attachments": [
                {
                    "id": 0,
                    "filename": "voice-message.ogg",
                    "duration_secs": duration,
                    "waveform": waveform,
                }
            ],
        }
        if text:
            payload["content"] = text

        payload_json = json.dumps(payload)

        # Replace the JSON payload in the multipart form.
        for part in params.multipart:
            if part.get("name") == "payload_json":
                part["value"] = payload_json
                break
        else:
            params.multipart[0]["value"] = payload_json

        await channel._state.http.send_message(channel.id, params=params)
