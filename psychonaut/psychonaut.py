"""PsychonautWiki — substances, dosages, durations, effects and interactions.

Two public interfaces of PsychonautWiki are used together:

* ``https://api.psychonautwiki.org`` — a GraphQL API that returns the structured
  SubstanceBox data: chemical and psychoactive class, tolerance, addiction
  potential, toxicity, cross-tolerances and one entry per route of administration
  with dose ranges, bioavailability and the full duration breakdown, plus
  dangerous / unsafe / uncertain interactions.
* ``https://psychonautwiki.org/w/api.php`` — the MediaWiki API for the parts the
  GraphQL API does not expose usefully: the article intro paragraph, the
  structure image and the subjective effect lists.

Payloads are cached in memory and on disk under the cog's data directory, so a
restart or a short outage of either endpoint degrades to older data instead of an
empty command.  Nothing here is medical advice: every dosing embed carries the
source and the wiki's own warning.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import random
import time
from typing import Any, Dict, Iterable, List, Optional, Tuple

import aiohttp
import discord
from redbot.core import checks, commands
from redbot.core.data_manager import cog_data_path
from redbot.core.utils.chat_formatting import humanize_list
from redbot.core.utils.menus import DEFAULT_CONTROLS, menu

from .wiki import (
    EFFECT_LABELS,
    SITE,
    box_template_name,
    build_index,
    build_token_index,
    class_matches,
    clean,
    dose_rows,
    duration_rows,
    humanize_class,
    intro,
    match,
    norm,
    paginate,
    parse_box,
    parse_effects,
    suggest,
    thumb_url,
    trim,
)

__red_end_user_data_statement__ = "This cog does not store any End User Data."

log = logging.getLogger("red.freak_cogs.psychonaut")

API_GQL = "https://api.psychonautwiki.org/"
API_WIKI = "https://psychonautwiki.org/w/api.php"
REPO_URL = "https://github.com/cens0r3d/freak-cogs"
REQUEST_TIMEOUT = 25
INDEX_TTL = 7 * 86400
DETAIL_TTL = 30 * 86400
CACHE_VERSION = 3

INDEX_QUERY = """
{
  substances(limit: 500) {
    name
    url
    commonNames
    class { chemical psychoactive }
  }
}
"""

DETAIL_QUERY = """
query ($name: String) {
  substances(query: $name, limit: 5) {
    name
    url
    commonNames
    systematicName
    class { chemical psychoactive }
    tolerance { full half zero }
    addictionPotential
    toxicity
    crossTolerances
    roas {
      name
      bioavailability { min max }
      dose {
        units
        threshold
        heavy
        light { min max }
        common { min max }
        strong { min max }
      }
      duration {
        total { min max units }
        onset { min max units }
        comeup { min max units }
        peak { min max units }
        offset { min max units }
        afterglow { min max units }
      }
    }
    dangerousInteractions { name }
    unsafeInteractions { name }
    uncertainInteractions { name }
  }
}
"""

SEARCH_QUERY = """
query ($text: String) {
  substances(query: $text, limit: 12) {
    name
    class { chemical psychoactive }
  }
}
"""


class PsychonautError(Exception):
    """Raised when both data sources are unreachable and no cache exists."""


# --------------------------------------------------------------------------- #
# small helpers
# --------------------------------------------------------------------------- #


def as_list(value: Any) -> List[str]:
    """GraphQL answers a repeated field as a list, a single value as a string."""
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        return [str(item) for item in value if item not in (None, "")]
    text = str(value).strip()
    return [text] if text else []


def names_from(value: Any) -> List[str]:
    """``[{name: "Cannabis"}, …]`` -> ``["Cannabis", …]``."""
    output: List[str] = []
    for item in value or []:
        if isinstance(item, dict):
            name = str(item.get("name") or "").strip()
        else:
            name = str(item or "").strip()
        if name and name not in output:
            output.append(name)
    return output


def sane_summary(text: Any) -> str:
    """The GraphQL ``summary`` field returns template noise for some pages."""
    value = clean(str(text or ""))
    if not value or len(value) < 40:
        return ""
    lowered = value.lower()
    if lowered.startswith(("summary sheet", "summary sheet:")):
        return ""
    return value


class Psychonaut(commands.Cog):
    """PsychonautWiki: dosages, durations, effects and interactions."""

    def __init__(self, bot: Any) -> None:
        self.bot = bot
        self.session: Optional[aiohttp.ClientSession] = None
        self._cache: Dict[str, Any] = {
            "version": CACHE_VERSION,
            "index": {},
            "detail": {},
        }
        self._index_rows: List[Dict[str, Any]] = []
        self._index_stamp: float = 0.0
        self._index_map: Dict[str, str] = {}
        self._token_map: Dict[str, List[str]] = {}
        self._lock = asyncio.Lock()
        self._loaded = False

    # ------------------------------------------------------------------ lifecycle

    async def cog_load(self) -> None:
        self.session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=REQUEST_TIMEOUT),
            headers={
                "User-Agent": ("Red-DiscordBot psychonaut cog (+" + REPO_URL + ")")
            },
        )
        self._load_cache()

    def cog_unload(self) -> None:
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

    # ------------------------------------------------------------------ cache

    @property
    def _cache_file(self):
        return cog_data_path(self) / "psychonaut_cache.json"

    def _load_cache(self) -> None:
        """Read the disk cache once at load; failures are never fatal."""
        path = self._cache_file
        try:
            raw = path.read_text(encoding="utf-8")
        except OSError:
            return
        try:
            payload = json.loads(raw)
        except ValueError:
            log.debug("cache file %s is not valid JSON, ignoring it", path)
            return
        if not isinstance(payload, dict) or payload.get("version") != CACHE_VERSION:
            return
        index = payload.get("index")
        if isinstance(index, dict) and isinstance(index.get("rows"), list):
            self._index_rows = [row for row in index["rows"] if isinstance(row, dict)]
            self._index_stamp = float(index.get("stamp") or 0)
            self._rebuild_maps()
        detail = payload.get("detail")
        if isinstance(detail, dict):
            self._cache["detail"] = {
                name: entry
                for name, entry in detail.items()
                if isinstance(entry, dict) and isinstance(entry.get("data"), dict)
            }
        self._loaded = True

    def _save_cache(self) -> None:
        payload = {
            "version": CACHE_VERSION,
            "index": {"stamp": self._index_stamp, "rows": self._index_rows},
            "detail": self._cache["detail"],
        }
        try:
            self._cache_file.parent.mkdir(parents=True, exist_ok=True)
            self._cache_file.write_text(json.dumps(payload), encoding="utf-8")
        except OSError:
            log.debug("could not write the cache file", exc_info=True)

    def _rebuild_maps(self) -> None:
        self._index_map = build_index(self._index_rows)
        self._token_map = build_token_index(self._index_map)

    # ------------------------------------------------------------------ transport

    async def _session(self) -> aiohttp.ClientSession:
        if self.session is None or self.session.closed:
            self.session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=REQUEST_TIMEOUT),
                headers={
                    "User-Agent": "Red-DiscordBot psychonaut cog (+" + REPO_URL + ")"
                },
            )
        return self.session

    async def _gql(
        self, query: str, variables: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        """Run one GraphQL query and return its ``data`` object."""
        session = await self._session()
        payload: Dict[str, Any] = {"query": query}
        if variables:
            payload["variables"] = variables
        async with session.post(API_GQL, json=payload) as response:
            if response.status != 200:
                raise PsychonautError(
                    f"api.psychonautwiki.org returned HTTP {response.status}"
                )
            body = await response.json(content_type=None)
        if not isinstance(body, dict):
            raise PsychonautError(
                "api.psychonautwiki.org returned an unreadable payload"
            )
        errors = body.get("errors")
        if errors:
            message = (
                errors[0].get("message")
                if isinstance(errors[0], dict)
                else str(errors[0])
            )
            raise PsychonautError(
                f"api.psychonautwiki.org rejected the query: {message}"
            )
        data = body.get("data")
        return data if isinstance(data, dict) else {}

    async def _wiki(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """Call the MediaWiki API and return the parsed JSON."""
        query = {"format": "json", "formatversion": 2}
        query.update(params)
        session = await self._session()
        async with session.get(API_WIKI, params=query) as response:
            if response.status != 200:
                raise PsychonautError(
                    f"psychonautwiki.org returned HTTP {response.status}"
                )
            body = await response.json(content_type=None)
        if not isinstance(body, dict):
            raise PsychonautError("psychonautwiki.org returned an unreadable payload")
        return body

    async def _wikitext(self, page: str) -> str:
        body = await self._wiki({"action": "parse", "page": page, "prop": "wikitext"})
        parse = body.get("parse")
        if not isinstance(parse, dict):
            return ""
        return str(parse.get("wikitext") or "")

    # ------------------------------------------------------------------ index

    async def _index(self, force: bool = False) -> List[Dict[str, Any]]:
        """All substances with their classes and aliases, cached for a week."""
        if (
            self._index_rows
            and not force
            and time.time() - self._index_stamp < INDEX_TTL
        ):
            return self._index_rows

        async with self._lock:
            if (
                self._index_rows
                and not force
                and time.time() - self._index_stamp < INDEX_TTL
            ):
                return self._index_rows
            try:
                data = await self._gql(INDEX_QUERY)
            except Exception as error:  # noqa: BLE001 - served as an embed
                if self._index_rows:
                    log.warning(
                        "index refresh failed (%s), keeping the cached list", error
                    )
                    return self._index_rows
                raise PsychonautError(
                    f"the PsychonautWiki index is unreachable ({error})"
                ) from error
            rows: List[Dict[str, Any]] = []
            for raw in data.get("substances") or []:
                if not isinstance(raw, dict):
                    continue
                name = str(raw.get("name") or "").strip()
                if not name:
                    continue
                classes = raw.get("class") or {}
                rows.append(
                    {
                        "name": name,
                        "url": str(raw.get("url") or ""),
                        "common": as_list(raw.get("commonNames")),
                        "chemical": as_list(classes.get("chemical")),
                        "psychoactive": as_list(classes.get("psychoactive")),
                    }
                )
            if not rows:
                raise PsychonautError("the PsychonautWiki index came back empty")
            self._index_rows = rows
            self._index_stamp = time.time()
            self._rebuild_maps()
            self._save_cache()
            log.info("psychonaut index refreshed: %s substances", len(rows))
            return rows

    async def _index_or_fail(
        self, ctx: commands.Context
    ) -> Optional[List[Dict[str, Any]]]:
        await ctx.typing()
        try:
            return await self._index()
        except PsychonautError as error:
            await ctx.send(
                embed=self._error_embed(
                    await ctx.embed_color(),
                    "PsychonautWiki is unreachable",
                    f"{error}.\n\nThe cog keeps a local copy for outages; if this "
                    f"keeps happening, try again later or run "
                    f"`{ctx.clean_prefix}psychonaut refresh` as bot owner.",
                )
            )
            return None

    # ------------------------------------------------------------------ detail

    async def _detail(self, name: str, force: bool = False) -> Dict[str, Any]:
        """Full record for one substance: GraphQL data plus wiki enrichment."""
        entry = self._cache["detail"].get(name)
        if (
            isinstance(entry, dict)
            and not force
            and time.time() - float(entry.get("stamp") or 0) < DETAIL_TTL
        ):
            return entry["data"]

        data = await self._gql(DETAIL_QUERY, {"name": name})
        rows = [row for row in (data.get("substances") or []) if isinstance(row, dict)]
        if not rows:
            raise PsychonautError(f"no PsychonautWiki entry for {name}")
        row = rows[0]
        for candidate in rows:
            if norm(str(candidate.get("name") or "")) == norm(name):
                row = candidate
                break

        article = ""
        with contextlib.suppress(Exception):
            article = await self._wikitext(str(row.get("name") or name))

        structure = ""
        caption = ""
        box_roas: List[str] = []
        box_name = box_template_name(article)
        if box_name:
            with contextlib.suppress(Exception):
                box = parse_box(await self._wikitext("Template:" + box_name))
                structure = str(box.get("structure") or "")
                caption = str(box.get("caption") or "")
                box_roas = list(box.get("roas") or ())

        summary = intro(article, 900) or sane_summary(row.get("summary"))
        effects = parse_effects(article) if article else {}

        detail = {
            "name": str(row.get("name") or name),
            "url": str(row.get("url") or f"{SITE}{name.replace(' ', '_')}"),
            "common": as_list(row.get("commonNames")),
            "classes": row.get("class") or {},
            "tolerance": row.get("tolerance") or {},
            "addiction": str(row.get("addictionPotential") or ""),
            "toxicity": as_list(row.get("toxicity")),
            "cross": as_list(row.get("crossTolerances")),
            "roas": [roa for roa in (row.get("roas") or []) if isinstance(roa, dict)],
            "dangerous": names_from(row.get("dangerousInteractions")),
            "unsafe": names_from(row.get("unsafeInteractions")),
            "uncertain": names_from(row.get("uncertainInteractions")),
            "structure": structure,
            "caption": caption,
            "intro": summary,
            "effects": effects,
            "box_roas": box_roas,
            "fetched": time.time(),
        }
        self._cache["detail"][name] = {"stamp": time.time(), "data": detail}
        self._save_cache()
        return detail

    async def _detail_or_fail(
        self, ctx: commands.Context, name: str
    ) -> Optional[Dict[str, Any]]:
        await ctx.typing()
        try:
            return await self._detail(name)
        except Exception as error:  # noqa: BLE001 - reported as an embed
            cached = self._cache["detail"].get(name)
            if isinstance(cached, dict) and isinstance(cached.get("data"), dict):
                log.warning("live fetch for %s failed (%s), serving cache", name, error)
                return cached["data"]
            await ctx.send(
                embed=self._error_embed(
                    await ctx.embed_color(),
                    f"No data for {name}",
                    f"PsychonautWiki could not be read ({error}). Try again in a moment.",
                )
            )
            return None

    # ------------------------------------------------------------------ resolution

    async def _search_remote(self, text: str) -> List[Dict[str, Any]]:
        data = await self._gql(SEARCH_QUERY, {"text": text})
        return [row for row in (data.get("substances") or []) if isinstance(row, dict)]

    async def _resolve(self, ctx: commands.Context, query: str) -> Optional[str]:
        """Turn user text into a canonical page title, or explain why not."""
        if await self._index_or_fail(ctx) is None:
            return None
        title, candidates = match(query, self._index_map, self._token_map)
        if title:
            return title
        if candidates:
            await self._send_candidates(ctx, query, candidates)
            return None

        try:
            found = await self._search_remote(query)
        except Exception:  # noqa: BLE001 - the local index already said no
            log.debug("remote search for %r failed", query, exc_info=True)
            found = []
        names = [str(row.get("name") or "") for row in found if row.get("name")]
        if len(names) == 1:
            return names[0]
        if names:
            await self._send_candidates(ctx, query, names[:15])
            return None

        suggestions = suggest(query, self._index_map)
        text = f"Nothing on PsychonautWiki matches **{trim(query, 80)}**."
        if suggestions:
            text += (
                "\n\nDid you mean: "
                + humanize_list([f"`{name}`" for name in suggestions])
                + "?"
            )
        await ctx.send(
            embed=self._error_embed(await ctx.embed_color(), "Not found", text)
        )
        return None

    async def _send_candidates(
        self, ctx: commands.Context, query: str, candidates: Iterable[str]
    ) -> None:
        names: List[str] = []
        for name in candidates:
            if name and name not in names:
                names.append(name)
        lines = [f"• `{name}`" for name in names[:20]]
        if len(names) > 20:
            lines.append(f"… and {len(names) - 20} more")
        await ctx.send(
            embed=discord.Embed(
                title=f"Several substances match “{trim(query, 60)}”",
                description="\n".join(lines),
                colour=await ctx.embed_color(),
            ).set_footer(text=f"Pick one: {ctx.clean_prefix}psychonaut <name>")
        )

    # ------------------------------------------------------------------ embeds

    @staticmethod
    def _error_embed(colour: Any, title: str, description: str) -> discord.Embed:
        return discord.Embed(title=title, description=description, colour=colour)

    @staticmethod
    def _class_text(classes: Dict[str, Any]) -> str:
        psychoactive = [
            humanize_class(name) for name in as_list(classes.get("psychoactive"))
        ]
        chemical = [humanize_class(name) for name in as_list(classes.get("chemical"))]
        parts: List[str] = []
        if psychoactive:
            parts.append("**" + " / ".join(psychoactive) + "**")
        if chemical:
            parts.append(" / ".join(chemical))
        return " · ".join(parts) if parts else "—"

    def _tolerance_text(self, tolerance: Dict[str, Any]) -> str:
        parts: List[str] = []
        for key, label in (
            ("full", "Full tolerance"),
            ("half", "Half tolerance"),
            ("zero", "Back to baseline"),
        ):
            value = clean(str(tolerance.get(key) or ""))
            if value:
                parts.append(f"**{label}:** {value}")
        return "\n".join(parts) if parts else "—"

    def _lookup_embed(
        self, ctx: commands.Context, detail: Dict[str, Any]
    ) -> discord.Embed:
        name = str(detail.get("name") or "?")
        embed = discord.Embed(
            title=name,
            url=str(detail.get("url") or ""),
            colour=0x00A8A8,
        )
        text = trim(str(detail.get("intro") or ""), 900)
        embed.description = text or None
        embed.add_field(
            name="Class",
            value=trim(self._class_text(detail.get("classes") or {}), 1000),
            inline=False,
        )
        common = as_list(detail.get("common"))
        if common:
            embed.add_field(
                name="Also known as",
                value=trim(", ".join(common[:12]), 1000),
                inline=False,
            )
        routes = [
            str(roa.get("name") or "").strip().title()
            for roa in detail.get("roas") or []
            if str(roa.get("name") or "").strip()
        ]
        if routes:
            embed.add_field(name="Routes", value=", ".join(routes), inline=False)
        tolerance = self._tolerance_text(detail.get("tolerance") or {})
        if tolerance != "—":
            embed.add_field(name="Tolerance", value=trim(tolerance, 1000), inline=False)
        toxicity = as_list(detail.get("toxicity"))
        if toxicity:
            embed.add_field(
                name="Toxicity", value=trim("; ".join(toxicity[:3]), 1000), inline=False
            )
        addiction = clean(str(detail.get("addiction") or ""))
        if addiction:
            embed.add_field(
                name="Addiction potential", value=trim(addiction, 1000), inline=False
            )
        cross = as_list(detail.get("cross"))
        if cross:
            embed.add_field(
                name="Cross-tolerance", value=", ".join(cross[:8]), inline=False
            )

        structure = str(detail.get("structure") or "")
        if structure:
            embed.set_image(url=thumb_url(structure, 330))
        embed.set_footer(
            text="PsychonautWiki · api.psychonautwiki.org + mediawiki · no medical advice"
        )
        return embed

    def _dose_embeds(
        self, detail: Dict[str, Any], route: Optional[str]
    ) -> List[discord.Embed]:
        name = str(detail.get("name") or "?")
        url = str(detail.get("url") or "")
        roas = [roa for roa in detail.get("roas") or [] if isinstance(roa, dict)]
        if route:
            wanted = norm(route)
            roas = [roa for roa in roas if norm(str(roa.get("name") or "")) == wanted]

        pages: List[discord.Embed] = []
        embed = discord.Embed(
            title=f"{name} — dosage",
            url=url,
            colour=0x8E44AD,
            description=(
                "Values as listed by PsychonautWiki. Dosage depends on body weight, "
                "tolerance and purity — read the wiki page before use."
            ),
        )
        for roa in roas:
            rows = dose_rows(roa.get("dose"))
            timing = duration_rows(roa.get("duration"))
            if not rows and not timing:
                continue
            lines = [f"**{label}:** {value}" for label, value in rows]
            if timing:
                if lines:
                    lines.append("")
                lines.extend(f"*{label}:* {value}" for label, value in timing)
            bioavailability = roa.get("bioavailability") or {}
            if isinstance(bioavailability, dict):
                low = str(bioavailability.get("min") or "").strip()
                high = str(bioavailability.get("max") or "").strip()
                if low:
                    span = f"{low}–{high}%" if high and high != low else f"{low}%"
                    lines.append(f"*Bioavailability:* {span}")
            label = str(roa.get("name") or "?").strip().title()
            embed.add_field(
                name=label, value=trim("\n".join(lines), 1024), inline=False
            )
            if len(embed.fields) == 4:
                pages.append(embed)
                embed = discord.Embed(
                    title=f"{name} — dosage (cont.)", url=url, colour=0x8E44AD
                )
        if embed.fields:
            pages.append(embed)
        for page in pages:
            page.set_footer(text=f"PsychonautWiki · {url}")
        return pages

    def _effects_embeds(self, detail: Dict[str, Any]) -> List[discord.Embed]:
        name = str(detail.get("name") or "?")
        url = str(detail.get("url") or "")
        effects = detail.get("effects") or {}
        pages: List[discord.Embed] = []
        if not isinstance(effects, dict) or not effects:
            return pages
        for category, effects_list in effects.items():
            label = EFFECT_LABELS.get(category, category.title())
            chunks = paginate([f"• {effect}" for effect in effects_list], 22)
            for index, chunk in enumerate(chunks, start=1):
                embed = discord.Embed(
                    title=f"{name} — {label} effects",
                    description=trim("\n".join(chunk), 4000),
                    colour=0x2ECC71,
                    url=url,
                )
                footer = f"{len(effects_list)} effects · PsychonautWiki"
                if len(chunks) > 1:
                    footer = f"page {index}/{len(chunks)} · {footer}"
                embed.set_footer(text=footer)
                pages.append(embed)
        return pages

    def _interaction_embed(self, detail: Dict[str, Any]) -> discord.Embed:
        name = str(detail.get("name") or "?")
        embed = discord.Embed(
            title=f"{name} — interactions",
            url=str(detail.get("url") or ""),
            colour=0xE67E22,
            description=(
                "Taken from the wiki's interaction tables. Combination effects are "
                "often unpredictable — this is not medical advice."
            ),
        )
        groups = (
            ("🔴 Dangerous", detail.get("dangerous") or []),
            ("🟠 Unsafe", detail.get("unsafe") or []),
            ("🟡 Uncertain", detail.get("uncertain") or []),
        )
        empty = True
        for label, items in groups:
            values = [str(item) for item in items if item]
            if not values:
                continue
            empty = False
            embed.add_field(
                name=f"{label} ({len(values)})",
                value=trim(", ".join(values[:40]), 1024),
                inline=False,
            )
        if empty:
            embed.add_field(
                name="No interaction data",
                value="The wiki lists no interactions for this substance.",
                inline=False,
            )
        embed.set_footer(text="PsychonautWiki · psychonautwiki.org")
        return embed

    # ------------------------------------------------------------------ commands

    @commands.group(
        name="psychonaut",
        aliases=["pw", "psy"],
        invoke_without_command=True,
    )
    async def psychonaut(
        self, ctx: commands.Context, *, name: Optional[str] = None
    ) -> None:
        """Look up a psychoactive substance on PsychonautWiki.

        Without an argument you get the command overview.
        """
        if name is None:
            prefix = ctx.clean_prefix
            embed = discord.Embed(
                title="PsychonautWiki",
                description=(
                    "Dosages, durations, effects and interactions from "
                    "[psychonautwiki.org](https://psychonautwiki.org).\n\n"
                    f"**`{prefix}psychonaut <name>`** — substance overview\n"
                    f"**`{prefix}psychonaut dose <name> [route]`** — dose and duration table\n"
                    f"**`{prefix}psychonaut effects <name>`** — subjective effects by category\n"
                    f"**`{prefix}psychonaut interactions <name>`** — combination warnings\n"
                    f"**`{prefix}psychonaut search <text>`** — search names and aliases\n"
                    f"**`{prefix}psychonaut class [class]`** — browse by substance class\n"
                    f"**`{prefix}psychonaut random [class]`** — random substance\n"
                    f"**`{prefix}psychonaut stats`** — cache state\n"
                    f"**`{prefix}psychonaut refresh`** — reload the index (bot owner)\n\n"
                    "*Harm reduction only — no medical advice.*"
                ),
                colour=await ctx.embed_color(),
            )
            await ctx.send(embed=embed)
            return

        title = await self._resolve(ctx, name)
        if title is None:
            return
        detail = await self._detail_or_fail(ctx, title)
        if detail is None:
            return
        await ctx.send(embed=self._lookup_embed(ctx, detail))

    @psychonaut.command(name="dose", aliases=["dosage", "doses"])
    async def psychonaut_dose(
        self, ctx: commands.Context, name: str, *, route: Optional[str] = None
    ) -> None:
        """Show dose ranges and durations, optionally for one route of administration."""
        title = await self._resolve(ctx, name)
        if title is None:
            return
        detail = await self._detail_or_fail(ctx, title)
        if detail is None:
            return
        pages = self._dose_embeds(detail, route)
        if not pages:
            if route:
                text = (
                    f"PsychonautWiki lists no dosing for the route "
                    f"**{trim(route, 40)}** on {title}. "
                    f"Available: "
                    + humanize_list(
                        [
                            str(roa.get("name") or "").title()
                            for roa in detail.get("roas") or []
                            if roa.get("name")
                        ]
                        or ["none"]
                    )
                    + "."
                )
            else:
                text = f"PsychonautWiki lists no dose data for **{title}**."
            await ctx.send(
                embed=self._error_embed(await ctx.embed_color(), "No dosage data", text)
            )
            return
        if len(pages) == 1:
            await ctx.send(embed=pages[0])
        else:
            await menu(ctx, pages, DEFAULT_CONTROLS)

    @psychonaut.command(name="effects", aliases=["effect"])
    async def psychonaut_effects(self, ctx: commands.Context, *, name: str) -> None:
        """List the subjective effects the wiki documents for a substance."""
        title = await self._resolve(ctx, name)
        if title is None:
            return
        detail = await self._detail_or_fail(ctx, title)
        if detail is None:
            return
        pages = self._effects_embeds(detail)
        if not pages:
            await ctx.send(
                embed=self._error_embed(
                    await ctx.embed_color(),
                    "No effect list",
                    f"The PsychonautWiki article for **{title}** has no parsed "
                    "subjective effects section.",
                )
            )
            return
        if len(pages) == 1:
            await ctx.send(embed=pages[0])
        else:
            await menu(ctx, pages, DEFAULT_CONTROLS)

    @psychonaut.command(name="interactions", aliases=["interaction", "inter"])
    async def psychonaut_interactions(
        self, ctx: commands.Context, *, name: str
    ) -> None:
        """Show dangerous, unsafe and uncertain combinations."""
        title = await self._resolve(ctx, name)
        if title is None:
            return
        detail = await self._detail_or_fail(ctx, title)
        if detail is None:
            return
        await ctx.send(embed=self._interaction_embed(detail))

    @psychonaut.command(name="search", aliases=["find"])
    async def psychonaut_search(self, ctx: commands.Context, *, text: str) -> None:
        """Search substance names and street names."""
        needle = norm(text)
        if not needle:
            await ctx.send(
                embed=self._error_embed(
                    await ctx.embed_color(),
                    "Nothing to search for",
                    f"`{ctx.clean_prefix}psychonaut search <text>`",
                )
            )
            return
        rows = await self._index_or_fail(ctx)
        if rows is None:
            return

        hits: List[Tuple[str, str]] = []
        for row in rows:
            name = str(row.get("name") or "")
            names = [name] + as_list(row.get("common"))
            if any(needle in norm(alias) for alias in names if alias):
                hits.append(
                    (
                        name,
                        self._class_text(
                            {
                                "psychoactive": row.get("psychoactive"),
                                "chemical": row.get("chemical"),
                            }
                        ),
                    )
                )
        if not hits:
            try:
                found = await self._search_remote(text)
            except Exception:  # noqa: BLE001 - nothing found is the expected answer
                found = []
            hits = [
                (
                    str(row.get("name") or ""),
                    self._class_text(row.get("class") or {}),
                )
                for row in found
                if row.get("name")
            ]
        if not hits:
            await ctx.send(
                embed=self._error_embed(
                    await ctx.embed_color(),
                    "No matches",
                    f"Nothing in the PsychonautWiki index matches **{trim(text, 80)}**.",
                )
            )
            return

        hits.sort(key=lambda item: item[0].lower())
        lines = [f"• **{name}** — {classes}" for name, classes in hits]
        pages: List[discord.Embed] = []
        for page in paginate(lines, 15):
            embed = discord.Embed(
                title=f"🔎 {text}",
                description=trim("\n".join(page), 4000),
                colour=await ctx.embed_color(),
            )
            pages.append(embed)
        for index, embed in enumerate(pages, start=1):
            embed.set_footer(text=f"{len(hits)} matches · page {index}/{len(pages)}")
        if len(pages) == 1:
            await ctx.send(embed=pages[0])
        else:
            await menu(ctx, pages, DEFAULT_CONTROLS)

    @psychonaut.command(name="class", aliases=["classes"])
    async def psychonaut_class(
        self, ctx: commands.Context, *, name: Optional[str] = None
    ) -> None:
        """Browse substances by psychoactive class, or list the classes."""
        rows = await self._index_or_fail(ctx)
        if rows is None:
            return

        if name is None:
            counts: Dict[str, int] = {}
            for row in rows:
                for value in as_list(row.get("psychoactive")):
                    key = humanize_class(value)
                    counts[key] = counts.get(key, 0) + 1
            if not counts:
                await ctx.send(
                    embed=self._error_embed(
                        await ctx.embed_color(),
                        "No classes",
                        "The index carries no psychoactive classes right now.",
                    )
                )
                return
            ordered = sorted(counts.items(), key=lambda item: (-item[1], item[0]))
            lines = [f"• **{label}** ({count})" for label, count in ordered]
            pages: List[discord.Embed] = []
            for page in paginate(lines, 20):
                embed = discord.Embed(
                    title="Substance classes",
                    description="\n".join(page),
                    colour=await ctx.embed_color(),
                )
                embed.set_footer(
                    text=f"{len(ordered)} classes · {ctx.clean_prefix}psychonaut class <name>"
                )
                pages.append(embed)
            if len(pages) == 1:
                await ctx.send(embed=pages[0])
            else:
                await menu(ctx, pages, DEFAULT_CONTROLS)
            return

        wanted = norm(name)
        members: Dict[str, List[str]] = {}
        for row in rows:
            for value in as_list(row.get("psychoactive")) + as_list(
                row.get("chemical")
            ):
                key = humanize_class(value)
                if class_matches(wanted, key):
                    members.setdefault(key, []).append(str(row.get("name") or ""))
        if not members:
            available = sorted(
                {
                    humanize_class(value)
                    for row in rows
                    for value in as_list(row.get("psychoactive"))
                }
            )
            await ctx.send(
                embed=self._error_embed(
                    await ctx.embed_color(),
                    f"No class “{trim(name, 60)}”",
                    "Known classes: " + humanize_list(available[:15]) + ".",
                )
            )
            return

        label, names = max(members.items(), key=lambda item: len(item[1]))
        names = sorted({entry for entry in names if entry}, key=str.lower)
        pages = []
        for page in paginate([f"• {entry}" for entry in names], 20):
            embed = discord.Embed(
                title=f"{label} substances",
                description="\n".join(page),
                colour=await ctx.embed_color(),
            )
            pages.append(embed)
        for index, embed in enumerate(pages, start=1):
            embed.set_footer(
                text=f"{len(names)} substances · page {index}/{len(pages)}"
            )
        if len(pages) == 1:
            await ctx.send(embed=pages[0])
        else:
            await menu(ctx, pages, DEFAULT_CONTROLS)

    @psychonaut.command(name="random")
    async def psychonaut_random(
        self, ctx: commands.Context, *, name: Optional[str] = None
    ) -> None:
        """Pick a random substance, optionally from one class."""
        rows = await self._index_or_fail(ctx)
        if rows is None:
            return
        pool = rows
        if name:
            wanted = norm(name)
            pool = [
                row
                for row in rows
                if any(
                    class_matches(wanted, humanize_class(value))
                    for value in as_list(row.get("psychoactive"))
                    + as_list(row.get("chemical"))
                )
            ]
            if not pool:
                await ctx.send(
                    embed=self._error_embed(
                        await ctx.embed_color(),
                        f"No class “{trim(name, 60)}”",
                        f"Try `{ctx.clean_prefix}psychonaut class` for the full list.",
                    )
                )
                return
        title = str(random.choice(pool).get("name") or "")
        detail = await self._detail_or_fail(ctx, title)
        if detail is None:
            return
        await ctx.send(embed=self._lookup_embed(ctx, detail))

    @psychonaut.command(name="stats")
    async def psychonaut_stats(self, ctx: commands.Context) -> None:
        """Show what the cog has cached and how old it is."""
        now = time.time()
        index_age = (
            f"{int((now - self._index_stamp) / 3600)}h ago"
            if self._index_stamp
            else "never"
        )
        details = self._cache.get("detail") or {}
        saved = sum(
            len(json.dumps(entry))
            for entry in details.values()
            if isinstance(entry, dict)
        )
        embed = discord.Embed(
            title="PsychonautWiki cache", colour=await ctx.embed_color()
        )
        embed.add_field(
            name="Index", value=f"{len(self._index_rows)} substances", inline=True
        )
        embed.add_field(name="Index age", value=index_age, inline=True)
        embed.add_field(name="Cached pages", value=str(len(details)), inline=True)
        embed.add_field(name="Cache size", value=f"{saved / 1024:.0f} KiB", inline=True)
        embed.add_field(
            name="Detail TTL", value=f"{DETAIL_TTL // 86400} days", inline=True
        )
        embed.add_field(
            name="Index TTL", value=f"{INDEX_TTL // 86400} days", inline=True
        )
        embed.set_footer(text="PsychonautWiki · api.psychonautwiki.org + mediawiki")
        await ctx.send(embed=embed)

    @psychonaut.command(name="refresh")
    @checks.is_owner()
    async def psychonaut_refresh(self, ctx: commands.Context) -> None:
        """Drop the cached data and fetch the substance index again."""
        await ctx.typing()
        self._cache["detail"] = {}
        self._index_rows = []
        self._index_stamp = 0.0
        try:
            rows = await self._index(force=True)
        except PsychonautError as error:
            await ctx.send(
                embed=self._error_embed(
                    await ctx.embed_color(),
                    "Refresh failed",
                    f"{error}.\n\nNothing was lost: the previous cache file is still on "
                    "disk and will be used on the next load.",
                )
            )
            return
        await ctx.send(
            embed=discord.Embed(
                title="PsychonautWiki refreshed",
                description=(
                    f"Index reloaded: **{len(rows)} substances**.\n"
                    f"Cached detail pages cleared, {len(self._index_map)} names and "
                    "aliases indexed."
                ),
                colour=await ctx.embed_color(),
            )
        )
