# osint

Passive infrastructure reconnaissance for Red-DiscordBot. Everything
it does reads public sources — nothing is scanned, probed or brute-forced.

| Command | What it does |
| --- | --- |
| `[p]osint lookup <target>` | Recognise the type by itself and route it |
| `[p]osint ip <ip>` | Geo, ASN, open ports, network block, reputation |
| `[p]osint domain <domain>` | RDAP registration, DNS, SPF/DMARC, HTTP header grade |
| `[p]osint subs <domain>` | Subdomains from certificate transparency logs |
| `[p]osint url <url>` | Live headers, urlscan history, Wayback snapshots |
| `[p]osint asn <AS…\|ip>` | RIPEstat routing data |
| `[p]osint mac <mac>` | OUI vendor lookup |
| `[p]osint gravatar <mail>` | Public Gravatar profile |
| `[p]osint exif [url]` | EXIF/GPS, PNG text, PDF info, hashes of a file |
| `[p]osint sources` | Every external service contacted, with its limits |
| `[p]osint status` | The settings for this server |
| `[p]osint set …` | Channel list, cooldown, audit log, provider cache |

The folder is called `osint` and contains three modules:

* `osint.py` — the cog, commands, HTTP layer, report builders
* `parsers.py` — pure stdlib: target classification, DoH/RDAP shaping, SPF/DMARC,
  security-header grading, EXIF/TIFF, PNG chunks, PDF info

There are **no requirements** — the cog uses `aiohttp`, which ships with Red, so
`[p]cog update` costs nothing on the bot host.

