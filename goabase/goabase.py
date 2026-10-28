"""goabase.net party announcements for Red-DiscordBot.

Watches the goabase.net event catalogue for new Goa/Psytrance parties and posts
each new one as an embed with link buttons (tickets, map, party page).

The site publishes both an RSS feed (``/party/rss/``, 20 newest *by date*) and a
schema.org export. The RSS feed is not usable for new-party detection: an entry
only appears once its date moves into the next 20 positions, so a party
announced three months ahead shows up late and one that drops out of the window
looks like a deletion. The JSON-LD export lists every upcoming party per country
with a stable id, so a set difference over poll intervals is the reliable signal.

    GET https://www.goabase.net/api/party/jsonld/?country=CH   -> ids
    GET https://www.goabase.net/api/party/jsonld/<id>          -> full event

Party data is published by goabase.net under CC BY-SA 2.0; the embed footer
carries the credit.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Sequence, Tuple
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import aiohttp
import discord
from redbot.core import Config, commands
from redbot.core.bot import Red
from redbot.core.utils.chat_formatting import humanize_list

from . import parties as api

log = logging.getLogger("red.freak_cogs.goabase")

REQUEST_TIMEOUT = 20
MAX_ATTEMPTS = 3
TICK_SECONDS = 60
#: How long a party id stays in the "already announced" set.
SEEN_RETENTION_DAYS = 45
MIN_INTERVAL = 300
MAX_INTERVAL = 21600
MAX_DAYS = 365
MIN_BATCH = 1
MAX_BATCH = 10
MAX_COUNTRIES = 20
#: Detail fetches per poll, so a bulk import cannot hammer the site.
DETAILS_PER_CYCLE = 15
#: Give up on a party whose detail payload keeps failing.
DETAIL_FAIL_LIMIT = 3
#: How long the live country list is cached.
COUNTRY_TTL = 3600
#: Parties that started this long ago are not "new" any more.
STALE_GRACE_HOURS = 12
USER_AGENT = "Red-DiscordBot goabase cog (+https://github.com/cens0r3d/freak-cogs)"
#: Party links: /party/<slug>/<id>, /party/<id>, /de/party/<slug>/<id> and the
#: bare /<id> form the RSS guid uses.
LINK_RE = re.compile(
    r"https?://(?:www\.)?goabase\.net/(?:[a-z]{2}/)?(?:party/(?:[^/\s?#]+/)?)?(\d{1,7})(?:\b|[/?#])",
    re.IGNORECASE,
)
#: Detail payloads are cached this long so a link posted twice costs nothing.
EVENT_TTL = 600
MAX_EVENT_CACHE = 200
#: Embeds per message, so a wall of links cannot flood the channel.
MAX_LINKS_PER_MESSAGE = 3


class PartyView(discord.ui.View):
    """Link buttons for one party announcement."""

    def __init__(self, buttons: Sequence[Dict[str, str]]):
        super().__init__(timeout=None)
        for spec in list(buttons)[:5]:
            self.add_item(
                discord.ui.Button(
                    style=discord.ButtonStyle.link,
                    label=str(spec.get("label") or "Open")[:80],
                    emoji=spec.get("emoji") or None,
                    url=str(spec["url"]),
                )
            )


class Goabase(commands.Cog):
    """Announce new goabase.net parties in a channel."""

    __author__ = "cens0r3d"
    __version__ = "1.1.0"

    def __init__(self, bot: Red):
        self.bot = bot
        self.config = Config.get_conf(
            self, identifier="goabase", force_registration=True
        )
        self.config.register_guild(
            channel=None,
            countries=["CH"],
            worldwide=False,
            interval=1800,
            timezone="Europe/Zurich",
            days=120,
            batch=5,
            seen={},
            paused=False,
            autoembed=False,
            autochannel=None,
        )
        self._country_cache: Optional[Dict[str, str]] = None
        self._country_cache_at: float = 0.0
        self.session: Optional[aiohttp.ClientSession] = None
        self._ticker: Optional[asyncio.Task] = None
        self._next_run: Dict[int, float] = {}
        self._failures: Dict[int, int] = {}
        self._event_cache: Dict[int, Tuple[float, Dict[str, Any]]] = {}

    # ------------------------------------------------------------------ lifecycle

    async def cog_load(self) -> None:
        self.session = aiohttp.ClientSession(
            headers={
                "User-Agent": USER_AGENT,
                "Accept": "application/ld+json, application/json",
            }
        )
        self._ticker = asyncio.create_task(self._ticker_loop())

    def cog_unload(self) -> None:
        if self._ticker is not None:
            self._ticker.cancel()
            self._ticker = None
        if self.session is not None:
            session = self.session
            self.session = None
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                loop = None
            if loop is not None:
                loop.create_task(session.close())

    @commands.Cog.listener()
    async def on_guild_channel_delete(self, channel: discord.abc.GuildChannel) -> None:
        """Clear the announcement channel when that channel is deleted."""
        guild = channel.guild
        if guild is None:
            return
        try:
            configured = await self.config.guild(guild).channel()
        except Exception:  # pragma: no cover - config read guard
            return
        if configured and int(configured) == channel.id:
            await self.config.guild(guild).channel.set(None)
            log.info(
                "Announcement channel removed in guild %s, cleared the setting",
                guild.id,
            )

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        """Post an embed when someone drops a goabase party link."""
        if message.guild is None or message.author.bot:
            return
        content = message.content or ""
        if "goabase" not in content.lower():
            return
        try:
            if await self.bot.cog_disabled_in_guild(self, message.guild):
                return
        except Exception:  # never let the guard kill the listener
            log.debug("goabase: cog_disabled_in_guild check failed", exc_info=True)
        conf = await self.config.guild(message.guild).all()
        if not conf.get("autoembed"):
            return
        autochannel = conf.get("autochannel")
        if autochannel and message.channel.id != int(autochannel):
            return

        ids: List[int] = []
        for match in LINK_RE.finditer(content):
            party_id = int(match.group(1))
            if party_id not in ids:
                ids.append(party_id)
        if not ids:
            return

        tz = self._tz(conf.get("timezone"))
        for party_id in ids[:MAX_LINKS_PER_MESSAGE]:
            event = await self._cached_event(party_id)
            if event is None:
                log.debug("goabase: no payload for party %s in a posted link", party_id)
                continue
            await self._send_party(message.channel, event, tz, reference=message)

    # ------------------------------------------------------------------ http

    async def _get_json(self, url: str) -> Optional[Any]:
        if self.session is None:
            return None
        last_error = "no attempt"
        for attempt in range(1, MAX_ATTEMPTS + 1):
            try:
                async with self.session.get(
                    url, timeout=aiohttp.ClientTimeout(total=REQUEST_TIMEOUT)
                ) as resp:
                    if resp.status == 404:
                        return None
                    if resp.status == 429 or resp.status >= 500:
                        last_error = f"HTTP {resp.status}"
                        await asyncio.sleep(min(2 * attempt, 12))
                        continue
                    resp.raise_for_status()
                    return await resp.json(content_type=None)
            except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as exc:
                last_error = f"{exc.__class__.__name__}: {exc}"
                await asyncio.sleep(min(2 * attempt, 8))
        log.warning("goabase request failed for %s (%s)", url, last_error)
        return None

    async def _party_ids(
        self, countries: Sequence[str], worldwide: bool = False
    ) -> Optional[Dict[int, str]]:
        """Upcoming party ids: the whole catalogue, or one country at a time."""
        found: Dict[int, str] = {}
        reachable = False
        if worldwide:
            payload = await self._get_json(f"{api.API_ROOT}/")
            if not isinstance(payload, dict):
                return None
            for item in payload.get("itemListElement") or []:
                if not isinstance(item, dict):
                    continue
                party_id = api.party_id_from_url(item.get("url"))
                if party_id:
                    found[party_id] = str(item.get("name") or "")
            return found
        for code in countries:
            payload = await self._get_json(f"{api.API_ROOT}/?country={code}")
            if not isinstance(payload, dict):
                continue
            reachable = True
            for item in payload.get("itemListElement") or []:
                if not isinstance(item, dict):
                    continue
                party_id = api.party_id_from_url(item.get("url"))
                if party_id:
                    found[party_id] = str(item.get("name") or "")
        return found if reachable else None

    async def _country_map(self) -> Dict[str, str]:
        """Country code -> name, as goabase lists it right now.

        The site's list is not a whitelist of supported countries: it is exactly
        the countries that currently have upcoming parties, so it grows and
        shrinks with the catalogue (France is absent while no French party is
        listed). ``?country=<code>`` accepts any ISO code and answers an empty
        list for the others, so the live list is only used to label and to warn -
        never to reject a code.
        """
        now = time.monotonic()
        if (
            self._country_cache is not None
            and now - self._country_cache_at < COUNTRY_TTL
        ):
            return self._country_cache
        payload = await self._get_json(f"{api.API_ROOT}/country/list")
        found: Dict[str, str] = {}
        if isinstance(payload, dict):
            for item in payload.get("itemListElement") or []:
                if not isinstance(item, dict):
                    continue
                # ".../country/AR" -> "AR" (not an id, so not party_id_from_url)
                tail = str(item.get("url") or "").rstrip("/").rsplit("/", 1)[-1].upper()
                if len(tail) != 2 or not tail.isalpha():
                    continue
                name = str(item.get("name") or "").strip()
                if name:
                    found[tail] = name
        if not found:
            # Offline or a changed payload shape: the bundled snapshot still
            # labels the usual suspects.
            return api.COUNTRIES
        self._country_cache = found
        self._country_cache_at = now
        return found

    async def _party_event(self, party_id: int) -> Optional[Dict[str, Any]]:
        payload = await self._get_json(f"{api.API_ROOT}/{party_id}")
        if payload is None:
            return None
        return api.event_from_payload(payload)

    async def _cached_event(self, party_id: int) -> Optional[Dict[str, Any]]:
        """Fetch a party's detail payload, reusing a recent answer."""
        now = time.monotonic()
        cached = self._event_cache.get(party_id)
        if cached is not None and now - cached[0] < EVENT_TTL:
            return cached[1]
        event = await self._party_event(party_id)
        if event is None:
            return None
        if len(self._event_cache) >= MAX_EVENT_CACHE:
            oldest = min(self._event_cache, key=lambda key: self._event_cache[key][0])
            self._event_cache.pop(oldest, None)
        self._event_cache[party_id] = (now, event)
        return event

    async def _upcoming(
        self, countries: Sequence[str], limit: int, worldwide: bool = False
    ) -> List[Dict[str, Any]]:
        """The next ``limit`` parties by start date."""
        ids = await self._party_ids(countries, worldwide)
        if not ids:
            return []
        wanted = max(limit * 2, limit)
        events: List[Dict[str, Any]] = []
        for position, party_id in enumerate(ids):
            if position >= wanted:
                break
            event = await self._party_event(party_id)
            if event is not None:
                events.append(event)
        events.sort(key=lambda item: item["start"])
        return events[:limit]

    # ------------------------------------------------------------------ polling

    async def _ticker_loop(self) -> None:
        await self.bot.wait_until_red_ready()
        while True:
            try:
                for guild in list(self.bot.guilds):
                    conf = await self.config.guild(guild).all()
                    if not conf.get("channel") or conf.get("paused"):
                        continue
                    now = time.monotonic()
                    if now < self._next_run.get(guild.id, 0.0):
                        continue
                    self._next_run[guild.id] = now + int(conf.get("interval") or 1800)
                    try:
                        await self._poll_guild(guild, conf)
                    except asyncio.CancelledError:
                        raise
                    except Exception:  # one broken guild must not kill the loop
                        log.exception("goabase poll failed for guild %s", guild.id)
                    await asyncio.sleep(2)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("goabase ticker failed")
            await asyncio.sleep(TICK_SECONDS)

    async def _poll_guild(self, guild: discord.Guild, conf: Dict[str, Any]) -> None:
        channel_id = conf.get("channel")
        channel = guild.get_channel(int(channel_id)) if channel_id else None
        if channel is None:
            return

        countries = list(conf.get("countries") or ["CH"])
        ids = await self._party_ids(countries, bool(conf.get("worldwide")))
        if not ids:
            return

        now = datetime.now(timezone.utc)
        seen: Dict[str, str] = dict(conf.get("seen") or {})

        if not seen:
            await self.config.guild(guild).seen.set(
                {str(party_id): now.isoformat() for party_id in ids}
            )
            log.info(
                "goabase: baselined %s parties for guild %s, nothing posted",
                len(ids),
                guild.id,
            )
            return

        pending = [
            party_id
            for party_id in ids
            if str(party_id) not in seen
            and self._failures.get(party_id, 0) < DETAIL_FAIL_LIMIT
        ]
        if not pending:
            await self._persist_seen(guild, seen, ids, now)
            return

        cutoff_past = now - timedelta(hours=STALE_GRACE_HOURS)
        cutoff_future = now + timedelta(days=int(conf.get("days") or 120))
        announce: List[Dict[str, Any]] = []
        handled: List[int] = []

        for party_id in pending[:DETAILS_PER_CYCLE]:
            event = await self._party_event(party_id)
            if event is None:
                self._failures[party_id] = self._failures.get(party_id, 0) + 1
                if self._failures[party_id] >= DETAIL_FAIL_LIMIT:
                    log.warning("goabase: giving up on party %s", party_id)
                    handled.append(party_id)
                await asyncio.sleep(1)
                continue
            handled.append(party_id)
            if cutoff_past <= event["start"] <= cutoff_future:
                announce.append(event)
            await asyncio.sleep(1)

        announce.sort(key=lambda item: item["start"])
        tz = self._tz(conf.get("timezone"))
        batch = max(MIN_BATCH, min(MAX_BATCH, int(conf.get("batch") or 5)))
        for event in announce[:batch]:
            await self._send_party(channel, event, tz)
        skipped = len(announce) - min(len(announce), batch)
        if skipped > 0:
            log.info(
                "goabase: %s further new parties held back for the next cycle", skipped
            )

        for party_id in handled:
            self._failures.pop(party_id, None)
            seen[str(party_id)] = now.isoformat()
        await self._persist_seen(guild, seen, ids, now)

    async def _persist_seen(
        self,
        guild: discord.Guild,
        seen: Dict[str, str],
        current: Dict[int, str],
        now: datetime,
    ) -> None:
        horizon = now - timedelta(days=SEEN_RETENTION_DAYS)
        cleaned: Dict[str, str] = {}
        for key, stamp in seen.items():
            try:
                when = datetime.fromisoformat(str(stamp))
            except ValueError:
                when = now
            if when.tzinfo is None:
                when = when.replace(tzinfo=timezone.utc)
            if when >= horizon or int(key) in current:
                cleaned[key] = when.isoformat()
        await self.config.guild(guild).seen.set(cleaned)

    # ------------------------------------------------------------------ rendering

    def _tz(self, name: Optional[str]) -> Any:
        try:
            return ZoneInfo(str(name or "Europe/Zurich"))
        except (ZoneInfoNotFoundError, ValueError):
            log.warning(
                "goabase: unknown timezone %r, falling back to Europe/Zurich", name
            )
            return ZoneInfo("Europe/Zurich")

    async def _send_party(
        self,
        channel: discord.abc.Messageable,
        event: Dict[str, Any],
        tz,
        reference: Optional[discord.Message] = None,
    ) -> bool:
        spec = api.build_embed_spec(event, tz)
        embed = discord.Embed(
            title=spec["title"][:256],
            url=spec["url"],
            description=spec["description"][:4096],
            colour=discord.Colour(spec["colour"]),
        )
        author = spec.get("author") or {}
        if author:
            embed.set_author(
                name=str(author.get("name"))[:256], icon_url=author.get("icon_url")
            )
        for field in spec["fields"]:
            if field["name"] and field["value"]:
                embed.add_field(
                    name=str(field["name"])[:256],
                    value=str(field["value"])[:1024],
                    inline=bool(field.get("inline")),
                )
        if spec.get("image"):
            embed.set_image(url=spec["image"])
        if spec.get("footer"):
            embed.set_footer(text=str(spec["footer"])[:2048])

        view = PartyView(spec.get("buttons") or [])
        payload: Dict[str, Any] = {
            "embed": embed,
            "view": view,
            "allowed_mentions": discord.AllowedMentions.none(),
        }
        if reference is not None:
            payload["reference"] = reference
            payload["mention_author"] = False
        try:
            await channel.send(**payload)
            return True
        except discord.Forbidden:
            log.warning(
                "goabase: missing permissions to post in channel %s",
                getattr(channel, "id", "?"),
            )
        except discord.HTTPException as exc:
            log.warning("goabase: sending party %s failed: %s", event.get("id"), exc)
        return False

    # ------------------------------------------------------------------ commands

    @commands.hybrid_group(
        name="goabase", aliases=["gbase"], invoke_without_command=True
    )
    @commands.guild_only()
    async def goabase(self, ctx: commands.Context) -> None:
        """Announce new goabase.net parties in a channel."""
        await ctx.send_help()

    @goabase.command(name="channel")
    @commands.admin_or_permissions(manage_guild=True)
    async def goabase_channel(
        self, ctx: commands.Context, channel: Optional[discord.TextChannel] = None
    ) -> None:
        """Set (or clear) the channel new parties are posted to."""
        if channel is None:
            await self.config.guild(ctx.guild).channel.set(None)
            await ctx.send("Announcement channel cleared; polling is off.")
            return
        perms = channel.permissions_for(ctx.guild.me)
        missing = [
            label
            for label, allowed in (
                ("View Channel", perms.view_channel),
                ("Send Messages", perms.send_messages),
                ("Embed Links", perms.embed_links),
            )
            if not allowed
        ]
        if missing:
            await ctx.send(
                f"I am missing these permissions in {channel.mention}: {humanize_list(missing)}"
            )
            return
        await self.config.guild(ctx.guild).channel.set(channel.id)
        # A fresh channel means a fresh baseline on the next poll.
        await self.config.guild(ctx.guild).seen.set({})
        await ctx.send(
            f"New goabase parties will be posted in {channel.mention}.\n"
            "The next poll records the current parties as a baseline and posts nothing; "
            "only parties added afterwards are announced. Use `preview` to post the upcoming ones now."
        )

    @goabase.command(name="worldwide")
    @commands.admin_or_permissions(manage_guild=True)
    async def goabase_worldwide(
        self, ctx: commands.Context, state: Optional[bool] = None
    ) -> None:
        """Announce parties from every country instead of a country list.

        Without an argument this toggles the setting. Worldwide mode reads the
        whole catalogue in a single request, so it is cheaper than watching many
        countries - but it also sees every party on the site, which is why the
        day window and the batch size matter there.
        """
        current = await self.config.guild(ctx.guild).worldwide()
        target = (not current) if state is None else bool(state)
        await self.config.guild(ctx.guild).worldwide.set(target)
        await self.config.guild(ctx.guild).seen.set({})
        if not target:
            await ctx.send(
                "Worldwide mode off; watching the configured countries again. "
                "The next poll rebuilds the baseline and posts nothing."
            )
            return
        conf = await self.config.guild(ctx.guild).all()
        await ctx.send(
            "Worldwide mode on: every country on goabase, one request per poll. "
            f"Announcements are limited to parties starting within {conf.get('days')} days "
            f"and to {conf.get('batch')} per cycle. The next poll rebuilds the baseline and "
            "posts nothing - `preview 3` posts the upcoming ones now."
        )

    @goabase.command(name="countries")
    @commands.admin_or_permissions(manage_guild=True)
    async def goabase_countries(
        self, ctx: commands.Context, *, codes: str = ""
    ) -> None:
        """Set the countries to watch, as ISO codes (default: CH).

        Separate the codes with spaces or commas, e.g. `CH DE AT`.
        """
        wanted_tokens = [token for token in codes.replace(",", " ").split() if token]
        worldwide = await self.config.guild(ctx.guild).worldwide()
        country_map = await self._country_map()
        if not wanted_tokens:
            current = await self.config.guild(ctx.guild).countries()
            names = [
                country_map.get(code) or api.COUNTRIES.get(code) or code
                for code in current
            ]
            prefix = (
                "Worldwide mode is on, the list is unused: "
                if worldwide
                else "Watching: "
            )
            active = ", ".join(sorted(country_map))
            await ctx.send(
                f"{prefix}{humanize_list(names)}.\n"
                f"Countries with upcoming parties right now ({len(country_map)}): {active}"
            )
            return
        wanted: List[str] = []
        invalid: List[str] = []
        empty: List[str] = []
        for code in wanted_tokens:
            key = code.strip().upper()
            # Any ISO 3166-1 alpha-2 code is accepted: the site answers an empty
            # list for a country without parties, which is not an error.
            if len(key) != 2 or not key.isalpha():
                invalid.append(code)
                continue
            if key not in country_map:
                empty.append(key)
            if key not in wanted:
                wanted.append(key)
        if invalid:
            await ctx.send(
                f"Not a two-letter country code: {humanize_list(invalid)}\n"
                f"Countries with upcoming parties right now: {', '.join(sorted(country_map))}"
            )
            return
        if len(wanted) > MAX_COUNTRIES:
            await ctx.send(f"At most {MAX_COUNTRIES} countries, please.")
            return
        await self.config.guild(ctx.guild).countries.set(wanted)
        await self.config.guild(ctx.guild).seen.set({})
        names = [
            country_map.get(code) or api.COUNTRIES.get(code) or code for code in wanted
        ]
        # A country list only makes sense outside worldwide mode, so setting one
        # switches it off instead of leaving the user with a silent no-op.
        await self.config.guild(ctx.guild).worldwide.set(False)
        note = " Worldwide mode switched off." if worldwide else ""
        if empty:
            note += (
                f" No upcoming parties in {humanize_list(empty)} right now, "
                "so nothing will come from there until one is listed."
            )
        await ctx.send(
            f"Watching {humanize_list(names)}.{note} The next poll rebuilds the baseline, "
            "so the first cycle after a change posts nothing."
        )

    @goabase.command(name="interval")
    @commands.admin_or_permissions(manage_guild=True)
    async def goabase_interval(self, ctx: commands.Context, minutes: int) -> None:
        """Set how often to check for new parties (5-360 minutes)."""
        seconds = minutes * 60
        if not MIN_INTERVAL <= seconds <= MAX_INTERVAL:
            await ctx.send(
                f"Interval must be between {MIN_INTERVAL // 60} and {MAX_INTERVAL // 60} minutes."
            )
            return
        await self.config.guild(ctx.guild).interval.set(seconds)
        await ctx.send(f"Checking for new parties every {minutes} minutes.")

    @goabase.command(name="timezone")
    @commands.admin_or_permissions(manage_guild=True)
    async def goabase_timezone(self, ctx: commands.Context, zone: str) -> None:
        """Set the timezone party times are rendered in (e.g. Europe/Zurich)."""
        try:
            ZoneInfo(zone)
        except (ZoneInfoNotFoundError, ValueError):
            await ctx.send(
                f"`{zone}` is not a known timezone name (use e.g. `Europe/Zurich`)."
            )
            return
        await self.config.guild(ctx.guild).timezone.set(zone)
        await ctx.send(f"Party times are now rendered in `{zone}`.")

    @goabase.command(name="days")
    @commands.admin_or_permissions(manage_guild=True)
    async def goabase_days(self, ctx: commands.Context, days: int) -> None:
        """Only announce parties that start within this many days (1-365)."""
        if not 1 <= days <= MAX_DAYS:
            await ctx.send(f"Days must be between 1 and {MAX_DAYS}.")
            return
        await self.config.guild(ctx.guild).days.set(days)
        await ctx.send(f"Announcing parties that start within the next {days} days.")

    @goabase.command(name="batch")
    @commands.admin_or_permissions(manage_guild=True)
    async def goabase_batch(self, ctx: commands.Context, per_cycle: int) -> None:
        """How many new parties one poll may post at most (1-10)."""
        if not MIN_BATCH <= per_cycle <= MAX_BATCH:
            await ctx.send(f"Batch size must be between {MIN_BATCH} and {MAX_BATCH}.")
            return
        await self.config.guild(ctx.guild).batch.set(per_cycle)
        await ctx.send(f"At most {per_cycle} announcements per poll cycle.")

    @goabase.command(name="pause")
    @commands.admin_or_permissions(manage_guild=True)
    async def goabase_pause(self, ctx: commands.Context) -> None:
        """Stop announcing new parties without losing the settings."""
        await self.config.guild(ctx.guild).paused.set(True)
        await ctx.send("Polling paused.")

    @goabase.command(name="resume")
    @commands.admin_or_permissions(manage_guild=True)
    async def goabase_resume(self, ctx: commands.Context) -> None:
        """Resume announcing new parties."""
        await self.config.guild(ctx.guild).paused.set(False)
        await ctx.send("Polling resumed.")

    @goabase.command(name="reseed")
    @commands.admin_or_permissions(manage_guild=True)
    async def goabase_reseed(self, ctx: commands.Context) -> None:
        """Mark every current party as already announced."""
        conf = await self.config.guild(ctx.guild).all()
        ids = await self._party_ids(
            list(conf.get("countries") or ["CH"]), bool(conf.get("worldwide"))
        )
        if not ids:
            await ctx.send(
                "Could not read the party list from goabase.net; nothing changed."
            )
            return
        now = datetime.now(timezone.utc)
        await self.config.guild(ctx.guild).seen.set(
            {str(party_id): now.isoformat() for party_id in ids}
        )
        await ctx.send(f"{len(ids)} parties marked as already announced.")

    @goabase.command(name="status")
    async def goabase_status(self, ctx: commands.Context) -> None:
        """Show the current configuration and when the next check runs."""
        conf = await self.config.guild(ctx.guild).all()
        channel = (
            ctx.guild.get_channel(int(conf["channel"])) if conf.get("channel") else None
        )
        seen = conf.get("seen") or {}
        country_map = await self._country_map()
        countries = [
            country_map.get(code) or api.COUNTRIES.get(code) or code
            for code in conf.get("countries") or []
        ]
        interval = int(conf.get("interval") or 1800)
        next_run = self._next_run.get(ctx.guild.id)
        if channel is None:
            state = "off — no channel set"
        elif conf.get("paused"):
            state = "paused"
        else:
            state = "active"
        scope = (
            "worldwide (all countries)"
            if conf.get("worldwide")
            else (humanize_list(countries) if countries else "none")
        )
        lines = [
            f"**Channel:** {channel.mention if channel else 'not set'}",
            f"**State:** {state}",
            f"**Countries:** {scope}",
            f"**Interval:** {interval // 60} min",
            f"**Timezone:** {conf.get('timezone')}",
            f"**Max days ahead:** {conf.get('days')}",
            f"**Batch per cycle:** {conf.get('batch')}",
            f"**Known parties:** {len(seen)}",
            f"**Link embeds:** {'on' if conf.get('autoembed') else 'off'}"
            + (f" in <#{int(conf['autochannel'])}>" if conf.get("autochannel") else ""),
        ]
        if next_run and state == "active":
            remaining = max(0, int(next_run - time.monotonic()))
            lines.append(f"**Next check:** in ~{remaining}s")
        await ctx.send("\n".join(lines))

    @goabase.command(name="next")
    async def goabase_next(self, ctx: commands.Context, count: int = 5) -> None:
        """List the next parties without posting them."""
        count = max(1, min(10, count))
        if ctx.interaction is not None and not ctx.interaction.response.is_done():
            await ctx.defer()
        else:
            await ctx.typing()
        conf = await self.config.guild(ctx.guild).all()
        tz = self._tz(conf.get("timezone"))
        events = await self._upcoming(
            list(conf.get("countries") or ["CH"]), count, bool(conf.get("worldwide"))
        )
        if not events:
            await ctx.send("No upcoming parties found.")
            return
        embed = discord.Embed(
            title=f"Next {len(events)} goabase parties",
            url=api.PARTY_LIST_URL,
            colour=discord.Colour(api.COLOR),
        )
        for event in events:
            when = api.format_when(event["start"], None, tz)
            place, _ = event["location"]
            embed.add_field(
                name=when,
                value=f"[{event['name']}]({event['url']})"
                + (f"\n{place}" if place else ""),
                inline=False,
            )
        embed.set_footer(text="goabase.net · CC BY-SA 2.0")
        await ctx.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())

    @goabase.command(name="preview")
    @commands.admin_or_permissions(manage_guild=True)
    async def goabase_preview(self, ctx: commands.Context, count: int = 3) -> None:
        """Post the next parties now, as the announcement would look."""
        count = max(1, min(10, count))
        if ctx.interaction is not None and not ctx.interaction.response.is_done():
            await ctx.defer()
        else:
            await ctx.typing()
        conf = await self.config.guild(ctx.guild).all()
        tz = self._tz(conf.get("timezone"))
        events = await self._upcoming(
            list(conf.get("countries") or ["CH"]), count, bool(conf.get("worldwide"))
        )
        if not events:
            await ctx.send("No upcoming parties found.")
            return
        target = ctx.channel
        if conf.get("channel") and ctx.channel.id != int(conf["channel"]):
            configured = ctx.guild.get_channel(int(conf["channel"]))
            if configured is not None:
                target = configured
        sent = 0
        for event in events:
            if await self._send_party(target, event, tz):
                sent += 1
        if target is not ctx.channel:
            await ctx.send(
                f"Posted {sent} preview announcement(s) in {target.mention}."
            )
        elif sent < len(events):
            await ctx.send(f"Posted {sent} of {len(events)} announcements.")

    @goabase.command(name="autoembed")
    @commands.admin_or_permissions(manage_guild=True)
    async def goabase_autoembed(
        self, ctx: commands.Context, state: Optional[bool] = None
    ) -> None:
        """Post an embed when someone posts a goabase party link.

        Without an argument this toggles the setting. The embed is a reply to
        the message that carried the link; details are cached for ten minutes,
        so a link posted twice costs no request.
        """
        current = await self.config.guild(ctx.guild).autoembed()
        target = (not current) if state is None else bool(state)
        await self.config.guild(ctx.guild).autoembed.set(target)
        if not target:
            await ctx.send("Link embeds are off.")
            return
        channel = await self.config.guild(ctx.guild).autochannel()
        where = (
            f"in <#{int(channel)}>" if channel else "in every channel I can embed in"
        )
        await ctx.send(
            f"Link embeds are on {where}. Post a goabase party link and the embed follows."
        )

    @goabase.command(name="autochannel")
    @commands.admin_or_permissions(manage_guild=True)
    async def goabase_autochannel(
        self, ctx: commands.Context, channel: Optional[discord.TextChannel] = None
    ) -> None:
        """Limit link embeds to one channel (no argument: every channel)."""
        if channel is None:
            await self.config.guild(ctx.guild).autochannel.set(None)
            await ctx.send("Link embeds answer in every channel again.")
            return
        perms = channel.permissions_for(ctx.guild.me)
        if not perms.embed_links or not perms.send_messages:
            await ctx.send(
                f"I need Send Messages and Embed Links in {channel.mention}."
            )
            return
        await self.config.guild(ctx.guild).autochannel.set(channel.id)
        await ctx.send(f"Link embeds only answer in {channel.mention}.")

    @goabase.command(name="show")
    async def goabase_show(self, ctx: commands.Context, link: str = "") -> None:
        """Post the embed for a goabase party link or party id."""
        ids = [int(match.group(1)) for match in LINK_RE.finditer(link)]
        party_id: Optional[int] = ids[0] if ids else None
        if party_id is None and link.strip().isdigit():
            party_id = int(link.strip())
        if party_id is None:
            await ctx.send("Give me a goabase party link (or the party id).")
            return
        if ctx.interaction is not None and not ctx.interaction.response.is_done():
            await ctx.defer()
        else:
            await ctx.typing()
        conf = await self.config.guild(ctx.guild).all()
        event = await self._cached_event(party_id)
        if event is None:
            await ctx.send(
                f"Party {party_id} is not on goabase (or the site is unreachable)."
            )
            return
        await self._send_party(ctx.channel, event, self._tz(conf.get("timezone")))

    # ------------------------------------------------------------------ end user data

    async def red_get_data_for_user(self, *, user_id: int) -> Dict[str, Any]:
        """This cog stores no per-user data."""
        return {}

    async def red_delete_data_for_user(self, *, requester: str, user_id: int) -> None:
        """This cog stores no per-user data."""
        return
