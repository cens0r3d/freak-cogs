"""OSINT — passive infrastructure reconnaissance for Red-DiscordBot.

Everything this cog does is **passive**: it only reads from public sources
(certificate transparency logs, RDAP, DoH resolvers, web archives, blocklists)
and never scans, probes or brute-forces the target. Targets are limited to
infrastructure — IP addresses, domains, ASN, URLs, MAC addresses and file
metadata — so the cog cannot be used to hunt people.

Design notes worth knowing before changing anything:

* Every source is API-key free and HTTP based, so ``requirements`` stays empty
  and an ``[p]cog update`` costs nothing on the bot host.
* ``parsers.py`` next to this file holds the pure logic (classification, RDAP
  shaping, SPF/DMARC, EXIF/PDF metadata) with no framework or network imports,
  so it can be tested on its own.
* Requests are cached (default 30 minutes) and rate limited per source.  The
  free tiers are small — ``ip-api`` allows 45 requests per minute, Shodan's
  InternetDB and certspotter are similar, and ``hackertarget`` only 100 per day,
  which is why it is a last-resort fallback for subdomain discovery.
* ``[p]osint user`` is the one command that makes many requests at once: it
  works through Sherlock's public site list (``usernames.py``) with up to 20
  parallel profile requests.  The list is downloaded once a week and cached in
  the cog's data directory; only a hit where the site's own "not found" signal
  stayed absent counts, everything weaker is reported as *unsure*.
* A group's checks do not reach its subcommands, so the lookup gate and the
  admin check are attached to every command individually.
"""

from __future__ import annotations

import asyncio
import io
import json
import logging
import re
import time
from dataclasses import dataclass, field
from hashlib import md5
from pathlib import Path
from typing import (
    Any,
    Awaitable,
    Callable,
    Dict,
    List,
    Optional,
    Sequence,
    Tuple,
)
from urllib.parse import urljoin

import aiohttp
import discord
from redbot.core import Config, commands
from redbot.core.bot import Red
from redbot.core.utils.chat_formatting import box, humanize_list, inline, text_to_file

try:  # Red's documented data directory; the cog still works memory-only without it
    from redbot.core.data_manager import cog_data_path
except ImportError:  # pragma: no cover - only if Red changes its layout
    cog_data_path = None

from .parsers import (
    classify_target,
    doh_answers,
    doh_authority,
    dns_status,
    exposed_fingerprint,
    file_metadata,
    grade_security_headers,
    gps_links,
    host_of,
    is_public_address,
    is_public_hostname,
    lower_headers,
    mac_details,
    normalize_url,
    parse_dmarc,
    parse_spf,
    pick_entity,
    plural,
    rdap_summary,
    truncate,
)
from .usernames import (
    DATA_FILENAME,
    DATA_MAX_AGE,
    DATA_URL as USERNAME_DATA_URL,
    Check,
    Site,
    VERDICT_ERROR,
    VERDICT_HIT,
    VERDICT_MISS,
    VERDICT_UNSURE,
    classify as classify_response,
    parse_sites,
    profile_url,
    select_sites,
    snapshot as verdict_snapshot,
    summarize as summarize_checks,
    valid_username,
)

log = logging.getLogger("red.freak_cogs.osint")

USER_AGENT = "freak-cogs-osint/1.0 (+https://github.com/cens0r3d/freak-cogs)"
HTTP_TIMEOUT = 20
RETRY_DELAY = 1.5
#: Responses worth one silent retry — CDX and crt.sh answer 502/503 under load.
RETRY_STATUS = (429, 500, 502, 503, 504)
CACHE_SECONDS = 1800
MAX_FILE_BYTES = 8 * 1024 * 1024
MAX_REPORTS_KEPT = 200
MAX_INLINE_LIST = 24
#: Parallel profile checks for `[p]osint user` — high enough to finish in
#: seconds, low enough not to look like an attack to the sites.
USER_CONCURRENCY = 20
#: Bytes of a profile page read to look for the site's own not-found text.
USER_BODY_BYTES = 65536
#: Seconds between two progress edits of the same message. Discord rate limits
#: message edits per channel (about one per second sustained), and a 150-site run
#: finishes checks much faster than that — counting edits instead of timing them
#: produced a wall of 429s on the first live run.
USER_PROGRESS_INTERVAL = 3.0
#: Sites shown inline before the full list becomes a file.
USER_INLINE_HITS = 20

IP_API = "http://ip-api.com/json/{target}"
IP_API_FIELDS = (
    "status,message,country,countryCode,regionName,city,zip,lat,lon,timezone,"
    "isp,org,as,asname,reverse,mobile,proxy,hosting"
)
INTERNETDB = "https://internetdb.shodan.io/{target}"
RDAP = "https://rdap.org/{kind}/{target}"
DOH_GOOGLE = "https://dns.google/resolve"
DOH_CLOUDFLARE = "https://cloudflare-dns.com/dns-query"
CERTSPOTTER = "https://api.certspotter.com/v1/issuances"
CRT_SH = "https://crt.sh/"
HACKERTARGET_HOSTSEARCH = "https://api.hackertarget.com/hostsearch/"
URLSCAN_SEARCH = "https://urlscan.io/api/v1/search/"
WAYBACK_CDX = "https://web.archive.org/cdx/search/cdx"
RIPESTAT = "https://stat.ripe.net/data/{call}/data.json"
STOPFORUMSPAM = "https://api.stopforumspam.org/api"
TOR_EXIT_LIST = "https://check.torproject.org/torbulkexitlist"
MACVENDORS = "https://api.macvendors.com/{target}"
GRAVATAR = "https://gravatar.com/{digest}.json"

DNSBLS = ("zen.spamhaus.org", "bl.spamcop.net")

_TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.I | re.S)
_TAG_RE = re.compile(r"<[^>]+>")

#: Sources this cog contacts, shown by ``[p]osint sources``.
SOURCES: Tuple[Tuple[str, str, str], ...] = (
    (
        "ip-api.com",
        "Geolocation, ISP, ASN, reverse DNS, proxy/hosting flags",
        "IPv4 only, HTTP-only on the free tier, 45 requests per minute",
    ),
    (
        "internetdb.shodan.io",
        "Open ports, observed hostnames, CVEs and CPEs for an IP",
        "Keyless Shodan dataset, one request per IP",
    ),
    (
        "rdap.org",
        "Registry data for domains, IP ranges and AS numbers (the WHOIS successor)",
        "Redirects to the responsible RIR, no key, be gentle",
    ),
    (
        "dns.google / cloudflare-dns.com",
        "DNS records over DNS-over-HTTPS, including TXT, MX and CAA",
        "Keyless, public resolvers",
    ),
    (
        "api.certspotter.com",
        "Subdomains from certificate transparency logs",
        "Keyless with a small per-hour limit, primary subdomain source",
    ),
    (
        "crt.sh",
        "Certificate transparency fallback for subdomains",
        "Keyless, frequently returns 502 — fallback only",
    ),
    (
        "api.hackertarget.com",
        "Host search and reverse IP lookups",
        "Keyless but capped at 100 requests per day, last resort only",
    ),
    (
        "urlscan.io",
        "Search of public URL scans (screenshot, server, IP)",
        "Keyless search API",
    ),
    (
        "web.archive.org",
        "Wayback Machine CDX index of archived URLs",
        "Keyless, can be slow",
    ),
    (
        "stat.ripe.net",
        "Routing data: prefixes, AS holder and announcement status",
        "Keyless RIPEstat, one request per call",
    ),
    (
        "api.stopforumspam.org",
        "Reputation data: spam report count and frequency for an IP",
        "Keyless",
    ),
    (
        "check.torproject.org",
        "Tor exit node list",
        "Keyless, downloaded once and cached for six hours",
    ),
    (
        "api.macvendors.com",
        "Hardware vendor for a MAC address (OUI lookup)",
        "Keyless, rate limited",
    ),
    (
        "gravatar.com",
        "Public Gravatar profile for an email address",
        "Keyless; only the MD5 hash of the address is sent, never the address",
    ),
    (
        "raw.githubusercontent.com",
        "Sherlock's site list (~480 sites) for `[p]osint user`",
        "One download per week, cached in the cog's data directory",
    ),
    (
        "die Sites des Sherlock-Datensatzes",
        "`[p]osint user` fragt jede ausgewählte Site einzeln ab (ein Profilaufruf pro Site)",
        "Standardgrenze 150 Sites pro Lookup, 20 parallel — die IP des Bots ist für "
        "diese Sites sichtbar",
    ),
)

#: Minimum seconds between two calls to the same source.
MIN_INTERVAL = {
    "ip-api": 1.4,
    "internetdb": 1.0,
    "ripestat": 1.0,
    "certspotter": 1.0,
    "crt.sh": 3.0,
    "urlscan": 1.0,
    "hackertarget": 5.0,
    "macvendors": 1.5,
    "stopforumspam": 1.0,
    "gravatar": 1.0,
    "wayback": 1.0,
    "rdap": 0.5,
}

CONFIG_COMMANDS = True


class OSINTError(Exception):
    """A refusal or a dead end that should be shown to the user as-is."""


@dataclass
class Report:
    """What a lookup produced: embeds to send plus the raw payloads."""

    embeds: List[discord.Embed] = field(default_factory=list)
    files: List[discord.File] = field(default_factory=list)
    data: Dict[str, Any] = field(default_factory=dict)


def gated():
    """Attach the guild gate (enabled flag, channel list, cooldown) to a command.

    Red reads ``Command.checks`` only on the command itself and never walks up
    to the parent group, so this has to sit on every lookup command.
    """

    async def predicate(ctx: commands.Context) -> bool:
        ok, message = await ctx.cog.gate(ctx)
        if not ok:
            raise commands.UserFeedbackCheckFailure(message)
        return True

    predicate.__name__ = "osint_gate"
    return commands.check(predicate)


class ReportView(discord.ui.View):
    """Buttons under a lookup result: raw JSON export and the source list.

    The custom ids are fixed, so one data-less instance registered in
    ``cog_load`` answers clicks on stale messages from before a restart
    instead of leaving the interaction to time out.
    """

    def __init__(self, cog: "OSINT", *, timeout: Optional[float] = 300.0) -> None:
        super().__init__(timeout=timeout)
        self.cog = cog

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        entry = self.cog._reports.get(
            interaction.message.id if interaction.message else 0
        )
        if entry is None:
            await interaction.response.send_message(
                "Diese Auswertung ist abgelaufen — bitte den Befehl erneut ausführen.",
                ephemeral=True,
            )
            return False
        if interaction.user.id != entry[0]:
            await interaction.response.send_message(
                "Nur wer den Befehl ausgeführt hat, kann die Rohdaten öffnen.",
                ephemeral=True,
            )
            return False
        return True

    @discord.ui.button(
        label="Rohdaten (JSON)",
        style=discord.ButtonStyle.secondary,
        custom_id="osint:json",
    )
    async def export_json(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        """Send the collected payloads as a JSON file."""
        entry = self.cog._reports.get(
            interaction.message.id if interaction.message else 0
        )
        payload = entry[1] if entry else {}
        blob = json.dumps(payload, indent=2, ensure_ascii=False, default=str)
        await interaction.response.send_message(
            file=discord.File(
                io.BytesIO(blob.encode()), filename="osint-rohdaten.json"
            ),
            ephemeral=True,
        )

    @discord.ui.button(
        label="Quellen", style=discord.ButtonStyle.secondary, custom_id="osint:sources"
    )
    async def show_sources(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        """Show which external services this cog talks to."""
        await interaction.response.send_message(
            embeds=[self.cog.sources_embed()], ephemeral=True
        )


class OSINT(commands.Cog):
    """Passive OSINT lookups for domains, IPs, ASN, URLs and file metadata."""

    def __init__(self, bot: Red) -> None:
        self.bot = bot
        self.session: Optional[aiohttp.ClientSession] = None
        self._cache: Dict[str, Tuple[float, Tuple[Optional[int], Any, str]]] = {}
        self._last_call: Dict[str, float] = {}
        self._reports: Dict[int, Tuple[int, Dict[str, Any]]] = {}
        self._last_use: Dict[Tuple[int, int], float] = {}
        self._tor_cache: Optional[Tuple[float, frozenset]] = None
        self._sites: Dict[str, Site] = {}
        self._sites_loaded = 0.0
        self.config = Config.get_conf(
            self, identifier=72_194_508_314_66, force_registration=True
        )
        self.config.register_guild(
            enabled=True,
            channels=[],
            cooldown=15,
            log_channel=None,
            exempt_managers=True,
            user_sites=150,
            user_timeout=10,
            user_nsfw=True,
            user_misses=False,
        )

    async def cog_load(self) -> None:
        """Create the HTTP session and register the persistent button template."""
        self.session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=HTTP_TIMEOUT)
        )
        self.bot.add_view(ReportView(self, timeout=None))

    async def cog_unload(self) -> None:
        """Close the HTTP session."""
        if self.session is not None and not self.session.closed:
            await self.session.close()
        self.session = None

    async def red_delete_data_for_user(self, *, requester: str, user_id: int) -> None:
        """Only guild level configuration is stored, so there is nothing to delete."""
        return

    async def red_get_data_for_user(self, *, user_id: int) -> Dict[str, io.BytesIO]:
        """Only guild level configuration is stored, so there is nothing to hand out."""
        return {}

    # ------------------------------------------------------------------
    # Gate
    # ------------------------------------------------------------------

    async def gate(self, ctx: commands.Context) -> Tuple[bool, str]:
        """Decide whether this invocation may run.

        Returns ``(allowed, reason)``. A granted lookup consumes the cooldown,
        so this must be called exactly once per invocation — which is why the
        gate sits on the subcommands and not additionally on the group.
        """
        if ctx.guild is None:
            return False, "OSINT-Lookups funktionieren nur auf einem Server."
        conf = await self.config.guild(ctx.guild).all()
        if not conf["enabled"]:
            return False, "OSINT ist auf diesem Server deaktiviert."
        allowed = [int(channel) for channel in conf["channels"] or []]
        if allowed and ctx.channel.id not in allowed:
            names = humanize_list([f"<#{channel}>" for channel in allowed])
            return False, f"Lookups sind hier nicht erlaubt. Erlaubt: {names}."

        cooldown = int(conf["cooldown"] or 0)
        if cooldown <= 0:
            return True, ""
        if conf["exempt_managers"] and ctx.author.guild_permissions.manage_guild:
            return True, ""

        key = (ctx.guild.id, ctx.author.id)
        now = time.monotonic()
        remaining = cooldown - (now - self._last_use.get(key, 0.0))
        if remaining > 0:
            return False, f"Bitte noch {remaining:.0f}s warten bis zum nächsten Lookup."
        self._last_use[key] = now
        if len(self._last_use) > 2000:
            cutoff = now - max(cooldown, 60) * 4
            self._last_use = {k: v for k, v in self._last_use.items() if v >= cutoff}
        return True, ""

    # ------------------------------------------------------------------
    # HTTP layer
    # ------------------------------------------------------------------

    def _client(self) -> aiohttp.ClientSession:
        if self.session is None or self.session.closed:
            self.session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=HTTP_TIMEOUT)
            )
        return self.session

    @staticmethod
    def _cache_key(source: str, url: str, params: Any) -> str:
        if not params:
            return f"{source}|{url}"
        pairs = params.items() if isinstance(params, dict) else params
        flattened = "&".join(f"{key}={value}" for key, value in sorted(pairs))
        return f"{source}|{url}|{flattened}"

    async def _throttle(self, source: str) -> None:
        """Keep at least ``MIN_INTERVAL`` seconds between two calls to a source."""
        interval = MIN_INTERVAL.get(source, 0.0)
        if interval <= 0:
            return
        wait = interval - (time.monotonic() - self._last_call.get(source, 0.0))
        if wait > 0:
            await asyncio.sleep(wait)
        self._last_call[source] = time.monotonic()

    async def _fetch(
        self,
        source: str,
        url: str,
        *,
        params: Any = None,
        headers: Optional[Dict[str, str]] = None,
        ttl: Optional[int] = CACHE_SECONDS,
    ) -> Tuple[Optional[int], Any, str]:
        """GET a URL, returning ``(status, parsed_payload, text)``.

        Never raises: a dead source yields ``(None, None, "")`` so one failing
        provider cannot take a whole report down. Only 200 and 404 are cached —
        both are definitive answers, while a 429 or a 5xx must be retryable.
        """
        key = self._cache_key(source, url, params)
        cached = self._cache.get(key)
        if cached is not None:
            if cached[0] > time.monotonic():
                return cached[1]
            self._cache.pop(key, None)

        await self._throttle(source)
        request_headers = {
            "User-Agent": USER_AGENT,
            "Accept": "application/json, text/plain, */*",
        }
        if headers:
            request_headers.update(headers)

        status: Optional[int] = None
        raw = b""
        for attempt in range(2):
            last = attempt == 1
            try:
                async with self._client().get(
                    url, params=params, headers=request_headers
                ) as response:
                    status = response.status
                    raw = await response.read()
            except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
                if last:
                    log.debug("OSINT: request to %s failed: %s", source, exc)
                    return None, None, ""
                await asyncio.sleep(RETRY_DELAY)
                continue
            except Exception:
                log.exception("OSINT: unexpected error talking to %s", source)
                return None, None, ""
            if status in RETRY_STATUS and not last:
                log.debug("OSINT: %s answered HTTP %s, retrying once", source, status)
                await asyncio.sleep(RETRY_DELAY)
                continue
            break

        text = raw.decode("utf-8", "replace")
        payload: Any = None
        stripped = text.lstrip()
        if stripped[:1] in ("{", "["):
            try:
                payload = json.loads(text)
            except ValueError:
                payload = None
        if payload is None and text:
            payload = text

        result: Tuple[Optional[int], Any, str] = (status, payload, text)
        if ttl and status in (200, 404):
            if len(self._cache) > 600:
                self._prune_cache()
            self._cache[key] = (time.monotonic() + ttl, result)
        return result

    def _prune_cache(self) -> None:
        now = time.monotonic()
        for key in [key for key, entry in self._cache.items() if entry[0] <= now]:
            self._cache.pop(key, None)
        while len(self._cache) > 600:
            self._cache.pop(next(iter(self._cache)), None)

    @staticmethod
    async def gather_limited(coros: Sequence[Any], limit: int = 4) -> List[Any]:
        """Await coroutines with a concurrency cap; failures become ``None``."""
        semaphore = asyncio.Semaphore(max(1, limit))

        async def run(coro: Any) -> Any:
            async with semaphore:
                try:
                    return await coro
                except Exception:
                    log.exception("OSINT: source call failed")
                    return None

        return list(await asyncio.gather(*(run(coro) for coro in coros)))

    async def _doh(self, name: str, rtype: str) -> Tuple[Optional[int], Any]:
        """Resolve one record type, trying Google's resolver then Cloudflare's."""
        status, payload, _ = await self._fetch(
            "doh",
            DOH_GOOGLE,
            params={"name": name, "type": rtype},
            headers={"Accept": "application/dns-json"},
        )
        if status == 200 and isinstance(payload, dict):
            return status, payload
        status, payload, _ = await self._fetch(
            "doh",
            DOH_CLOUDFLARE,
            params={"name": name, "type": rtype},
            headers={"Accept": "application/dns-json"},
        )
        return status, payload

    async def _rdap(self, kind: str, target: str) -> Dict[str, Any]:
        """Fetch and flatten an RDAP record; ``{}`` when the registry has none."""
        status, payload, _ = await self._fetch(
            "rdap", RDAP.format(kind=kind, target=target)
        )
        if status != 200 or not isinstance(payload, dict):
            return {}
        return rdap_summary(payload)

    async def _tor_exits(self) -> frozenset:
        """Cached Tor exit node list (about 100 KB, refreshed every six hours)."""
        now = time.monotonic()
        if self._tor_cache is not None and self._tor_cache[0] > now:
            return self._tor_cache[1]
        status, _, text = await self._fetch("tor", TOR_EXIT_LIST, ttl=6 * 3600)
        if status != 200:
            return self._tor_cache[1] if self._tor_cache else frozenset()
        exits = frozenset(
            line.strip()
            for line in text.splitlines()
            if line.strip() and not line.startswith("#")
        )
        self._tor_cache = (now + 6 * 3600, exits)
        return exits

    async def _http_probe(self, url: str, *, max_hops: int = 3) -> Dict[str, Any]:
        """Follow a URL by hand, checking that every hop stays on a public host.

        aiohttp's own redirect handling would happily follow a chain into
        ``127.0.0.1``, so the chain is walked explicitly and each host is
        resolved and validated first.
        """
        hops: List[Dict[str, Any]] = []
        current = normalize_url(url)

        for _ in range(max_hops + 1):
            host = host_of(current)
            if not is_public_hostname(host):
                raise OSINTError(f"`{host}` ist kein öffentlicher Hostname.")
            if re.fullmatch(r"[0-9.]+", host) and not is_public_address(host):
                raise OSINTError(f"`{host}` ist keine öffentliche Adresse.")
            if not re.fullmatch(r"[0-9.]+", host):
                _, payload = await self._doh(host, "A")
                addresses = doh_answers(payload or {}, "A")
                if addresses and not any(
                    is_public_address(address) for address in addresses
                ):
                    raise OSINTError(f"`{host}` löst nur auf private Adressen auf.")

            try:
                async with self._client().get(
                    current,
                    headers={"User-Agent": USER_AGENT, "Accept": "*/*"},
                    allow_redirects=False,
                ) as response:
                    status = response.status
                    response_headers = lower_headers(response.headers)
                    body = await response.content.read(65536)
                    location = response.headers.get("Location")
            except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
                return {"hops": hops, "error": f"{type(exc).__name__}: {exc}"}

            hops.append({"url": current, "status": status, "headers": response_headers})
            if (
                status in (301, 302, 303, 307, 308)
                and location
                and len(hops) <= max_hops
            ):
                current = urljoin(current, location)
                continue
            hops[-1]["body"] = body.decode("utf-8", "replace")
            break

        return {"hops": hops}

    # ------------------------------------------------------------------
    # Commands
    # ------------------------------------------------------------------

    @commands.group(name="osint", aliases=["recon"], invoke_without_command=True)
    @commands.guild_only()
    async def osint(self, ctx: commands.Context) -> None:
        """Passive OSINT lookups: IPs, domains, ASN, URLs, MACs and file metadata.

        Every command only reads public sources — nothing is scanned or probed.
        """
        if ctx.invoked_subcommand is None:
            await ctx.send_help()

    @osint.command(name="lookup")
    @commands.guild_only()
    @gated()
    async def lookup(self, ctx: commands.Context, *, target: str) -> None:
        """Look a target up without naming its type.

        Recognises IP addresses, domains, URLs, `AS13335`, MAC addresses,
        email addresses (Gravatar) and SHA-256 style hashes.

        **Examples**

        * `[p]osint lookup 8.8.8.8`
        * `[p]osint lookup example.com`
        * `[p]osint lookup AS13335`
        """
        kind = classify_target(target)
        routes = {
            "ip": self.report_ip,
            "ipv6": self.report_ip,
            "domain": self.report_domain,
            "url": self.report_url,
            "asn": self.report_asn,
            "mac": self.report_mac,
            "email": self.report_gravatar,
        }
        builder = routes.get(kind)
        if builder is None:
            await ctx.send(
                "Damit kann ich nichts anfangen. Erwartet werden IP, Domain, URL, "
                "`AS<nummer>`, MAC oder E-Mail-Adresse."
            )
            return
        await self._run(ctx, f"lookup:{kind}", target, builder)

    @osint.command(name="ip")
    @commands.guild_only()
    @gated()
    async def ip(self, ctx: commands.Context, address: str) -> None:
        """Show geolocation, ports, network block and reputation for an IP.

        **Example:** `[p]osint ip 8.8.8.8`
        """
        await self._run(ctx, "ip", address, self.report_ip)

    @osint.command(name="domain", aliases=["dom"])
    @commands.guild_only()
    @gated()
    async def domain(self, ctx: commands.Context, name: str) -> None:
        """Show registration, DNS records, mail policy and HTTP headers.

        **Example:** `[p]osint domain example.com`
        """
        await self._run(ctx, "domain", name, self.report_domain)

    @osint.command(name="subs", aliases=["subdomains"])
    @commands.guild_only()
    @gated()
    async def subs(self, ctx: commands.Context, name: str) -> None:
        """Enumerate subdomains from certificate transparency logs.

        Long lists arrive as a text file.

        **Example:** `[p]osint subs example.com`
        """
        await self._run(ctx, "subs", name, self.report_subs)

    @osint.command(name="url")
    @commands.guild_only()
    @gated()
    async def url(self, ctx: commands.Context, *, target: str) -> None:
        """Check a URL: live headers, urlscan history and Wayback snapshots.

        **Example:** `[p]osint url https://example.com/login`
        """
        await self._run(ctx, "url", target, self.report_url)

    @osint.command(name="asn", aliases=["net"])
    @commands.guild_only()
    @gated()
    async def asn(self, ctx: commands.Context, target: str) -> None:
        """Show the network behind an AS number or an IP address.

        **Examples:** `[p]osint asn AS13335` · `[p]osint asn 8.8.8.8`
        """
        await self._run(ctx, "asn", target, self.report_asn)

    @osint.command(name="mac")
    @commands.guild_only()
    @gated()
    async def mac(self, ctx: commands.Context, address: str) -> None:
        """Look up the hardware vendor for a MAC address.

        **Example:** `[p]osint mac 00:1A:2B:3C:4D:5E`
        """
        await self._run(ctx, "mac", address, self.report_mac)

    @osint.command(name="gravatar", aliases=["mail"])
    @commands.guild_only()
    @gated()
    async def gravatar(self, ctx: commands.Context, address: str) -> None:
        """Show the public Gravatar profile of an email address.

        Only the MD5 hash of the address is sent to Gravatar, never the address
        itself — the lookup reveals nothing that Gravatar does not already
        publish.

        **Example:** `[p]osint gravatar someone@example.com`
        """
        await self._run(ctx, "gravatar", address, self.report_gravatar)

    @osint.command(name="exif")
    @commands.guild_only()
    @gated()
    async def exif(self, ctx: commands.Context, url: Optional[str] = None) -> None:
        """Read metadata out of an image, PDF or any other file.

        Works on an attachment of the message (or of the message being
        replied to) and on a direct URL. Shows EXIF including GPS, PNG text
        chunks, PDF author/producer fields, dimensions and file hashes.

        Note that Discord re-encodes uploaded images and drops their metadata,
        so for a photo use a URL to the original file.

        **Examples:** `[p]osint exif` (with an attachment) · `[p]osint exif https://…/photo.jpg`
        """
        await self._run_exif(ctx, url)

    @osint.command(name="user", aliases=["username"])
    @commands.guild_only()
    @gated()
    async def user(
        self, ctx: commands.Context, username: str, sites: Optional[str] = None
    ) -> None:
        """Check a username across ~480 sites (Sherlock site list).

        A **hit** is a profile where the site did not report its own "not found".
        Results that rest on the status code alone, with page text suggesting
        otherwise, are listed as **unsure** instead of being sold as found.

        Narrow the run with a comma-separated site list — useful when the bot's
        host is blocked by most sites.

        **Examples**

        * `[p]osint user torvalds`
        * `[p]osint user torvalds GitHub,Reddit,GitLab`
        """
        names = (
            [part.strip() for part in sites.split(",") if part.strip()]
            if sites
            else None
        )
        await self._run_user(ctx, username, names)

    @osint.command(name="sitelist", aliases=["sites"])
    @commands.guild_only()
    async def sitelist(
        self, ctx: commands.Context, *, search: Optional[str] = None
    ) -> None:
        """List the sites `[p]osint user` can check, optionally filtered.

        Long lists arrive as a text file.

        **Example:** `[p]osint sitelist git`
        """
        try:
            catalogue = await self.site_catalogue()
        except OSINTError as exc:
            await ctx.send(str(exc))
            return

        names = sorted(
            (
                name
                for name in catalogue
                if not search or search.lower() in name.lower()
            ),
            key=str.lower,
        )
        if not names:
            await ctx.send(f"Keine Site passt zu {inline(truncate(search or '', 40))}.")
            return
        listing = "\n".join(names)
        if len(listing) > 1500:
            await ctx.send(
                f"**{len(names)}** Sites im Datensatz.",
                file=text_to_file(listing, filename="osint-sites.txt"),
            )
            return
        await ctx.send(f"**{len(names)}** Sites:\n{box(listing, lang='yaml')}")

    @osint.command(name="sources")
    @commands.guild_only()
    async def sources(self, ctx: commands.Context) -> None:
        """List every external service this cog contacts, and its limits."""
        await ctx.send(embeds=[self.sources_embed()])

    @osint.command(name="status")
    @commands.guild_only()
    async def status(self, ctx: commands.Context) -> None:
        """Show the OSINT settings for this server."""
        conf = await self.config.guild(ctx.guild).all()
        channels = [int(channel) for channel in conf["channels"] or []]
        log_channel = conf["log_channel"]
        lines = [
            f"**Aktiv:** {'ja' if conf['enabled'] else 'nein'}",
            f"**Kanäle:** {humanize_list([f'<#{c}>' for c in channels]) if channels else 'überall'}",
            f"**Cooldown:** {conf['cooldown']}s pro Nutzer",
            f"**Manager ausgenommen:** {'ja' if conf['exempt_managers'] else 'nein'}",
            (
                f"**Log-Kanal:** <#{log_channel}>"
                if log_channel
                else "**Log-Kanal:** keiner"
            ),
            f"**Cache:** {len(self._cache)} Einträge, {CACHE_SECONDS // 60} Minuten TTL",
            f"**User-Suche:** {'alle' if not conf['user_sites'] else conf['user_sites']} Sites, "
            f"{conf['user_timeout']}s Timeout, NSFW-Filter "
            f"{'an' if conf['user_nsfw'] else 'aus'}",
        ]
        await ctx.send("\n".join(lines))

    # ------------------------------------------------------------------
    # Settings
    # ------------------------------------------------------------------

    @osint.group(name="set", invoke_without_command=True)
    @commands.guild_only()
    @commands.admin_or_permissions(manage_guild=True)
    async def set_group(self, ctx: commands.Context) -> None:
        """Configure OSINT for this server."""
        if ctx.invoked_subcommand is None:
            await ctx.send_help()

    @set_group.command(name="toggle")
    @commands.guild_only()
    @commands.admin_or_permissions(manage_guild=True)
    async def set_toggle(self, ctx: commands.Context) -> None:
        """Enable or disable all lookups on this server."""
        current = await self.config.guild(ctx.guild).enabled()
        await self.config.guild(ctx.guild).enabled.set(not current)
        await ctx.send(
            f"OSINT-Lookups sind jetzt {'**aktiv**' if not current else '**aus**'}."
        )

    @set_group.command(name="channels")
    @commands.guild_only()
    @commands.admin_or_permissions(manage_guild=True)
    async def set_channels(
        self,
        ctx: commands.Context,
        action: str,
        channel: Optional[discord.TextChannel] = None,
    ) -> None:
        """Restrict lookups to specific channels.

        `add`/`remove` take a channel, `clear` opens lookups up again and
        `list` shows the current selection.

        **Examples:** `[p]osint set channels add #osint` · `[p]osint set channels clear`
        """
        action = action.lower()
        if action not in {"add", "remove", "clear", "list"}:
            await ctx.send("Erlaubt sind `add`, `remove`, `clear` und `list`.")
            return

        if action == "list":
            channels = [
                int(c) for c in await self.config.guild(ctx.guild).channels() or []
            ]
            await ctx.send(
                humanize_list([f"<#{c}>" for c in channels])
                if channels
                else "Keine Einschränkung."
            )
            return

        async with self.config.guild(ctx.guild).channels() as channels:
            if action == "clear":
                channels.clear()
                message = "Lookups sind wieder in jedem Kanal möglich."
            elif channel is None:
                await ctx.send("Dafür brauche ich einen Kanal.")
                return
            else:
                changed = [int(c) for c in channels]
                if action == "add":
                    if channel.id in changed:
                        await ctx.send(f"{channel.mention} ist bereits eingetragen.")
                        return
                    changed.append(channel.id)
                    message = f"{channel.mention} hinzugefügt."
                else:
                    if channel.id not in changed:
                        await ctx.send(f"{channel.mention} steht nicht in der Liste.")
                        return
                    changed.remove(channel.id)
                    message = f"{channel.mention} entfernt."
                channels[:] = changed

        await ctx.send(message)

    @set_group.command(name="logchannel")
    @commands.guild_only()
    @commands.admin_or_permissions(manage_guild=True)
    async def set_logchannel(
        self, ctx: commands.Context, channel: Optional[discord.TextChannel] = None
    ) -> None:
        """Log every lookup to a channel, or without a channel stop logging."""
        if channel is None:
            await self.config.guild(ctx.guild).log_channel.set(None)
            await ctx.send("Audit-Log deaktiviert.")
            return
        await self.config.guild(ctx.guild).log_channel.set(channel.id)
        await ctx.send(f"Lookups werden jetzt in {channel.mention} protokolliert.")

    @set_group.command(name="cooldown")
    @commands.guild_only()
    @commands.admin_or_permissions(manage_guild=True)
    async def set_cooldown(self, ctx: commands.Context, seconds: int) -> None:
        """Set the per-user cooldown in seconds (`0` disables it).

        **Example:** `[p]osint set cooldown 30`
        """
        if seconds < 0 or seconds > 3600:
            await ctx.send("Der Cooldown muss zwischen 0 und 3600 Sekunden liegen.")
            return
        await self.config.guild(ctx.guild).cooldown.set(seconds)
        await ctx.send(
            "Cooldown aufgehoben."
            if seconds == 0
            else f"Cooldown auf {seconds}s gesetzt."
        )

    @set_group.command(name="exempt")
    @commands.guild_only()
    @commands.admin_or_permissions(manage_guild=True)
    async def set_exempt(self, ctx: commands.Context) -> None:
        """Toggle whether members with *Manage Server* skip the cooldown."""
        current = await self.config.guild(ctx.guild).exempt_managers()
        await self.config.guild(ctx.guild).exempt_managers.set(not current)
        await ctx.send(
            "Manager umgehen den Cooldown weiterhin."
            if current
            else "Manager unterliegen jetzt ebenfalls dem Cooldown."
        )

    @set_group.command(name="clearcache")
    @commands.guild_only()
    @commands.admin_or_permissions(manage_guild=True)
    async def set_clearcache(self, ctx: commands.Context) -> None:
        """Drop every cached provider response."""
        count = len(self._cache)
        self._cache.clear()
        self._tor_cache = None
        await ctx.send(f"{count} gecachte Antworten verworfen.")

    @set_group.command(name="usersites")
    @commands.guild_only()
    @commands.admin_or_permissions(manage_guild=True)
    async def set_usersites(self, ctx: commands.Context, count: int) -> None:
        """How many sites `[p]osint user` checks per lookup (`0` = all ~480).

        **Example:** `[p]osint set usersites 60`
        """
        if count < 0 or count > 600:
            await ctx.send("Erlaubt sind 0 (alle) bis 600.")
            return
        await self.config.guild(ctx.guild).user_sites.set(count)
        await ctx.send(
            f"`osint user` prüft jetzt "
            f"{'alle Sites' if count == 0 else str(count) + ' Sites'} pro Lookup."
        )

    @set_group.command(name="usertimeout")
    @commands.guild_only()
    @commands.admin_or_permissions(manage_guild=True)
    async def set_usertimeout(self, ctx: commands.Context, seconds: int) -> None:
        """Timeout per site for `[p]osint user` (3–30 seconds, default 10)."""
        if seconds < 3 or seconds > 30:
            await ctx.send("Erlaubt sind 3 bis 30 Sekunden.")
            return
        await self.config.guild(ctx.guild).user_timeout.set(seconds)
        await ctx.send(f"Timeout pro Site: **{seconds}s**.")

    @set_group.command(name="usermisses")
    @commands.guild_only()
    @commands.admin_or_permissions(manage_guild=True)
    async def set_usermisses(self, ctx: commands.Context) -> None:
        """Toggle whether `[p]osint user` also lists the sites without a profile."""
        current = await self.config.guild(ctx.guild).user_misses()
        await self.config.guild(ctx.guild).user_misses.set(not current)
        await ctx.send(
            "Sites ohne Profil werden jetzt mit ausgegeben."
            if not current
            else "Sites ohne Profil werden nicht mehr ausgegeben."
        )

    @set_group.command(name="usernsfw")
    @commands.guild_only()
    @commands.admin_or_permissions(manage_guild=True)
    async def set_usernsfw(self, ctx: commands.Context) -> None:
        """Toggle whether `[p]osint user` skips NSFW sites (19 in the dataset).

        With the filter on, the ~19 NSFW entries are only checked in channels
        Discord marks as age-restricted.
        """
        current = await self.config.guild(ctx.guild).user_nsfw()
        await self.config.guild(ctx.guild).user_nsfw.set(not current)
        await ctx.send(
            "NSFW-Sites werden gefiltert (nur in NSFW-Kanälen geprüft)."
            if not current
            else "NSFW-Sites werden immer mitgeprüft."
        )

    @set_group.command(name="userdataset")
    @commands.guild_only()
    @commands.admin_or_permissions(manage_guild=True)
    async def set_userdataset(
        self, ctx: commands.Context, action: Optional[str] = None
    ) -> None:
        """Show the state of the site list, or reload it with `refresh`."""
        if action and action.lower() == "refresh":
            async with ctx.typing():
                try:
                    catalogue = await self.site_catalogue(force=True)
                except OSINTError as exc:
                    await ctx.send(str(exc))
                    return
            await ctx.send(f"Site-Liste neu geladen: **{len(catalogue)}** Sites.")
            return

        path = self._site_cache_path()
        age = (
            f"{int((time.monotonic() - self._sites_loaded) / 60)} Minuten"
            if self._sites_loaded
            else "noch nicht geladen"
        )
        await ctx.send(
            f"**Sites im Speicher:** {len(self._sites)} ({age})\n"
            f"**Quelle:** {USERNAME_DATA_URL}\n"
            f"**Datei:** {inline(str(path)) if path else 'keine (nur im Speicher)'}\n"
            f"**Gültigkeit:** {DATA_MAX_AGE // 86400} Tage\n"
            f"**Erneuern:** `[p]osint set userdataset refresh`"
        )

    # ------------------------------------------------------------------
    # Lookup plumbing
    # ------------------------------------------------------------------

    async def _run(
        self,
        ctx: commands.Context,
        module: str,
        target: str,
        builder: Any,
    ) -> None:
        """Build a report, send it, and keep its payload for the JSON button."""
        await self._audit(ctx, module, target)
        try:
            async with ctx.typing():
                report = await builder(target)
        except OSINTError as exc:
            await ctx.send(str(exc))
            return
        except asyncio.TimeoutError:
            await ctx.send(
                "Die Quelle hat zu lange nicht geantwortet — bitte später erneut."
            )
            return
        except Exception:
            log.exception("OSINT: %s lookup for %r failed", module, target)
            await ctx.send("Der Lookup ist fehlgeschlagen. Details stehen im Bot-Log.")
            return

        if not report.embeds:
            await ctx.send("Keine Daten gefunden.")
            return

        view = ReportView(self)
        try:
            message = await ctx.send(
                embeds=report.embeds[:10], files=report.files or None, view=view
            )
        except discord.HTTPException:
            log.exception("OSINT: could not deliver the report for %r", target)
            return
        self._remember(message.id, ctx.author.id, report.data)

    def _remember(
        self, message_id: int, author_id: int, payload: Dict[str, Any]
    ) -> None:
        """Keep a copy of the raw payloads so the JSON button can hand them out."""
        self._reports[message_id] = (author_id, dict(payload))
        while len(self._reports) > MAX_REPORTS_KEPT:
            self._reports.pop(next(iter(self._reports)), None)

    async def _audit(self, ctx: commands.Context, module: str, target: str) -> None:
        """Write the lookup to the configured log channel; never raise."""
        log_channel_id = await self.config.guild(ctx.guild).log_channel()
        if not log_channel_id:
            return
        channel = ctx.guild.get_channel(int(log_channel_id))
        if channel is None:
            return
        try:
            await channel.send(
                f"`{module}` · {ctx.author.mention} (`{ctx.author.id}`) → `{truncate(target, 200)}`"
            )
        except discord.HTTPException:
            log.debug("OSINT: audit log message to %s failed", log_channel_id)

    # ------------------------------------------------------------------
    # Reports
    # ------------------------------------------------------------------

    async def report_ip(self, target: str) -> Report:
        """Geolocation, open ports, network block and reputation for one IP."""
        address = target.strip()
        if not is_public_address(address):
            raise OSINTError(
                "Nur öffentlich erreichbare IP-Adressen — private, Loopback-, "
                "Multicast- und reservierte Bereiche bleiben außen vor."
            )

        results = await self.gather_limited(
            [
                self._fetch(
                    "ip-api",
                    IP_API.format(target=address),
                    params={"fields": IP_API_FIELDS},
                ),
                self._fetch("internetdb", INTERNETDB.format(target=address)),
                self._rdap("ip", address),
                self._fetch(
                    "stopforumspam", STOPFORUMSPAM, params={"ip": address, "json": ""}
                ),
            ],
            limit=4,
        )
        (geo_status, geo, _), (db_status, db, _), rdap_entry, (sfs_status, sfs, _) = (
            results
        )
        rdap = rdap_entry or {}
        exits = await self._tor_exits()

        geo = geo if isinstance(geo, dict) else {}
        db = db if isinstance(db, dict) else {}
        sfs = sfs if isinstance(sfs, dict) else {}
        if geo_status == 200 and geo.get("status") == "fail":
            geo = {}

        embed = discord.Embed(
            title=f"IP {address}",
            colour=await self._colour(),
            timestamp=discord.utils.utcnow(),
        )
        summary = []
        if geo.get("as"):
            summary.append(f"**{geo['as']}**")
        elif geo.get("org"):
            summary.append(f"**{geo['org']}**")
        if geo.get("isp") and geo.get("isp") != geo.get("org"):
            summary.append(f"ISP: {geo['isp']}")
        if rdap.get("range"):
            summary.append(f"Bereich: `{rdap['range']}`")
        if summary:
            embed.description = "\n".join(summary)

        location = ", ".join(
            part
            for part in (
                geo.get("city"),
                geo.get("regionName"),
                geo.get("country"),
                geo.get("zip"),
            )
            if part
        )
        if location:
            coordinates = ""
            if geo.get("lat") is not None and geo.get("lon") is not None:
                links = gps_links(float(geo["lat"]), float(geo["lon"]))
                coordinates = f"\n[{geo['lat']}, {geo['lon']}]({links['osm']})"
            embed.add_field(
                name="Standort",
                value=truncate(location + coordinates, 1020),
                inline=True,
            )
        if geo.get("timezone"):
            embed.add_field(name="Zeitzone", value=geo["timezone"], inline=True)
        if geo.get("reverse"):
            embed.add_field(
                name="Reverse DNS", value=inline(geo["reverse"]), inline=True
            )
        if rdap.get("handle") or rdap.get("cidrs"):
            block = "\n".join(
                filter(
                    None,
                    [
                        (
                            f"Registry: {inline(rdap['handle'])}"
                            if rdap.get("handle")
                            else ""
                        ),
                        (
                            f"Netz: {inline(', '.join(rdap['cidrs']))}"
                            if rdap.get("cidrs")
                            else ""
                        ),
                        f"Land: {rdap['country']}" if rdap.get("country") else "",
                    ],
                )
            )
            embed.add_field(
                name="Netzblock (RDAP)", value=truncate(block, 1020), inline=True
            )

        ports = db.get("ports") or []
        if ports:
            shown = ", ".join(f"`{port}`" for port in sorted(ports)[:MAX_INLINE_LIST])
            embed.add_field(
                name=f"Offene Ports ({len(ports)})",
                value=truncate(shown, 1020),
                inline=False,
            )
        hostnames = db.get("hostnames") or []
        if hostnames:
            embed.add_field(
                name=f"Beobachtete Hostnames ({len(hostnames)})",
                value=truncate(", ".join(f"`{name}`" for name in hostnames[:8]), 1020),
                inline=False,
            )
        if db.get("vulns"):
            embed.add_field(
                name=f"CVEs ({len(db['vulns'])})",
                value=truncate(", ".join(f"`{cve}`" for cve in db["vulns"][:15]), 1020),
                inline=False,
            )

        flags = [name for name in ("proxy", "hosting", "mobile") if geo.get(name)]
        reputation = []
        if address in exits:
            reputation.append("**Tor-Exit-Node**")
        if flags:
            reputation.append("Flags: " + ", ".join(flags))
        reports = (sfs.get("ip") or {}) if isinstance(sfs, dict) else {}
        if sfs_status == 200 and reports:
            reputation.append(
                f"StopForumSpam: {reports.get('frequency', 0)} Meldungen, "
                f"aktiv: {'ja' if reports.get('appears') else 'nein'}"
            )
        if reputation:
            embed.add_field(
                name="Reputation", value="\n".join(reputation), inline=False
            )

        embed.add_field(
            name="Weiter", value=self._link_block(address, "ip"), inline=False
        )
        embed.set_footer(text="Nur öffentliche, passive Quellen · kein Scan")

        return Report(
            embeds=[embed],
            data={
                "target": address,
                "ip-api": geo,
                "shodan-internetdb": db,
                "rdap": rdap,
                "stopforumspam": sfs,
                "tor_exit": address in exits,
            },
        )

    async def report_domain(self, target: str) -> Report:
        """Registration, DNS, mail policy and HTTP headers for a domain."""
        name = host_of(target).lower()
        if not is_public_hostname(name):
            raise OSINTError(f"`{name or target}` ist keine auswertbare Domain.")

        record_types = ("A", "AAAA", "MX", "NS", "TXT", "CAA", "SOA")
        results = await self.gather_limited(
            [self._rdap("domain", name)]
            + [self._doh(name, rtype) for rtype in record_types]
            + [self._doh(f"_dmarc.{name}", "TXT")],
            limit=5,
        )
        rdap = results[0] or {}
        records: Dict[str, Any] = {}
        for index, rtype in enumerate(record_types, start=1):
            entry = results[index] if len(results) > index else None
            if entry:
                records[rtype] = entry[1]
        dmarc_entry = results[8] if len(results) > 8 else None
        dmarc_answers = doh_answers(
            (dmarc_entry[1] if dmarc_entry else None) or {}, "TXT"
        )

        answers = {
            rtype: doh_answers(payload or {}, rtype)
            for rtype, payload in records.items()
        }
        soa = doh_authority(records.get("SOA") or {}, "SOA") or answers.get("SOA", [])
        statuses = {
            rtype: dns_status(payload or {})
            for rtype, payload in records.items()
            if payload
        }

        try:
            probe = await self._http_probe(f"https://{name}")
        except OSINTError as exc:
            probe = {"error": str(exc)}

        embed = discord.Embed(
            title=f"Domain {name}",
            colour=await self._colour(),
            timestamp=discord.utils.utcnow(),
        )

        registrar = pick_entity(rdap.get("entities") or [], "registrar", "sponsor")
        if rdap.get("events") or registrar:
            events = rdap.get("events") or {}
            lines = [
                f"Registrar: {inline(registrar)}" if registrar else "",
                f"Registriert: {events.get('registration', '?')}",
                f"Läuft ab: {events.get('expiration', 'unbekannt')}",
                f"Letzte Änderung: {events.get('last changed', '?')}",
            ]
            if rdap.get("status"):
                lines.append(
                    "Status: " + ", ".join(f"`{s}`" for s in rdap["status"][:6])
                )
            embed.add_field(
                name="Registrierung (RDAP)",
                value=truncate("\n".join(filter(None, lines)), 1020),
                inline=False,
            )
        else:
            embed.add_field(
                name="Registrierung (RDAP)",
                value="Keine RDAP-Daten — bei Nicht-`gTLD`s oder nicht erreichbarem "
                "Registry-Server normal.",
                inline=False,
            )

        if rdap.get("nameservers"):
            embed.add_field(
                name=f"Nameserver ({len(rdap['nameservers'])})",
                value=truncate(
                    ", ".join(f"`{ns}`" for ns in rdap["nameservers"][:8]), 1020
                ),
                inline=False,
            )
        for rtype in ("A", "AAAA", "MX"):
            values = answers.get(rtype) or []
            if values:
                embed.add_field(
                    name=f"{rtype} ({len(values)})",
                    value=truncate(
                        "\n".join(f"`{value}`" for value in values[:12]), 1020
                    ),
                    inline=False,
                )

        spf = parse_spf(answers.get("TXT") or [])
        dmarc = parse_dmarc(dmarc_answers)
        mail_lines = []
        if spf:
            mail_lines.append(
                f"**SPF:** {spf['strength']} · {spf['lookups']} DNS-Lookups"
            )
            mail_lines.extend(f"  - {note}" for note in spf["notes"])
        else:
            mail_lines.append("**SPF:** fehlt")
        if dmarc:
            mail_lines.append(f"**DMARC:** {dmarc['strength']} · pct={dmarc['pct']}")
            mail_lines.extend(f"  - {note}" for note in dmarc["notes"])
        else:
            mail_lines.append("**DMARC:** fehlt")
        embed.add_field(
            name="Mail-Policy",
            value=truncate("\n".join(mail_lines), 1020),
            inline=False,
        )

        hops = probe.get("hops") or []
        if hops:
            final = hops[-1]
            grade, checks = grade_security_headers(final["headers"])
            missing = [label for label, present in checks if not present]
            lines = [
                f"HTTP {final['status']} nach {len(hops)} Hop(s) — Note **{grade}**"
            ]
            fingerprint = exposed_fingerprint(final["headers"])
            if fingerprint:
                lines.extend(f"`{line}`" for line in fingerprint[:4])
            if missing:
                lines.append("Fehlt: " + ", ".join(missing))
            title = _TITLE_RE.search(final.get("body", ""))
            if title:
                embed.add_field(
                    name="Seitentitel",
                    value=truncate(
                        _TAG_RE.sub("", title.group(1)).strip() or "(leer)", 200
                    ),
                    inline=False,
                )
            embed.add_field(
                name="HTTP-Header", value=truncate("\n".join(lines), 1020), inline=False
            )
        elif probe.get("error"):
            embed.add_field(
                name="HTTP",
                value=f"Keine Antwort: {truncate(probe['error'], 200)}",
                inline=False,
            )

        embed.add_field(
            name="Weiter",
            value=f"{self._link_block(name, 'domain')}\n"
            f"Subdomains: `[p]osint subs {name}`",
            inline=False,
        )
        embed.set_footer(text="Nur öffentliche, passive Quellen · kein Scan")

        embeds = [embed]
        txt = answers.get("TXT") or []
        if txt:
            listing = "\n".join(
                f"{rtype:<5} {value}" for rtype, value in sorted(statuses.items())
            )
            listing += "\n\nTXT:\n" + "\n".join(f"  {value}" for value in txt)
            if soa:
                listing += "\n\nSOA:\n  " + "\n  ".join(soa)
            embeds.append(
                discord.Embed(
                    title=f"DNS-Details {name}",
                    description=box(truncate(listing, 3900), lang="yaml"),
                    colour=await self._colour(),
                )
            )

        return Report(
            embeds=embeds,
            data={
                "target": name,
                "rdap": rdap,
                "dns": answers,
                "status": statuses,
                "spf": spf,
                "dmarc": dmarc,
                "http": [
                    {
                        "url": hop["url"],
                        "status": hop["status"],
                        "headers": hop["headers"],
                    }
                    for hop in hops
                ],
            },
        )

    async def report_subs(self, target: str) -> Report:
        """Subdomains from certificate transparency logs."""
        name = host_of(target).lower()
        if not is_public_hostname(name):
            raise OSINTError(f"`{name or target}` ist keine auswertbare Domain.")

        found: set = set()
        used: List[str] = []

        status, payload, _ = await self._fetch(
            "certspotter",
            CERTSPOTTER,
            params=[
                ("domain", name),
                ("include_subdomains", "true"),
                ("expand", "dns_names"),
                ("expand", "issuer"),
            ],
        )
        if status == 200 and isinstance(payload, list):
            for issuance in payload:
                if isinstance(issuance, dict):
                    for entry in issuance.get("dns_names") or []:
                        found.add(str(entry))
            if found:
                used.append(f"certspotter ({len(payload)} Zertifikate)")

        if not found:
            status, payload, _ = await self._fetch(
                "crt.sh", CRT_SH, params={"q": f"%.{name}", "output": "json"}
            )
            if status == 200 and isinstance(payload, list):
                for entry in payload:
                    if isinstance(entry, dict):
                        for value in str(entry.get("name_value", "")).splitlines():
                            found.add(value.strip())
                if found:
                    used.append(f"crt.sh ({len(payload)} Einträge)")
            elif status == 502:
                used.append("crt.sh: 502 (bekannt unzuverlässig)")

        if not found:
            status, payload, text = await self._fetch(
                "hackertarget", HACKERTARGET_HOSTSEARCH, params={"q": name}
            )
            if status == 200 and "," in text:
                for line in text.splitlines():
                    if "," in line:
                        found.add(line.split(",", 1)[0].strip())
                if found:
                    used.append("hackertarget (Tageslimit 100 beachten)")

        cleaned = sorted(
            {
                entry.lower().rstrip(".").lstrip("*.")
                for entry in found
                if entry and entry.strip()
            }
        )
        cleaned = [
            entry for entry in cleaned if entry == name or entry.endswith("." + name)
        ]

        embed = discord.Embed(
            title=f"Subdomains · {name}",
            colour=await self._colour(),
            timestamp=discord.utils.utcnow(),
        )
        if not cleaned:
            embed.description = (
                "Keine Subdomains gefunden. Das heißt nicht, dass es keine gibt — "
                "nur, dass in den Zertifikatslogs keine auftauchen."
            )
            if used:
                embed.add_field(name="Quellen", value="\n".join(used), inline=False)
            return Report(
                embeds=[embed], data={"target": name, "subdomains": [], "sources": used}
            )

        embed.description = (
            f"**{len(cleaned)}** {plural(len(cleaned), 'Name', 'Namen')} aus "
            f"{humanize_list(used) if used else 'den Zertifikatslogs'}."
        )
        inline_list = cleaned[:MAX_INLINE_LIST]
        embed.add_field(
            name="Auszug" if len(cleaned) > MAX_INLINE_LIST else "Gefunden",
            value=box(truncate("\n".join(inline_list), 1000), lang="yaml"),
            inline=False,
        )

        files: List[discord.File] = []
        if len(cleaned) > MAX_INLINE_LIST:
            files.append(
                text_to_file("\n".join(cleaned), filename=f"subdomains-{name}.txt")
            )
            embed.add_field(
                name="Vollständige Liste",
                value=f"{len(cleaned) - MAX_INLINE_LIST} weitere Namen in der Textdatei.",
                inline=False,
            )
        embed.set_footer(text="Passiv aus Zertifikatslogs · keine Auflösung der Namen")

        return Report(
            embeds=[embed],
            files=files,
            data={"target": name, "subdomains": cleaned, "sources": used},
        )

    async def report_url(self, target: str) -> Report:
        """Live headers, urlscan history and Wayback snapshots for a URL."""
        address = normalize_url(target)
        host = host_of(address)
        if not is_public_hostname(host) and not is_public_address(host):
            raise OSINTError(f"`{host or target}` ist kein öffentlicher Host.")

        probe, scan_entry, cdx_entry = await asyncio.gather(
            self._http_probe(address),
            self._fetch(
                "urlscan", URLSCAN_SEARCH, params={"q": f"domain:{host}", "size": "10"}
            ),
            self._fetch(
                "wayback",
                WAYBACK_CDX,
                params={
                    "url": address,
                    "output": "json",
                    "limit": "20",
                    # One row per day; `collapse=urlkey` makes the CDX server
                    # drop the connection entirely.
                    "collapse": "timestamp:8",
                },
            ),
        )
        scan_status = scan_entry[0] if scan_entry else None
        scan_payload = scan_entry[1] if scan_entry else None
        cdx_status = cdx_entry[0] if cdx_entry else None
        cdx_value = cdx_entry[1] if cdx_entry else None

        embed = discord.Embed(
            title=f"URL {truncate(address, 100)}",
            colour=await self._colour(),
            timestamp=discord.utils.utcnow(),
        )

        hops = probe.get("hops") or []
        if hops:
            final = hops[-1]
            grade, checks = grade_security_headers(final["headers"])
            lines = [f"HTTP {final['status']} · Note **{grade}**"]
            if len(hops) > 1:
                lines.append("Kette: " + " → ".join(str(hop["status"]) for hop in hops))
            fingerprint = exposed_fingerprint(final["headers"])
            lines.extend(f"`{line}`" for line in fingerprint[:3])
            missing = [label for label, present in checks if not present]
            if missing:
                lines.append("Fehlt: " + ", ".join(missing))
            title = _TITLE_RE.search(final.get("body", ""))
            if title:
                lines.append(f"Titel: {_TAG_RE.sub('', title.group(1)).strip()[:120]}")
            embed.add_field(
                name="Live", value=truncate("\n".join(lines), 1020), inline=False
            )
        elif probe.get("error"):
            embed.add_field(
                name="Live",
                value=f"Keine Antwort: {truncate(probe['error'], 180)}",
                inline=False,
            )

        results = (
            scan_payload.get("results") if isinstance(scan_payload, dict) else None
        )
        if results:
            lines = []
            for entry in results[:5]:
                page = entry.get("page") or {}
                task = entry.get("task") or {}
                when = str(task.get("time", ""))[:10]
                detail = " · ".join(
                    part
                    for part in (
                        page.get("server"),
                        page.get("ip"),
                        page.get("country"),
                    )
                    if part
                )
                lines.append(
                    f"[{truncate(str(page.get('url', task.get('url', '?'))), 70)}]"
                    f"(https://urlscan.io/result/{entry.get('_id')}/) — {when}"
                    + (f" · {detail}" if detail else "")
                )
            embed.add_field(
                name=f"urlscan.io ({len(results)} Treffer)",
                value=truncate("\n".join(lines), 1020),
            )
        elif scan_status == 200:
            embed.add_field(
                name="urlscan.io",
                value="Keine öffentlichen Scans zu diesem Host.",
                inline=False,
            )
        else:
            embed.add_field(
                name="urlscan.io",
                value=f"Suche fehlgeschlagen (HTTP {scan_status}) — später erneut versuchen.",
                inline=False,
            )

        snapshots = _parse_cdx(cdx_value)
        if snapshots:
            first, last = snapshots[0], snapshots[-1]
            lines = [
                f"{len(snapshots)} Snapshots (je Tag einer)",
                f"Ältester: {first[0]} · `{first[1]}`",
                f"Neuester: {last[0]}",
                f"[Im Web Archive öffnen](https://web.archive.org/web/{last[0]}/{address})",
            ]
            embed.add_field(
                name="Wayback Machine", value="\n".join(lines), inline=False
            )
        elif cdx_status == 200:
            embed.add_field(
                name="Wayback Machine",
                value="Keine archivierten Snapshots gefunden.",
                inline=False,
            )
        else:
            embed.add_field(
                name="Wayback Machine",
                value=f"Das CDX-Archiv hat nicht geantwortet (HTTP {cdx_status}) — "
                "die Schnittstelle ist zeitweise überlastet.",
                inline=False,
            )

        embed.add_field(
            name="Weiter", value=self._link_block(host, "domain"), inline=False
        )
        embed.set_footer(text="Passiv: Kopfzeilen, urlscan-Suche, Archiv")

        return Report(
            embeds=[embed],
            data={
                "target": address,
                "probe": {
                    "hops": [
                        {
                            "url": hop["url"],
                            "status": hop["status"],
                            "headers": hop["headers"],
                        }
                        for hop in hops
                    ],
                    "error": probe.get("error"),
                },
                "urlscan": results or [],
                "urlscan_status": scan_status,
                "wayback": snapshots,
                "wayback_status": cdx_status,
            },
        )

    async def report_asn(self, target: str) -> Report:
        """Routing data for an AS number or the network behind an IP."""
        value = target.strip().upper()
        prefix = ""
        if value.startswith("AS"):
            resource = value
        elif is_public_address(target.strip()):
            status, payload, _ = await self._fetch(
                "ripestat",
                RIPESTAT.format(call="network-info"),
                params={"resource": target.strip(), "sourceapp": "freak-cogs-osint"},
            )
            data = (payload or {}).get("data") or {}
            prefixes = data.get("prefixes") or []
            asns = data.get("asns") or []
            if not asns:
                raise OSINTError(f"Zu `{target}` liefert RIPEstat keine AS-Zuordnung.")
            resource = f"AS{asns[0]}"
            prefix = prefixes[0] if prefixes else ""
        elif value.isdigit():
            resource = f"AS{value}"
        else:
            raise OSINTError(
                "Erwartet wird `AS<nummer>` oder eine öffentliche IP-Adresse."
            )

        overview, announced = await asyncio.gather(
            self._fetch(
                "ripestat",
                RIPESTAT.format(call="as-overview"),
                params={"resource": resource, "sourceapp": "freak-cogs-osint"},
            ),
            self._fetch(
                "ripestat",
                RIPESTAT.format(call="announced-prefixes"),
                params={"resource": resource, "sourceapp": "freak-cogs-osint"},
            ),
        )
        overview_data = (
            ((overview[1] or {}).get("data") or {})
            if isinstance(overview[1], dict)
            else {}
        )
        announced_data = (
            ((announced[1] or {}).get("data") or {})
            if isinstance(announced[1], dict)
            else {}
        )
        prefixes = announced_data.get("prefixes") or []

        embed = discord.Embed(
            title=f"Netzwerk {resource}",
            description=prefix and f"Ausgangspunkt: `{prefix}`",
            colour=await self._colour(),
            timestamp=discord.utils.utcnow(),
        )
        embed.add_field(
            name="Halte",
            value=overview_data.get("holder") or "unbekannt",
            inline=False,
        )
        embed.add_field(
            name="Angekündigt",
            value="ja" if overview_data.get("announced") else "nein",
            inline=True,
        )
        block = overview_data.get("block")
        block_label = (
            str(block.get("resource", "-"))
            if isinstance(block, dict)
            else str(block or "-")
        )
        embed.add_field(name="Block", value=inline(block_label), inline=True)
        embed.add_field(
            name="RIPEstat",
            value=f"[{resource}](https://stat.ripe.net/{resource}) · "
            f"[bgp.he.net](https://bgp.he.net/{resource}) · "
            f"[Shodan](https://www.shodan.io/search?query=net%3A{prefix or resource})",
            inline=False,
        )
        if prefixes:
            sample = "\n".join(f"`{entry.get('prefix')}`" for entry in prefixes[:12])
            embed.add_field(
                name=f"Angekündigte Präfixe ({len(prefixes)})",
                value=truncate(sample, 1020),
                inline=False,
            )

        return Report(
            embeds=[embed],
            data={
                "target": resource,
                "as-overview": overview_data,
                "announced_total": len(prefixes),
                "announced-prefixes": prefixes[:50],
                "start_prefix": prefix,
            },
        )

    async def report_mac(self, target: str) -> Report:
        """Vendor lookup for a MAC address."""
        details = mac_details(target)
        if not details:
            raise OSINTError("Das ist keine gültige MAC-Adresse.")

        status, payload, _ = await self._fetch(
            "macvendors", MACVENDORS.format(target=details["oui"])
        )
        vendor = str(payload) if status == 200 and payload else ""

        embed = discord.Embed(
            title=f"MAC {details['display']}",
            colour=await self._colour(),
            timestamp=discord.utils.utcnow(),
        )
        embed.add_field(
            name="Hersteller", value=vendor or "unbekannt (nicht in der OUI-Liste)"
        )
        embed.add_field(name="OUI", value=inline(details["oui"]), inline=True)
        embed.add_field(name="Geräteteil", value=inline(details["nic"]), inline=True)

        flags = []
        if details["locally_administered"]:
            flags.append(
                "**lokal verwaltet** — die Adresse wurde per Software gesetzt "
                "(üblich bei Zufalls-MACs und virtuellen Geräten)"
            )
        if details["multicast"]:
            flags.append("**Multicast-Bit gesetzt** — normalerweise kein echtes Gerät")
        if not flags:
            flags.append("Global eindeutig (herstellerspezifisch)")
        embed.add_field(name="Einordnung", value="\n".join(flags), inline=False)

        return Report(
            embeds=[embed],
            data={"target": details["display"], "vendor": vendor, **details},
        )

    async def report_gravatar(self, target: str) -> Report:
        """Public Gravatar profile for an email address."""
        address = target.strip().strip("<>").lower()
        if "@" not in address or classify_target(address) != "email":
            raise OSINTError("Das sieht nicht nach einer E-Mail-Adresse aus.")

        digest = md5(
            address.encode()
        ).hexdigest()  # noqa: S324 - the Gravatar API requires MD5
        status, payload, _ = await self._fetch(
            "gravatar", GRAVATAR.format(digest=digest)
        )

        embed = discord.Embed(
            title=f"Gravatar · {digest[:12]}…",
            colour=await self._colour(),
            timestamp=discord.utils.utcnow(),
        )
        if status != 200 or not isinstance(payload, dict) or "entry" not in payload:
            embed.description = (
                "Kein öffentliches Gravatar-Profil zu dieser Adresse. "
                "Übertragen wurde nur der MD5-Hash, nie die Adresse selbst."
            )
            return Report(embeds=[embed], data={"target": digest, "profile": None})

        profile = (payload.get("entry") or [{}])[0]
        embed.description = (
            f"[{profile.get('profileUrl', 'Profil')}]({profile.get('profileUrl', '')})"
        )
        if profile.get("thumbnailUrl"):
            embed.set_thumbnail(url=profile["thumbnailUrl"])
        embed.add_field(
            name="Name",
            value=truncate(str(profile.get("displayName") or "-"), 200),
            inline=True,
        )
        embed.add_field(
            name="Benutzername",
            value=truncate(str(profile.get("preferredUsername") or "-"), 200),
            inline=True,
        )
        if profile.get("aboutMe"):
            embed.add_field(
                name="Über mich",
                value=truncate(_clean_text(str(profile["aboutMe"])), 1020),
                inline=False,
            )
        accounts = profile.get("accounts") or []
        if accounts:
            lines = []
            for account in accounts[:10]:
                name = account.get("username") or account.get("name") or "?"
                link = account.get("url") or ""
                domain = account.get("domain") or account.get("shortname") or ""
                lines.append(
                    f"[{name}]({link}) — {domain}" if link else f"{name} — {domain}"
                )
            embed.add_field(
                name=f"Verknüpfte Konten ({len(accounts)})",
                value=truncate("\n".join(lines), 1020),
                inline=False,
            )
        urls = profile.get("urls") or []
        if urls:
            lines = [
                f"[{item.get('title') or item.get('value')}]({item.get('value')})"
                for item in urls[:6]
                if item.get("value")
            ]
            if lines:
                embed.add_field(
                    name="Links", value=truncate("\n".join(lines), 1020), inline=False
                )
        embed.set_footer(
            text="Öffentliches Gravatar-Profil · übertragen wird nur der MD5-Hash"
        )

        return Report(embeds=[embed], data={"target": digest, "profile": payload})

    async def _run_exif(self, ctx: commands.Context, url: Optional[str]) -> None:
        """Attachment/URL metadata report — kept apart because it needs ctx."""
        await self._audit(ctx, "exif", url or "anhang")
        filename = ""
        data: Optional[bytes] = None

        try:
            if url:
                data, filename = await self._download(url)
            else:
                attachment = None
                for candidate in ctx.message.attachments:
                    attachment = candidate
                    break
                if attachment is None and ctx.message.reference is not None:
                    referenced = ctx.message.reference.resolved
                    if (
                        isinstance(referenced, discord.Message)
                        and referenced.attachments
                    ):
                        attachment = referenced.attachments[0]
                if attachment is None:
                    await ctx.send(
                        "Häng eine Datei an die Nachricht an (oder antworte auf eine mit "
                        "Anhang) oder gib eine direkte URL an."
                    )
                    return
                if attachment.size > MAX_FILE_BYTES:
                    await ctx.send(
                        f"Die Datei ist mit {attachment.size} Bytes zu groß "
                        f"(Grenze {MAX_FILE_BYTES // (1024 * 1024)} MB)."
                    )
                    return
                data = await attachment.read()
                filename = attachment.filename
        except OSINTError as exc:
            await ctx.send(str(exc))
            return
        except (aiohttp.ClientError, asyncio.TimeoutError, discord.HTTPException):
            await ctx.send("Die Datei konnte nicht geladen werden.")
            return

        if not data:
            await ctx.send("Die Datei ist leer.")
            return

        report = self.build_file_report(data, filename)
        view = ReportView(self)
        message = await ctx.send(
            embeds=report.embeds[:10], files=report.files, view=view
        )
        self._remember(message.id, ctx.author.id, report.data)

    async def _run_user(
        self, ctx: commands.Context, username: str, names: Optional[Sequence[str]]
    ) -> None:
        """`[p]osint user` — a live progress bar, then the report in its place."""
        await self._audit(ctx, "user", username)
        conf = await self.config.guild(ctx.guild).all()

        # Naming sites explicitly means the run is small on purpose, so the
        # per-guild cap does not apply to it.
        limit = 0 if names else int(conf["user_sites"] or 0)
        allow_nsfw = bool(getattr(ctx.channel, "nsfw", False)) or not conf["user_nsfw"]

        title = f"Benutzername · {username}"
        try:
            message = await ctx.send(
                embed=discord.Embed(
                    title=title,
                    description="Site-Liste wird geladen …",
                    colour=await self._colour(),
                )
            )
        except discord.HTTPException:
            log.exception("OSINT: could not open the progress message")
            return

        async def on_progress(checked: int, total: int, hits: int) -> None:
            bar = _progress_bar(checked, total)
            try:
                await message.edit(
                    embed=discord.Embed(
                        title=title,
                        description=(
                            f"{box(bar, lang='yaml')}\n"
                            f"**Geprüft:** {checked}/{total} · **Treffer:** {hits}"
                        ),
                        colour=await self._colour(),
                    )
                )
            except discord.HTTPException:
                log.debug("OSINT: progress update failed", exc_info=True)

        try:
            report = await self.report_user(
                username,
                allow_nsfw=allow_nsfw,
                names=names,
                limit=limit,
                timeout=int(conf["user_timeout"] or 10),
                include_misses=bool(conf["user_misses"]),
                on_progress=on_progress,
            )
        except OSINTError as exc:
            await message.edit(embed=discord.Embed(description=str(exc)))
            return
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("OSINT: username lookup for %r failed", username)
            await message.edit(
                embed=discord.Embed(
                    description="Der Lookup ist fehlgeschlagen. Details stehen im Bot-Log."
                )
            )
            return

        view = ReportView(self)
        try:
            await message.edit(embeds=report.embeds[:10], view=view)
        except discord.HTTPException:
            log.exception(
                "OSINT: could not deliver the username report for %r", username
            )
            return
        if report.files:
            # ``Message.edit`` has no ``files`` parameter — it takes
            # ``attachments``, and older discord.py builds refuse new uploads
            # there altogether — so the file travels as its own message, which
            # every supported version accepts.
            try:
                await ctx.send(files=report.files)
            except discord.HTTPException:
                log.warning("OSINT: could not attach the report file for %r", username)
        self._remember(message.id, ctx.author.id, report.data)

    def build_file_report(self, data: bytes, filename: str) -> Report:
        """Everything worth knowing about an arbitrary file, as embeds."""
        info = file_metadata(data, filename)
        image = info["image"]
        exif = info["exif"]

        embed = discord.Embed(
            title=f"Datei · {truncate(info['filename'], 80)}",
            colour=discord.Colour.dark_teal(),
            timestamp=discord.utils.utcnow(),
        )
        kind = image.get("kind") or "unbekannt"
        dimensions = (
            f"{image['width']}×{image['height']}"
            if image.get("width") and image.get("height")
            else "—"
        )
        embed.add_field(name="Typ", value=f"{kind} · {info['size_human']}", inline=True)
        embed.add_field(name="Maße", value=dimensions, inline=True)
        embed.add_field(
            name="Hashes",
            value=box(
                "\n".join(
                    f"{name:<6} {value}" for name, value in info["hashes"].items()
                ),
                lang="yaml",
            ),
            inline=False,
        )

        if exif.get("present"):
            tags = exif["tags"]
            lines = [
                f"{name:<22} {truncate(value, 90)}" for name, value in tags.items()
            ]
            if exif.get("unknown"):
                lines.append(f"+ {len(exif['unknown'])} unbekannte Tags")
            embed.add_field(
                name=f"EXIF ({len(tags)} Felder)",
                value=box(truncate("\n".join(lines[:22]), 1000), lang="yaml"),
                inline=False,
            )
            if exif.get("gps"):
                latitude, longitude = exif["gps"]
                links = gps_links(latitude, longitude)
                embed.add_field(
                    name="GPS",
                    value=f"{latitude}, {longitude}\n[OpenStreetMap]({links['osm']})",
                    inline=False,
                )
            else:
                embed.add_field(
                    name="GPS", value="Keine Koordinaten hinterlegt.", inline=False
                )
        else:
            embed.add_field(
                name="EXIF",
                value="Keine EXIF-Daten gefunden. Discord entfernt bei Bild-Uploads die "
                "Metadaten serverseitig — bei einer Bild-URL wird die Originaldatei geladen.",
                inline=False,
            )

        if info["png_text"]:
            embed.add_field(
                name=f"PNG-Text ({len(info['png_text'])})",
                value=box(
                    truncate(
                        "\n".join(
                            f"{k}: {v}" for k, v in list(info["png_text"].items())[:12]
                        ),
                        1000,
                    ),
                    lang="yaml",
                ),
                inline=False,
            )
        if info["pdf"]:
            embed.add_field(
                name="PDF-Metadaten",
                value=box(
                    truncate(
                        "\n".join(f"{k}: {v}" for k, v in info["pdf"].items()), 1000
                    ),
                    lang="yaml",
                ),
                inline=False,
            )

        return Report(embeds=[embed], data={"target": info["filename"], "file": info})

    async def _download(self, url: str) -> Tuple[bytes, str]:
        """Fetch a file the user pointed at, from a public host only."""
        address = normalize_url(url)
        host = host_of(address)
        if not is_public_hostname(host) and not is_public_address(host):
            raise OSINTError(f"`{host or url}` ist kein öffentlicher Host.")

        await self._throttle("download")
        try:
            async with self._client().get(
                address, headers={"User-Agent": USER_AGENT, "Accept": "*/*"}
            ) as response:
                if response.status != 200:
                    raise OSINTError(
                        f"Der Download antwortete mit HTTP {response.status}."
                    )
                declared = response.headers.get("Content-Length")
                if declared and declared.isdigit() and int(declared) > MAX_FILE_BYTES:
                    raise OSINTError("Die Datei ist größer als 8 MB.")
                data = await response.content.read(MAX_FILE_BYTES + 1)
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            raise OSINTError(f"Download fehlgeschlagen: {type(exc).__name__}") from exc

        if len(data) > MAX_FILE_BYTES:
            raise OSINTError("Die Datei ist größer als 8 MB.")
        filename = host_of(address) + (
            address.rsplit("/", 1)[-1][:40] if "/" in address else ""
        )
        return data, filename

    # ------------------------------------------------------------------
    # Username enumeration (`[p]osint user`)
    # ------------------------------------------------------------------

    def _site_cache_path(self) -> Optional[Path]:
        """Where the downloaded site list lives, if Red told us where to write."""
        if cog_data_path is None:
            return None
        try:
            return Path(cog_data_path(self)) / DATA_FILENAME
        except Exception:
            log.debug("OSINT: no data path for the site list", exc_info=True)
            return None

    async def site_catalogue(self, force: bool = False) -> Dict[str, Site]:
        """The Sherlock site list: memory first, then disk, then GitHub."""
        now = time.monotonic()
        if not force and self._sites and now - self._sites_loaded < DATA_MAX_AGE:
            return self._sites

        path = self._site_cache_path()
        if path is not None and path.exists() and not force:
            try:
                if time.time() - path.stat().st_mtime < DATA_MAX_AGE:
                    cached = parse_sites(json.loads(path.read_text(encoding="utf-8")))
                    if cached:
                        self._sites, self._sites_loaded = cached, now
                        log.debug("OSINT: site list restored from %s", path)
                        return cached
            except (OSError, ValueError):
                log.debug("OSINT: cached site list unreadable", exc_info=True)

        status, payload, _ = await self._fetch("site-list", USERNAME_DATA_URL, ttl=0)
        sites = parse_sites(payload) if status == 200 else {}
        if not sites:
            if self._sites:
                return self._sites
            raise OSINTError(
                "Die Site-Liste von GitHub konnte nicht geladen werden "
                f"(HTTP {status}) — bitte später erneut versuchen."
            )

        self._sites, self._sites_loaded = sites, now
        if path is not None:
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(json.dumps(payload), encoding="utf-8")
            except OSError:
                log.debug("OSINT: could not cache the site list", exc_info=True)
        return sites

    async def _check_site(self, site: Site, username: str, timeout: int) -> Check:
        """Ask one site whether this username exists there."""
        url = profile_url(site, username)
        try:
            async with self._client().get(
                url,
                headers={"User-Agent": USER_AGENT, "Accept": "text/html,*/*"},
                allow_redirects=True,
                timeout=aiohttp.ClientTimeout(total=timeout),
            ) as response:
                status = response.status
                final_url = str(response.url)
                body = ""
                if status < 400:
                    body = (await response.content.read(USER_BODY_BYTES)).decode(
                        "utf-8", "replace"
                    )
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            return Check(
                site=site, verdict=VERDICT_ERROR, reason=type(exc).__name__, url=url
            )

        verdict, reason = classify_response(
            site, status=status, body=body, final_url=final_url, username=username
        )
        return Check(
            site=site,
            verdict=verdict,
            reason=reason,
            status=status,
            url=final_url or url,
        )

    async def report_user(
        self,
        username: str,
        *,
        allow_nsfw: bool = False,
        names: Optional[Sequence[str]] = None,
        limit: int = 0,
        timeout: int = 10,
        include_misses: bool = False,
        on_progress: Optional[Callable[[int, int, int], Awaitable[None]]] = None,
    ) -> Report:
        """Check one username against the Sherlock site list."""
        username = username.strip().lstrip("@")
        if not valid_username(username):
            raise OSINTError(
                "Erwartet wird ein Benutzername aus 1–40 Zeichen: Buchstaben, Ziffern, "
                "`_`, `-` und `.`."
            )

        catalogue = await self.site_catalogue()
        chosen = select_sites(
            catalogue, username, allow_nsfw=allow_nsfw, names=names, limit=limit
        )
        if not chosen:
            raise OSINTError(
                "Keine passende Site im Datensatz — Filter oder Name prüfen."
            )

        semaphore = asyncio.Semaphore(USER_CONCURRENCY)
        checks: List[Check] = []
        done = 0
        hits = 0
        last_update = 0.0

        async def run(site: Site) -> None:
            nonlocal done, hits, last_update
            async with semaphore:
                check = await self._check_site(site, username, timeout)
            checks.append(check)
            done += 1
            if check.verdict == VERDICT_HIT:
                hits += 1
            if on_progress is None:
                return
            # The final update always goes out; everything in between is spaced
            # out in time so a fast run cannot trip Discord's edit rate limit.
            now = time.monotonic()
            if done == len(chosen) or now - last_update >= USER_PROGRESS_INTERVAL:
                last_update = now
                await on_progress(done, len(chosen), hits)

        await asyncio.gather(*(run(site) for site in chosen))

        stats = summarize_checks(checks)
        embed = discord.Embed(
            title=f"Benutzername · {username}",
            colour=await self._colour(),
            timestamp=discord.utils.utcnow(),
        )
        embed.description = (
            f"**{len(stats['hits'])}** Treffer · **{len(stats['unsure'])}** unsicher · "
            f"{len(stats['misses'])} ohne Profil · {len(stats['errors'])} Fehler\n"
            f"Geprüft: {stats['checked']} von {len(catalogue)} Sites im Datensatz\n\n"
            "Ein **Treffer** ist ein Profil, bei dem die Seite ihr eigenes "
            "„gibt es nicht“ nicht gemeldet hat. Wenn nur der Statuscode dafür spricht "
            "und der Seitentext widerspricht, steht der Eintrag unter **unsicher**."
        )

        if stats["hits"]:
            titles = [
                f"[{check.site.name}]({check.url})"
                for check in stats["hits"][:USER_INLINE_HITS]
            ]
            name = (
                "Treffer"
                if len(stats["hits"]) <= USER_INLINE_HITS
                else f"Treffer (erste {USER_INLINE_HITS} von {len(stats['hits'])})"
            )
            embed.add_field(
                name=name, value=truncate("\n".join(titles), 1020), inline=False
            )
        else:
            embed.add_field(
                name="Treffer",
                value="Keine — der Name ist in keinem der geprüften Profile belegt.",
                inline=False,
            )

        if stats["unsure"]:
            unsure = [
                f"[{check.site.name}]({check.url}) — {truncate(check.reason, 70)}"
                for check in stats["unsure"][:10]
            ]
            embed.add_field(
                name=f"Unsicher ({len(stats['unsure'])})",
                value=truncate("\n".join(unsure), 1020),
                inline=False,
            )
        if stats["errors"]:
            embed.add_field(
                name=f"Fehler ({len(stats['errors'])})",
                value=(
                    f"{len(stats['errors'])} Sites haben nicht geantwortet. Viele Dienste "
                    "weisen Rechenzentrums-IPs ab — mit `[p]osint user <name> <site>` "
                    "lassen sich einzelne Sites nachprüfen."
                ),
                inline=False,
            )
        embed.set_footer(
            text="Sherlock-Datensatz (sherlock-project) · ein Profilaufruf pro Site, kein Login"
        )

        files: List[discord.File] = []
        if len(stats["hits"]) > USER_INLINE_HITS or (
            include_misses and stats["misses"]
        ):
            lines = [
                f"# {username}",
                f"{stats['checked']} Sites geprüft",
                "",
                f"## Treffer ({len(stats['hits'])})",
            ]
            lines += [f"{check.site.name}: {check.url}" for check in stats["hits"]]
            lines += ["", f"## Unsicher ({len(stats['unsure'])})"]
            lines += [
                f"{check.site.name}: {check.reason} — {check.url}"
                for check in stats["unsure"]
            ]
            if include_misses:
                lines += ["", f"## Ohne Profil ({len(stats['misses'])})"]
                lines += [check.site.name for check in stats["misses"]]
            files.append(
                text_to_file("\n".join(lines), filename=f"username-{username}.txt")
            )

        data: Dict[str, Any] = {
            "target": username,
            "dataset_sites": len(catalogue),
            "checked": stats["checked"],
            "hits": verdict_snapshot(checks, [VERDICT_HIT]),
            "unsure": verdict_snapshot(checks, [VERDICT_UNSURE]),
            "errors": verdict_snapshot(checks, [VERDICT_ERROR]),
        }
        if include_misses:
            data["misses"] = verdict_snapshot(checks, [VERDICT_MISS])

        return Report(embeds=[embed], files=files, data=data)

    # ------------------------------------------------------------------
    # Presentation helpers
    # ------------------------------------------------------------------

    async def _colour(self) -> discord.Colour:
        """Red's configured embed colour for this context.

        ``get_embed_colour(None)`` is valid — Red falls back to the bot colour
        when the location has no guild — and the stub bots used in tests get a
        fixed colour instead.
        """
        getter = getattr(self.bot, "get_embed_colour", None)
        if getter is None:
            return discord.Colour.dark_teal()
        try:
            return await getter(None)
        except Exception:
            return discord.Colour.dark_teal()

    def sources_embed(self) -> discord.Embed:
        """The transparency list of every external service contacted."""
        embed = discord.Embed(
            title="Datenquellen",
            description=(
                "Alles passiv, alles ohne API-Key. Es wird nie ein Port gescannt und "
                "kein Verzeichnis durchprobiert — nur öffentliche Datensätze abgefragt. "
                "Die Anfragen kommen von der IP des Bot-Hosts."
            ),
            colour=discord.Colour.dark_teal(),
        )
        for name, provides, limits in SOURCES:
            embed.add_field(name=name, value=f"{provides}\n_{limits}_", inline=False)
        embed.set_footer(text=f"Cache: {CACHE_SECONDS // 60} Minuten pro Abfrage")
        return embed

    def _link_block(self, target: str, kind: str) -> str:
        """Handy external links for further reading, all keyless."""
        quoted = target.replace(" ", "%20")
        if kind == "ip":
            return (
                f"[Weitere Ports (Shodan)](https://www.shodan.io/host/{quoted}) · "
                f"[Censys](https://search.censys.io/hosts/{quoted}) · "
                f"[AbuseIPDB](https://www.abuseipdb.com/check/{quoted})"
            )
        return (
            f"[CRT-Suche](https://crt.sh/?q=%25.{quoted}) · "
            f"[urlscan](https://urlscan.io/domain/{quoted}) · "
            f"[RIPEstat](https://stat.ripe.net/{quoted})"
        )


def _progress_bar(done: int, total: int, width: int = 20) -> str:
    """A plain-ASCII progress bar for the username lookup."""
    ratio = done / total if total else 1.0
    filled = max(0, min(width, int(width * ratio)))
    return f"[{'#' * filled}{'.' * (width - filled)}] {int(ratio * 100):3d}%"


def _parse_cdx(value: Any) -> List[Tuple[str, str]]:
    """Turn a CDX response into ``(timestamp, original)`` pairs.

    Accepts the raw JSON text as well as an already parsed list, because
    ``_fetch`` hands back both.
    """
    if isinstance(value, str):
        if not value:
            return []
        try:
            rows = json.loads(value)
        except ValueError:
            return []
    else:
        rows = value
    if not isinstance(rows, list):
        return []
    snapshots: List[Tuple[str, str]] = []
    for row in rows:
        if not isinstance(row, list) or len(row) < 3 or row[0] == "urlkey":
            continue
        snapshots.append((str(row[1]), str(row[2])))
    snapshots.sort(key=lambda item: item[0])
    return snapshots


def _clean_text(value: str) -> str:
    """Strip markup out of a Gravatar about-me blob."""
    return _TAG_RE.sub("", value).replace("&nbsp;", " ").strip()
