"""DBD — Dead by Daylight information for Red-DiscordBot.

Data comes from the community JSON API behind <https://dbd.tricky.lol>, which mirrors the
current game build (killers, survivors, perks, items, offerings, maps).  The payloads are
fetched once, cached in memory for a few hours and on disk under the cog's data directory,
so a restart or a short outage of the API does not empty the commands.

Icons are optional eye candy: one GitHub mirror keeps every icon in a single tree
(survivor and killer portraits, perks per role, items, offerings, maps), the cog reads that
listing and links a file only after confirming it exists. A second mirror covers perk gaps.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple
from urllib.parse import quote

import aiohttp
import discord
from redbot.core import commands
from redbot.core.data_manager import cog_data_path
from redbot.core.utils.chat_formatting import humanize_list, pagify
from redbot.core.utils.menus import DEFAULT_CONTROLS, menu

from .dbd_data import (
    ITEM_TYPES,
    build_index,
    build_token_index,
    chunk,
    clean,
    difficulty_parts,
    killer_speed,
    lookup,
    norm,
    perk_text,
    rarity_parts,
    resolve_icon,
    resolve_placeholders,
    role_parts,
    suggest,
    terror_radius,
    title_case_tag,
    trim,
)

__red_end_user_data_statement__ = "This cog does not store any End User Data."

log = logging.getLogger("red.freak_cogs.dbd")

API_BASE = "https://dbd.tricky.lol/api"
API_SITE = "https://dbd.tricky.lol"
CACHE_TTL = 12 * 3600
ICON_TTL = 3 * 86400
ICON_CACHE_VERSION = 2
REQUEST_TIMEOUT = 20

# One mirror keeps every icon in a single tree, so one recursive listing maps them all:
# survivor *and* killer portraits, perks per role, items, offerings and maps.
ICON_TREE_REPO = "Zawgalicious/DBDImages"
ICON_TREE_API = (
    f"https://api.github.com/repos/{ICON_TREE_REPO}/git/trees/main?recursive=1"
)
ICON_RAW_BASE = f"https://raw.githubusercontent.com/{ICON_TREE_REPO}/main"
ICON_FOLDERS = {
    "images/characters/survivor/": "portrait_survivor",
    "images/characters/killer/": "portrait_killer",
    "images/perks/survivor/": "perk_survivor",
    "images/perks/killer/": "perk_killer",
    "images/items/": "item",
    "images/offerings/": "offering",
    "images/maps/": "map",
}
# Second opinion for perks only, filling whatever the tree above is missing.
ICON_FALLBACKS = {
    "perk_survivor": "profsoru/dbd-perk-icons",
    "perk_killer": "profsoru/dbd-killer-perk-icons",
}

# payload key -> what the API calls it
ENDPOINTS = {
    "characters": "characters",
    "perks": "perks",
    "items": "items",
    "offerings": "offerings",
    "maps": "maps",
}

KINDS = {
    "killer": ("characters", "killer"),
    "killers": ("characters", "killer"),
    "survivor": ("characters", "survivor"),
    "survivors": ("characters", "survivor"),
    "perk": ("perks", None),
    "perks": ("perks", None),
    "item": ("items", None),
    "items": ("items", None),
    "offering": ("offerings", None),
    "offerings": ("offerings", None),
    "map": ("maps", None),
    "maps": ("maps", None),
}


class DbdError(Exception):
    """Raised when the data source is unreachable and no cache exists."""


class DBD(commands.Cog):
    """Dead by Daylight: killers, survivors, perks, items, offerings and maps."""

    def __init__(self, bot: Any) -> None:
        self.bot = bot
        self.session: Optional[aiohttp.ClientSession] = None
        self._data: Dict[str, Dict[str, Dict[str, Any]]] = {}
        self._stamp: float = 0.0
        self._data_lock = asyncio.Lock()
        self._icons: Dict[str, Dict[str, str]] = {}
        self._icons_extra: Dict[str, Dict[str, str]] = {}
        self._icons_stamp: float = 0.0
        self._icon_lock = asyncio.Lock()
        self._warm_task: Optional[asyncio.Task] = None
        self._indexes: Dict[str, Dict[str, str]] = {}
        self._token_indexes: Dict[str, Dict[str, List[str]]] = {}

    # ------------------------------------------------------------------ lifecycle

    async def cog_load(self) -> None:
        self.session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=REQUEST_TIMEOUT),
            headers={
                "User-Agent": "Red-DiscordBot dbd cog (+https://github.com/cens0r3d/freak-cogs)"
            },
        )
        self._warm_task = asyncio.create_task(self._warm())

    def cog_unload(self) -> None:
        if self._warm_task is not None:
            self._warm_task.cancel()
            self._warm_task = None
        session, self.session = self.session, None
        if session is not None and not session.closed:
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                loop = None
            if loop is not None:
                loop.create_task(session.close())
            else:
                log.debug(
                    "no running loop on unload, leaving the aiohttp session to GC"
                )

    async def _warm(self) -> None:
        """Best effort preload so the first command does not carry the latency."""
        with contextlib.suppress(Exception):
            await self._load()
        with contextlib.suppress(Exception):
            await self._ensure_icons()

    # ------------------------------------------------------------------ data layer

    @property
    def _cache_file(self):
        return cog_data_path(self) / "dbd_data.json"

    @property
    def _icon_file(self):
        return cog_data_path(self) / "dbd_icons.json"

    async def _get_json(self, url: str) -> Any:
        if self.session is None or self.session.closed:
            self.session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=REQUEST_TIMEOUT),
                headers={"User-Agent": "Red-DiscordBot dbd cog"},
            )
        async with self.session.get(url) as response:
            if response.status != 200:
                raise DbdError(f"{url} returned HTTP {response.status}")
            return await response.json(content_type=None)

    def _read_cache(self, path, max_age: float) -> Optional[dict]:
        try:
            raw = path.read_text(encoding="utf-8")
        except OSError:
            return None
        try:
            payload = json.loads(raw)
        except ValueError:
            return None
        if not isinstance(payload, dict):
            return None
        if time.time() - float(payload.get("stamp", 0)) > max_age:
            return None
        return payload

    def _write_cache(self, path, payload: dict) -> None:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(payload), encoding="utf-8")
        except OSError:
            log.debug("could not write cache file %s", path, exc_info=True)

    async def _load(self, force: bool = False) -> Dict[str, Dict[str, Dict[str, Any]]]:
        """Return the payload dict, from memory, disk or the API - in that order."""
        if self._data and not force and time.time() - self._stamp < CACHE_TTL:
            return self._data

        async with self._data_lock:
            if self._data and not force and time.time() - self._stamp < CACHE_TTL:
                return self._data

            if not force:
                cached = self._read_cache(self._cache_file, CACHE_TTL * 2)
                if cached and isinstance(cached.get("data"), dict):
                    self._data = cached["data"]
                    self._stamp = float(cached.get("stamp", time.time()))
                    self._clear_indexes()
                    return self._data

            try:
                results = await asyncio.gather(
                    *(
                        self._get_json(f"{API_BASE}/{endpoint}")
                        for endpoint in ENDPOINTS.values()
                    )
                )
            except Exception as error:  # noqa: BLE001 - reported as an embed
                fallback = self._read_cache(self._cache_file, max_age=10**9)
                if fallback and isinstance(fallback.get("data"), dict):
                    self._data = fallback["data"]
                    self._stamp = float(fallback.get("stamp", 0))
                    self._clear_indexes()
                    log.warning("DBD API unreachable (%s), serving stale cache", error)
                    return self._data
                raise DbdError(
                    f"the DBD data source is unreachable ({error})"
                ) from error

            data: Dict[str, Dict[str, Dict[str, Any]]] = {}
            for index, name in enumerate(ENDPOINTS.keys()):
                result = results[index]
                data[name] = result if isinstance(result, dict) else {}
            self._data = data
            self._stamp = time.time()
            self._clear_indexes()
            self._write_cache(self._cache_file, {"stamp": self._stamp, "data": data})
            log.info(
                "DBD data refreshed: %s",
                ", ".join(f"{len(v)} {k}" for k, v in data.items()),
            )
            return self._data

    async def _data_or_fail(
        self, ctx: commands.Context
    ) -> Optional[Dict[str, Dict[str, Dict[str, Any]]]]:
        await ctx.typing()
        try:
            return await self._load()
        except DbdError as error:
            await ctx.send(
                embed=discord.Embed(
                    title="Dead by Daylight data unavailable",
                    description=(
                        f"{error}.\n\nThe cog keeps a local copy for outages; if this "
                        "persists, try again later or run "
                        f"`{ctx.clean_prefix}dbd refresh` as bot owner."
                    ),
                    colour=await ctx.embed_color(),
                )
            )
            return None

    def _index(self, name: str) -> Dict[str, str]:
        index = self._indexes.get(name)
        if index is None:
            index = build_index(self._data.get(name) or {})
            self._indexes[name] = index
        return index

    def _token_index(self, name: str) -> Dict[str, List[str]]:
        tokens = self._token_indexes.get(name)
        if tokens is None:
            tokens = build_token_index(self._data.get(name) or {})
            self._token_indexes[name] = tokens
        return tokens

    def _clear_indexes(self) -> None:
        self._indexes.clear()
        self._token_indexes.clear()

    # ------------------------------------------------------------------ icons

    async def _ensure_icons(self) -> None:
        if self._icons and time.time() - self._icons_stamp < ICON_TTL:
            return
        async with self._icon_lock:
            if self._icons and time.time() - self._icons_stamp < ICON_TTL:
                return
            cached = self._read_cache(self._icon_file, ICON_TTL * 4)
            if (
                cached
                and cached.get("version") == ICON_CACHE_VERSION
                and isinstance(cached.get("data"), dict)
            ):
                self._icons = cached["data"]
                extra = cached.get("extra")
                self._icons_extra = extra if isinstance(extra, dict) else {}
                self._icons_stamp = float(cached.get("stamp", time.time()))
                return
            found: Dict[str, Dict[str, str]] = {}
            try:
                tree = await self._get_json(ICON_TREE_API)
            except Exception:  # noqa: BLE001 - icons are optional
                log.debug("could not read the icon tree", exc_info=True)
                tree = {}
            for entry in (tree.get("tree") or []) if isinstance(tree, dict) else []:
                if not isinstance(entry, dict) or entry.get("type") != "blob":
                    continue
                path = str(entry.get("path") or "")
                if not path.lower().endswith(".png"):
                    continue
                for prefix, icon_kind in ICON_FOLDERS.items():
                    if path.startswith(prefix):
                        stem = path.rsplit("/", 1)[-1][:-4]
                        found.setdefault(icon_kind, {})[
                            norm(stem)
                        ] = f"{ICON_RAW_BASE}/{path}"
                        break
            extra = await self._merge_icon_fallbacks(found)
            if not found and not extra:
                return
            self._icons = found
            self._icons_extra = extra
            self._icons_stamp = time.time()
            self._write_cache(
                self._icon_file,
                {
                    "stamp": self._icons_stamp,
                    "version": ICON_CACHE_VERSION,
                    "data": found,
                    "extra": extra,
                },
            )

    async def _merge_icon_fallbacks(
        self, found: Dict[str, Dict[str, str]]
    ) -> Dict[str, Dict[str, str]]:
        """Read the second mirror for perks.

        Its files are only consulted when the main tree has nothing for a name, so the
        primary source stays authoritative.
        """
        extra: Dict[str, Dict[str, str]] = {}
        if not ICON_FALLBACKS:
            return extra
        try:
            listings = await asyncio.gather(
                *(
                    self._get_json(f"https://api.github.com/repos/{repo}/contents/")
                    for repo in ICON_FALLBACKS.values()
                )
            )
        except Exception:  # noqa: BLE001 - icons are optional
            log.debug("could not read the fallback icon listings", exc_info=True)
            return extra
        for index, kind in enumerate(ICON_FALLBACKS.keys()):
            listing = listings[index]
            if not isinstance(listing, list):
                continue
            pool = extra.setdefault(kind, {})
            repo = ICON_FALLBACKS[kind]
            for entry in listing:
                if not isinstance(entry, dict):
                    continue
                filename = str(entry.get("name") or "")
                if not filename.lower().endswith(".png"):
                    continue
                pool.setdefault(
                    norm(filename.rsplit(".", 1)[0]),
                    f"https://raw.githubusercontent.com/{repo}/main/{quote(filename)}",
                )
        return extra

    def _icon_url(self, kind: str, name: str) -> Optional[str]:
        url = resolve_icon(self._icons.get(kind) or {}, name)
        if url:
            return url
        return resolve_icon(self._icons_extra.get(kind) or {}, name)

    # ------------------------------------------------------------------ embeds

    def _character_embed(self, char: Dict[str, Any]) -> discord.Embed:
        role = (char.get("role") or "").lower()
        role_emoji, role_label, colour = role_parts(role)
        diff_emoji, diff_label = difficulty_parts(char.get("difficulty"))
        embed = discord.Embed(
            title=f"{role_emoji} {char.get('name') or 'Unknown'}",
            description=trim(clean(char.get("bio")), 900) or "—",
            colour=colour,
        )
        embed.add_field(name="Role", value=role_label, inline=True)
        embed.add_field(
            name="Difficulty", value=f"{diff_emoji} {diff_label}", inline=True
        )
        if char.get("gender"):
            embed.add_field(
                name="Gender", value=str(char["gender"]).title(), inline=True
            )
        if char.get("height"):
            embed.add_field(
                name="Height", value=str(char["height"]).title(), inline=True
            )

        speed = killer_speed(char.get("tunables"))
        if speed:
            embed.add_field(name="Move Speed", value=speed, inline=True)
        radius = terror_radius(char.get("tunables"))
        if radius:
            embed.add_field(name="Terror Radius", value=radius, inline=True)

        perks = self._perk_names(char.get("perks") or [])
        if perks:
            embed.add_field(
                name="Personal Perks",
                value="\n".join(f"• {name}" for name in perks),
                inline=False,
            )

        portrait = self._icon_url(f"portrait_{role}", str(char.get("name") or ""))
        if portrait:
            embed.set_thumbnail(url=portrait)
        embed.set_footer(text="Dead by Daylight · data from dbd.tricky.lol")
        return embed

    def _perk_names(self, keys: Iterable[str]) -> List[str]:
        perks = self._data.get("perks") or {}
        names = []
        for key in keys:
            entry = perks.get(key)
            if entry:
                names.append(str(entry.get("name") or key))
        return names

    def _perk_embed(self, perk: Dict[str, Any]) -> discord.Embed:
        role = (perk.get("role") or "").lower()
        role_emoji, role_label, colour = role_parts(role)
        embed = discord.Embed(
            title=f"{role_emoji} {perk.get('name') or 'Unknown'}",
            description=trim(perk_text(perk), 3400) or "—",
            colour=colour,
        )
        character = self._character_of_perk(perk)
        embed.add_field(name="Role", value=role_label, inline=True)
        embed.add_field(
            name="Character", value=character or "General perk", inline=True
        )
        teachable = perk.get("teachable")
        embed.add_field(
            name="Teachable at",
            value=f"Level {teachable}" if teachable else "—",
            inline=True,
        )
        categories = [title_case_tag(tag) for tag in (perk.get("categories") or [])]
        if categories:
            embed.add_field(
                name="Categories", value=humanize_list(categories), inline=False
            )
        icon_kind = "perk_killer" if role == "killer" else "perk_survivor"
        icon = self._icon_url(icon_kind, str(perk.get("name") or ""))
        if icon:
            embed.set_thumbnail(url=icon)
        embed.set_footer(text="Dead by Daylight · values shown as tier I/II/III")
        return embed

    def _character_of_perk(self, perk: Dict[str, Any]) -> Optional[str]:
        index = perk.get("character")
        if index is None:
            return None
        characters = self._data.get("characters") or {}
        entry = characters.get(str(index))
        if isinstance(entry, dict):
            return entry.get("name")
        return None

    def _item_embed(self, item: Dict[str, Any]) -> discord.Embed:
        rarity_emoji, rarity_label, colour = rarity_parts(item.get("rarity"))
        embed = discord.Embed(
            title=f"🎒 {item.get('name') or 'Unknown'}",
            description=trim(
                clean(resolve_placeholders(item.get("description") or "", None)), 3400
            )
            or "—",
            colour=colour,
        )
        kind = ITEM_TYPES.get(
            str(item.get("item_type") or "").lower(),
            str(item.get("item_type") or "—").title(),
        )
        embed.add_field(name="Type", value=kind, inline=True)
        embed.add_field(
            name="Rarity", value=f"{rarity_emoji} {rarity_label}", inline=True
        )
        role = item.get("role")
        if role:
            embed.add_field(name="Role", value=role_parts(role)[1], inline=True)
        icon = self._icon_url("item", str(item.get("name") or ""))
        if icon:
            embed.set_thumbnail(url=icon)
        embed.set_footer(text="Dead by Daylight · data from dbd.tricky.lol")
        return embed

    def _offering_embed(self, offering: Dict[str, Any]) -> discord.Embed:
        rarity_emoji, rarity_label, colour = rarity_parts(offering.get("rarity"))
        embed = discord.Embed(
            title=f"🕯️ {offering.get('name') or 'Unknown'}",
            description=trim(clean(offering.get("description") or ""), 3400) or "—",
            colour=colour,
        )
        embed.add_field(
            name="Rarity", value=f"{rarity_emoji} {rarity_label}", inline=True
        )
        embed.add_field(
            name="Type",
            value=title_case_tag(str(offering.get("type") or "—")),
            inline=True,
        )
        role = offering.get("role")
        embed.add_field(
            name="Role", value=role_parts(role)[1] if role else "Any", inline=True
        )
        tags = [title_case_tag(tag) for tag in (offering.get("tags") or [])]
        if tags:
            embed.add_field(name="Tags", value=humanize_list(tags), inline=False)
        if offering.get("retired"):
            embed.add_field(name="Retired", value="Yes", inline=True)
        icon = self._icon_url("offering", str(offering.get("name") or ""))
        if icon:
            embed.set_thumbnail(url=icon)
        embed.set_footer(text="Dead by Daylight · data from dbd.tricky.lol")
        return embed

    def _map_embed(self, entry: Dict[str, Any]) -> discord.Embed:
        embed = discord.Embed(
            title=f"🗺️ {entry.get('name') or 'Unknown'}",
            description=trim(clean(entry.get("description") or ""), 3400) or "—",
            colour=0x6D4C41,
        )
        if entry.get("realm"):
            embed.add_field(name="Realm", value=str(entry["realm"]), inline=True)
        if entry.get("alias"):
            embed.add_field(
                name="Alias",
                value=str(entry["alias"]).replace("_", " ").title(),
                inline=True,
            )
        icon = self._icon_url("map", str(entry.get("name") or ""))
        if icon:
            embed.set_thumbnail(url=icon)
        embed.set_footer(text="Dead by Daylight · data from dbd.tricky.lol")
        return embed

    def _error_embed(self, colour: int, title: str, description: str) -> discord.Embed:
        return discord.Embed(title=title, description=description, colour=colour)

    # ------------------------------------------------------------------ lookup helpers

    def _find(
        self,
        kind: str,
        query: str,
    ) -> Tuple[Optional[Dict[str, Any]], List[str], List[str]]:
        """Resolve a query in one payload table.

        Returns ``(entry, candidates, suggestions)``. ``entry`` is ``None`` when the query is
        ambiguous or unknown; ``candidates`` are real matches for that query, ``suggestions``
        are merely similar names offered as a hint.
        """
        table = self._data.get(kind) or {}
        index = self._index(kind)
        key, candidates = lookup(index, query, self._token_index(kind))
        if key is not None:
            entry = table.get(key)
            if isinstance(entry, dict):
                return entry, [], []
        names = [
            str(table[c].get("name") or c)
            for c in candidates
            if isinstance(table.get(c), dict)
        ]
        if names:
            return None, names, []
        return None, [], suggest(table, query)

    # ------------------------------------------------------------------ commands

    @commands.group(aliases=["deadbydaylight"], invoke_without_command=True)
    async def dbd(self, ctx: commands.Context) -> None:
        """Dead by Daylight information.

        Look up killers, survivors, perks, items, offerings and maps, browse the roster,
        or search everything at once.
        """
        prefix = ctx.clean_prefix
        embed = discord.Embed(
            title="🔪 Dead by Daylight",
            description=(
                f"`{prefix}dbd killer <name>` — a killer, with speed and terror radius\n"
                f"`{prefix}dbd survivor <name>` — a survivor\n"
                f"`{prefix}dbd char <name>` — either of the two\n"
                f"`{prefix}dbd perk <name>` — a perk, with all three tiers\n"
                f"`{prefix}dbd item <name>` — an item\n"
                f"`{prefix}dbd offering <name>` — an offering\n"
                f"`{prefix}dbd map <name>` — a map\n"
                f"`{prefix}dbd lore <name>` — the full backstory of a character\n"
                f"`{prefix}dbd random [perks|killers|survivors]` — roll the dice\n"
                f"`{prefix}dbd search <text>` — search everything\n"
                f"`{prefix}dbd list <killers|survivors|perks|items|maps>` — browse\n"
                f"`{prefix}dbd stats` — dataset size and cache age"
            ),
            colour=0x9B1B30,
        )
        embed.set_footer(
            text="Names are matched loosely: 'hex ruin', 'nurse', 'sprint'"
        )
        await ctx.send(embed=embed)

    @dbd.command(name="killer")
    async def dbd_killer(self, ctx: commands.Context, *, name: str) -> None:
        """Show a killer: difficulty, speed, terror radius and personal perks."""
        await self._show_character(ctx, name, expected="killer")

    @dbd.command(name="survivor")
    async def dbd_survivor(self, ctx: commands.Context, *, name: str) -> None:
        """Show a survivor and their personal perks."""
        await self._show_character(ctx, name, expected="survivor")

    @dbd.command(name="character", aliases=["char"])
    async def dbd_character(self, ctx: commands.Context, *, name: str) -> None:
        """Show a killer or survivor by name."""
        await self._show_character(ctx, name)

    async def _show_character(
        self, ctx: commands.Context, name: str, expected: Optional[str] = None
    ) -> None:
        await self._ensure_icons()
        if await self._data_or_fail(ctx) is None:
            return
        entry, candidates, suggestions = self._find("characters", name)
        if entry is None:
            await self._send_miss(ctx, "character", name, candidates, suggestions)
            return
        role = (entry.get("role") or "").lower()
        if expected and role != expected:
            role_emoji, role_label, colour = role_parts(role)
            await ctx.send(
                embed=self._error_embed(
                    colour,
                    f"{entry.get('name')} is a {role_label}",
                    f"{role_emoji} Try `{ctx.clean_prefix}dbd {role} {entry.get('name')}` instead.",
                )
            )
            return
        await ctx.send(embed=self._character_embed(entry))

    @dbd.command(name="perk")
    async def dbd_perk(self, ctx: commands.Context, *, name: str) -> None:
        """Show a perk with its description and all three tier values."""
        await self._ensure_icons()
        if await self._data_or_fail(ctx) is None:
            return
        entry, candidates, suggestions = self._find("perks", name)
        if entry is None:
            await self._send_miss(ctx, "perk", name, candidates, suggestions)
            return
        await ctx.send(embed=self._perk_embed(entry))

    @dbd.command(name="item")
    async def dbd_item(self, ctx: commands.Context, *, name: str) -> None:
        """Show an item: description, type and rarity."""
        await self._ensure_icons()
        if await self._data_or_fail(ctx) is None:
            return
        entry, candidates, suggestions = self._find("items", name)
        if entry is None:
            await self._send_miss(ctx, "item", name, candidates, suggestions)
            return
        await ctx.send(embed=self._item_embed(entry))

    @dbd.command(name="offering")
    async def dbd_offering(self, ctx: commands.Context, *, name: str) -> None:
        """Show an offering: description, rarity and tags."""
        await self._ensure_icons()
        if await self._data_or_fail(ctx) is None:
            return
        entry, candidates, suggestions = self._find("offerings", name)
        if entry is None:
            await self._send_miss(ctx, "offering", name, candidates, suggestions)
            return
        await ctx.send(embed=self._offering_embed(entry))

    @dbd.command(name="map")
    async def dbd_map(self, ctx: commands.Context, *, name: str) -> None:
        """Show a map and the realm it belongs to."""
        await self._ensure_icons()
        if await self._data_or_fail(ctx) is None:
            return
        entry, candidates, suggestions = self._find("maps", name)
        if entry is None:
            await self._send_miss(ctx, "map", name, candidates, suggestions)
            return
        await ctx.send(embed=self._map_embed(entry))

    @dbd.command(name="lore")
    async def dbd_lore(self, ctx: commands.Context, *, name: str) -> None:
        """Show the full backstory of a killer or survivor."""
        await self._ensure_icons()
        if await self._data_or_fail(ctx) is None:
            return
        entry, candidates, suggestions = self._find("characters", name)
        if entry is None:
            await self._send_miss(ctx, "character", name, candidates, suggestions)
            return
        story = clean(entry.get("story"))
        if not story:
            await ctx.send(
                embed=self._error_embed(
                    await ctx.embed_color(),
                    f"No backstory for {entry.get('name')}",
                    "This character has no lore text in the dataset.",
                )
            )
            return
        role_emoji, _, colour = role_parts(entry.get("role"))
        pages: List[discord.Embed] = []
        chunks = list(pagify(story, page_length=1800))
        for number, page in enumerate(chunks, start=1):
            embed = discord.Embed(
                title=f"{role_emoji} {entry.get('name')} — backstory",
                description=page,
                colour=colour,
            )
            embed.set_footer(text=f"Page {number}/{len(chunks)} · Dead by Daylight")
            pages.append(embed)
        if len(pages) == 1:
            await ctx.send(embed=pages[0])
        else:
            await menu(ctx, pages, DEFAULT_CONTROLS)

    @dbd.command(name="random")
    async def dbd_random(self, ctx: commands.Context, scope: str = "perks") -> None:
        """Pick a random perk, killer or survivor."""
        import random

        await self._ensure_icons()
        if await self._data_or_fail(ctx) is None:
            return
        wanted = scope.lower().rstrip("s")
        if wanted in ("killer", "survivor"):
            table = [
                entry
                for entry in (self._data.get("characters") or {}).values()
                if (entry.get("role") or "").lower() == wanted
            ]
            if not table:
                await self._send_miss(ctx, "character", scope, [])
                return
            await ctx.send(embed=self._character_embed(random.choice(table)))
            return
        if wanted != "perk":
            await ctx.send(
                embed=self._error_embed(
                    await ctx.embed_color(),
                    "Unknown scope",
                    f"Use `{ctx.clean_prefix}dbd random perks`, `killers` or `survivors`.",
                )
            )
            return
        table = list((self._data.get("perks") or {}).values())
        if not table:
            await self._send_miss(ctx, "perk", scope, [])
            return
        await ctx.send(embed=self._perk_embed(random.choice(table)))

    @dbd.command(name="search")
    async def dbd_search(self, ctx: commands.Context, *, query: str) -> None:
        """Search killers, survivors, perks, items, offerings and maps at once."""
        await self._ensure_icons()
        data = await self._data_or_fail(ctx)
        if data is None:
            return
        needle = norm(query)
        if not needle:
            await ctx.send(
                embed=self._error_embed(
                    await ctx.embed_color(),
                    "Nothing to search for",
                    f"`{ctx.clean_prefix}dbd search <text>`",
                )
            )
            return
        groups: List[Tuple[str, List[str]]] = []
        labels = [
            ("characters", "Characters"),
            ("perks", "Perks"),
            ("items", "Items"),
            ("offerings", "Offerings"),
            ("maps", "Maps"),
        ]
        for table_name, label in labels:
            hits = sorted(
                str(entry.get("name"))
                for entry in (data.get(table_name) or {}).values()
                if needle in norm(str(entry.get("name") or ""))
            )
            if hits:
                groups.append((label, hits))
        if not groups:
            await ctx.send(
                embed=self._error_embed(
                    await ctx.embed_color(),
                    "No matches",
                    f"Nothing in the Dead by Daylight dataset matches **{query}**.",
                )
            )
            return

        lines: List[str] = []
        total = 0
        for label, hits in groups:
            total += len(hits)
            lines.append(f"__{label}__ ({len(hits)})")
            lines.extend(f"• {name}" for name in hits[:10])
            if len(hits) > 10:
                lines.append(f"… and {len(hits) - 10} more")
            lines.append("")
        pages = [
            discord.Embed(
                title=f"🔎 Search: {query}",
                description=trim("\n".join(page), 4000),
                colour=0x9B1B30,
            )
            for page in _paginate(lines, 20)
        ]
        for number, embed in enumerate(pages, start=1):
            embed.set_footer(text=f"{total} matches · page {number}/{len(pages)}")
        if len(pages) == 1:
            await ctx.send(embed=pages[0])
        else:
            await menu(ctx, pages, DEFAULT_CONTROLS)

    @dbd.command(name="list")
    async def dbd_list(
        self, ctx: commands.Context, kind: str, role: Optional[str] = None
    ) -> None:
        """Browse a table: killers, survivors, perks, items, offerings or maps."""
        await self._ensure_icons()
        data = await self._data_or_fail(ctx)
        if data is None:
            return
        table_name, forced_role = KINDS.get(kind.lower(), (None, None))
        if table_name is None:
            await ctx.send(
                embed=self._error_embed(
                    await ctx.embed_color(),
                    "Unknown list",
                    "Pick one of: "
                    + humanize_list(
                        ["killers", "survivors", "perks", "items", "offerings", "maps"]
                    ),
                )
            )
            return
        wanted_role = (role or forced_role or "").lower().rstrip("s")
        if wanted_role not in ("", "killer", "survivor"):
            await ctx.send(
                embed=self._error_embed(
                    await ctx.embed_color(),
                    "Unknown role",
                    "The optional role is `killer` or `survivor`.",
                )
            )
            return

        entries = [
            entry
            for entry in (data.get(table_name) or {}).values()
            if isinstance(entry, dict)
            and (not wanted_role or (entry.get("role") or "").lower() == wanted_role)
        ]
        if not entries:
            await ctx.send(
                embed=self._error_embed(
                    await ctx.embed_color(),
                    "Empty list",
                    "The dataset has no entries for that filter.",
                )
            )
            return
        entries.sort(key=lambda entry: str(entry.get("name") or "").lower())

        title = f"📚 {table_name.title()}"
        if wanted_role:
            title += f" · {wanted_role.title()}s"
        pages: List[discord.Embed] = []
        for page in chunk(entries, 15):
            embed = discord.Embed(title=title, colour=0x9B1B30)
            embed.description = "\n".join(
                self._list_line(table_name, entry) for entry in page
            )
            embed.set_footer(text=f"{len(entries)} entries · page {len(pages) + 1}")
            pages.append(embed)
        for number, embed in enumerate(pages, start=1):
            embed.set_footer(
                text=f"{len(entries)} entries · page {number}/{len(pages)}"
            )
        if len(pages) == 1:
            await ctx.send(embed=pages[0])
        else:
            await menu(ctx, pages, DEFAULT_CONTROLS)

    def _list_line(self, table_name: str, entry: Dict[str, Any]) -> str:
        name = entry.get("name") or "?"
        if table_name == "characters":
            diff_emoji, _ = difficulty_parts(entry.get("difficulty"))
            return f"{diff_emoji} **{name}**"
        if table_name == "perks":
            role_emoji, _, _ = role_parts(entry.get("role"))
            owner = self._character_of_perk(entry) or "General"
            return f"{role_emoji} **{name}** — {owner}"
        if table_name == "items":
            rarity_emoji, rarity_label, _ = rarity_parts(entry.get("rarity"))
            kind = ITEM_TYPES.get(str(entry.get("item_type") or "").lower(), "—")
            return f"{rarity_emoji} **{name}** — {kind}, {rarity_label}"
        if table_name == "offerings":
            rarity_emoji, rarity_label, _ = rarity_parts(entry.get("rarity"))
            return f"{rarity_emoji} **{name}** — {rarity_label}"
        realm = entry.get("realm")
        return f"🗺️ **{name}**" + (f" — {realm}" if realm else "")

    @dbd.command(name="stats")
    async def dbd_stats(self, ctx: commands.Context) -> None:
        """How much data the cog holds and how old it is."""
        data = await self._data_or_fail(ctx)
        if data is None:
            return
        characters = list((data.get("characters") or {}).values())
        killers = sum(
            1 for entry in characters if (entry.get("role") or "").lower() == "killer"
        )
        survivors = sum(
            1 for entry in characters if (entry.get("role") or "").lower() == "survivor"
        )
        age = int(time.time() - self._stamp)
        embed = discord.Embed(
            title="📊 Dead by Daylight dataset",
            colour=await ctx.embed_color(),
        )
        embed.add_field(name="Killers", value=str(killers), inline=True)
        embed.add_field(name="Survivors", value=str(survivors), inline=True)
        embed.add_field(
            name="Perks", value=str(len(data.get("perks") or {})), inline=True
        )
        embed.add_field(
            name="Items", value=str(len(data.get("items") or {})), inline=True
        )
        embed.add_field(
            name="Offerings", value=str(len(data.get("offerings") or {})), inline=True
        )
        embed.add_field(
            name="Maps", value=str(len(data.get("maps") or {})), inline=True
        )
        embed.add_field(
            name="Cache age",
            value=f"{age // 60} min" if age >= 60 else f"{age} s",
            inline=True,
        )
        embed.add_field(
            name="Refreshes after", value=f"{CACHE_TTL // 3600} h", inline=True
        )
        icon_sets = (
            ", ".join(f"{kind}: {len(files)}" for kind, files in self._icons.items())
            or "none"
        )
        embed.add_field(name="Icon files", value=icon_sets, inline=False)
        embed.set_footer(text="Source: dbd.tricky.lol · icons: github.com/profsoru")
        await ctx.send(embed=embed)

    @dbd.command(name="refresh")
    @commands.is_owner()
    async def dbd_refresh(self, ctx: commands.Context) -> None:
        """Force a full reload from the data source (bot owner only)."""
        await ctx.typing()
        started = time.monotonic()
        try:
            data = await self._load(force=True)
        except DbdError as error:
            await ctx.send(
                embed=self._error_embed(0xC0392B, "Refresh failed", f"{error}.")
            )
            return
        await self._ensure_icons()
        took = time.monotonic() - started
        await ctx.send(
            embed=self._error_embed(
                0x27AE60,
                "Dataset reloaded",
                "\n".join(f"• {len(table)} {name}" for name, table in data.items())
                + f"\n\nTook {took:.1f} s.",
            )
        )

    # ------------------------------------------------------------------ misses

    async def _send_miss(
        self,
        ctx: commands.Context,
        kind: str,
        query: str,
        candidates: Sequence[str],
        suggestions: Sequence[str] = (),
    ) -> None:
        colour = await ctx.embed_color()
        if candidates:
            shown = list(candidates)[:8]
            description = "Did you mean:\n" + "\n".join(f"• {name}" for name in shown)
            if len(candidates) > len(shown):
                description += f"\n… and {len(candidates) - len(shown)} more"
            await ctx.send(
                embed=self._error_embed(
                    colour, f"Several {kind}s match “{query}”", description
                )
            )
            return
        description = (
            f"Try `{ctx.clean_prefix}dbd list {kind}s` or a shorter search term."
        )
        if suggestions:
            description = "Closest names:\n" + "\n".join(
                f"• {name}" for name in list(suggestions)[:5]
            )
        await ctx.send(
            embed=self._error_embed(
                colour, f"No {kind} found for “{query}”", description
            )
        )


def _paginate(lines: List[str], per_page: int) -> List[List[str]]:
    return [lines[i : i + per_page] for i in range(0, len(lines), per_page)] or [[]]


async def setup(bot: Any) -> None:
    await bot.add_cog(DBD(bot))
