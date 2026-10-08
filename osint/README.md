# osint

Passive infrastructure and username reconnaissance for Red-DiscordBot. Everything
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
| `[p]osint user <name> [sites]` | Username check across ~480 sites |
| `[p]osint sitelist [filter]` | Which sites `user` can check |
| `[p]osint sources` | Every external service contacted, with its limits |
| `[p]osint status` | The settings for this server |
| `[p]osint set …` | Channel list, cooldown, audit log, user-lookup limits |

The folder is called `osint` and contains three modules:

* `osint.py` — the cog, commands, HTTP layer, report builders
* `parsers.py` — pure stdlib: target classification, DoH/RDAP shaping, SPF/DMARC,
  security-header grading, EXIF/TIFF, PNG chunks, PDF info
* `usernames.py` — the username engine and its verdict rules

There are **no requirements** — the cog uses `aiohttp`, which ships with Red, so
`[p]cog update` costs nothing on the bot host.

## Username hits are not all equal

A site only tells you "this username exists" through the shape of its own error
page, and the Sherlock dataset has three shapes (`status_code`, `message`,
`response_url`). The engine only counts a **hit** when the site's not-found
signal stayed absent — a 2xx where the page then says "user does not exist", an
unreadable body, or a redirect away from the profile URL is reported as
**unsure**, never as found. NSFW entries (19 of 478) are skipped unless the
channel is age-restricted.

`[p]osint user` overlaps with the `namint` cog that lives next to it: both
enumerate usernames, `namint` against the WhatsMyName dataset, this one against
Sherlock's. Use whichever dataset fits; their verdict rules follow the same
policy.
