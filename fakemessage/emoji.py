"""Emoji pictures for the renderer: Twemoji for unicode, the CDN for custom ones.

Discord draws emoji with Twemoji, so the screenshots should too. The local
Noto Color Emoji build is a single 109 px bitmap strike: downscaled to 20 px it
looks like pixel art, and its ZWJ ligatures do not compose, so a family emoji
came out as one legacy glyph. Twemoji PNGs are 72 px vectors-to-bitmap and
match what the client shows.

Both sources are cached on disk under the cog's data path, so a second render
of the same emoji costs nothing and an offline bot still renders the emoji it
has seen before. Nothing here imports discord: the cog passes the session.
"""

from __future__ import annotations

import io
import os
from typing import Dict, Iterable, Optional

import aiohttp
from PIL import Image

TWEMOJI_URL = "https://cdn.jsdelivr.net/gh/jdecked/twemoji@15.1.0/assets/72x72/{name}.png"
CUSTOM_URL = "https://cdn.discordapp.com/emojis/{id}.png?size=128"
TIMEOUT = aiohttp.ClientTimeout(total=8)
MAX_PER_RENDER = 40


def twemoji_name(cluster: str) -> str:
    """Twemoji file name for a cluster: hex code points, FE0F dropped."""
    codes = []
    for char in cluster:
        code = ord(char)
        if code in (0xFE0F, 0xFE0E):
            continue
        codes.append("{:x}".format(code))
    return "-".join(codes)


class EmojiCache:
    """Disk + memory cache of small emoji PNGs, keyed by cluster or emoji id."""

    def __init__(self, directory: str):
        self.directory = directory
        self._memory: Dict[str, Image.Image] = {}
        self._failed: set[str] = set()
        os.makedirs(directory, exist_ok=True)

    # -- public ----------------------------------------------------------
    async def load_unicode(
        self, session: aiohttp.ClientSession, clusters: Iterable[str]
    ) -> Dict[str, Image.Image]:
        out: Dict[str, Image.Image] = {}
        for cluster in list(dict.fromkeys(clusters))[:MAX_PER_RENDER]:
            name = twemoji_name(cluster)
            if not name:
                continue
            image = await self._fetch(session, cluster, "u-" + name, TWEMOJI_URL.format(name=name))
            if image is not None:
                out[cluster] = image
        return out

    async def load_custom(
        self, session: aiohttp.ClientSession, ids: Iterable[str]
    ) -> Dict[str, Image.Image]:
        out: Dict[str, Image.Image] = {}
        for emoji_id in list(dict.fromkeys(ids))[:MAX_PER_RENDER]:
            if not emoji_id.isdigit():
                continue
            image = await self._fetch(
                session, emoji_id, "c-" + emoji_id, CUSTOM_URL.format(id=emoji_id)
            )
            if image is not None:
                out[emoji_id] = image
        return out

    # -- internals -------------------------------------------------------
    def _path(self, key: str) -> str:
        return os.path.join(self.directory, key + ".png")

    async def _fetch(
        self,
        session: aiohttp.ClientSession,
        key: str,
        stem: str,
        url: str,
    ) -> Optional[Image.Image]:
        cached = self._memory.get(key)
        if cached is not None:
            return cached
        if key in self._failed:
            return None
        path = self._path(stem)
        if os.path.exists(path):
            image = self._read(path)
            if image is not None:
                self._memory[key] = image
                return image
        try:
            async with session.get(url, timeout=TIMEOUT) as response:
                if response.status != 200:
                    self._failed.add(key)
                    return None
                data = await response.read()
        except (aiohttp.ClientError, OSError, TimeoutError):
            self._failed.add(key)
            return None
        image = self._decode(data)
        if image is None:
            self._failed.add(key)
            return None
        try:
            with open(path, "wb") as handle:
                handle.write(data)
        except OSError:
            pass
        self._memory[key] = image
        return image

    @staticmethod
    def _decode(data: bytes) -> Optional[Image.Image]:
        try:
            image = Image.open(io.BytesIO(data))
            image.load()
        except (OSError, ValueError):
            return None
        return image.convert("RGBA")

    def _read(self, path: str) -> Optional[Image.Image]:
        try:
            with open(path, "rb") as handle:
                return self._decode(handle.read())
        except OSError:
            return None
