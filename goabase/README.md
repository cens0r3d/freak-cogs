# goabase

New Goa/Psytrance parties from [goabase.net](https://www.goabase.net) as Discord
embeds, posted to a channel as they appear in the site's catalogue.

One message per party: flyer as the image, when/where/entry/line-up as fields,
and link buttons for tickets, map and the party page.

```
[p]goabase channel #parties
[p]goabase countries CH DE AT
[p]goabase interval 30
[p]goabase preview 3
```

Install as `goabase` (`[p]cog install freak-cogs goabase`), load with
`[p]load goabase`. The command is `[p]goabase`, alias `[p]gbase`, and every
subcommand works as a slash command too (`/goabase status`).

## Commands

| Command | What it does |
| --- | --- |
| `[p]goabase channel [#channel]` | Set the announcement channel (no argument clears it) |
| `[p]goabase countries [codes…]` | Watchlist of ISO country codes (max 20); no argument lists the current set and the countries that currently have parties. Setting a list switches worldwide mode off |
| `[p]goabase worldwide [on\|off]` | Ignore the country list and watch the whole site (no argument toggles) |
| `[p]goabase interval <5-360>` | Minutes between checks (default 30) |
| `[p]goabase timezone <zone>` | Timezone party times are rendered in (default `Europe/Zurich`) |
| `[p]goabase days <1-365>` | Only announce parties starting within this window (default 120) |
| `[p]goabase batch <1-10>` | At most this many announcements per poll (default 5) |
| `[p]goabase pause` / `resume` | Stop and restart announcements, settings kept |
| `[p]goabase reseed` | Mark every current party as already announced |
| `[p]goabase status` | Channel, state, countries, interval, known parties, next check |
| `[p]goabase next [n]` | Upcoming parties as a list, nothing posted |
| `[p]goabase show <link\|id>` | Post the embed for one party on demand |
| `[p]goabase autoembed [on\|off]` | Answer posted goabase party links with the embed (default off) |
| `[p]goabase autochannel [#channel]` | Limit link embeds to one channel (no argument: everywhere) |
| `[p]goabase preview [n]` | Post the next n parties right now, exactly as an announcement looks |

Settings need **Manage Server**; `status` and `next` are open to everyone.
The command group also needs **View Channel**, **Send Messages** and
**Embed Links** in the target channel.

## Where the data comes from

goabase ships three machine-readable views; the cog uses the second one.

| Endpoint | What it returns | Used |
| --- | --- | --- |
| `/party/rss/` | RSS 2.0, the 20 next parties **by date**, `ttl` 240. `?saAtt[country]=CH` filters by country | no |
| `/api/party/jsonld/?country=CH` | schema.org `ItemList` of **every** upcoming party for that country (id + name) | yes |
| `/api/party/jsonld/` | the same list without a filter: the whole catalogue, every country | yes, in worldwide mode |
| `/api/party/jsonld/<id>` | one `DanceEvent`: start/end, venue, address, geo, flyer, description, performers, offers, organizer, status | yes |

The RSS feed is fine as a feed but useless for "what is new": its items are
ordered by party date and capped at 20, so a party announced three months ahead
only enters the window when its date arrives, and one that drops out of the
window looks like it was deleted. `newest first by publication` is not a shape
the feed offers at all.

Detection is therefore a **set difference**: every poll reads the id list for the
configured countries, compares it against the ids already announced, and fetches
the detail payload only for the ones it has never seen. A party announced for
next spring is caught on the very poll where it first appears.

## Link embeds

`[p]goabase autoembed` makes the cog answer a posted party link with the same
embed, as a reply to that message:

```
[p]goabase autoembed
[p]goabase autochannel #parties     # optional: only that channel answers
[p]goabase show 118581              # or:  show https://www.goabase.net/party/biocross/118581
```

- Recognised: `/party/<slug>/<id>`, `/party/<id>`, `/<id>` (the form the RSS feed
  uses), with or without `www.`, with or without a language prefix (`/de/`,
  `/fr/`, `/es/`). Other hosts and non-party goabase pages are ignored.
- Up to three embeds per message, deduplicated by party id, so a wall of links
  cannot flood the channel.
- The embed is a **reply** to the message that carried the link; nothing is
  deleted and nobody is mentioned.
- Detail payloads are cached for ten minutes (200 entries), so re-posting the
  same link costs no request.
- Messages from bots, DMs, a guild where the cog is disabled, and a channel
  outside `autochannel` are skipped. A party that is not on goabase (or an
  unreachable site) stays silent rather than posting an error - the link is
  already in the channel.

## Country codes

Any ISO 3166-1 alpha-2 code is accepted (`CH`, `DE`, `AT`, `FR`, `US`, …). The
list the cog prints with `[p]goabase countries` is **not** a whitelist: it is
exactly the countries that currently have upcoming parties — 31 of them at the
time of writing, summing to the 321 parties of the worldwide list. That is why
France or Portugal are missing: goabase has no parties there right now. The API
answers an empty list for such a code instead of an error, so the cog accepts it
and says that nothing will come from there until a party is listed.

The list is read live from `/api/party/jsonld/country/list` and cached for an
hour; a bundled snapshot of the same 31 codes is only a fallback for when the
site is unreachable. A code that is not two letters is the only thing rejected.

## Worldwide

`[p]goabase worldwide` drops the country filter and watches the whole catalogue.
That is **one** request per poll instead of one per country, and it returns every
upcoming party on the site (~320 at the time of writing). The `countries` list
stays stored but is ignored until worldwide mode is switched off again.

Worldwide mode is where the two flood controls matter:

- `[p]goabase days <n>` — only announce parties that start within this window
  (default 120). Lower it to keep the channel tied to the near future.
- `[p]goabase batch <n>` — at most n announcements per poll (default 5, max 10).
  A worldwide setup with a long day window will sit at the batch cap for a while
  after the baseline; that is the intended behaviour, not a bug.
- `[p]goabase interval <min>` — with worldwide mode a shorter interval is cheap
  (one list request), so 15–30 minutes is reasonable.

Both toggling worldwide and changing the country list reset the baseline, so the
first cycle after a change announces nothing.

Consequences worth knowing:

- **The first poll after setup posts nothing.** It records the current catalogue
  as the baseline (`status` shows how many ids are known). `preview` is there for
  the moment you want to see the embeds.
- Changing `channel` or `countries` resets the baseline, so a new watchlist does
  not dump its backlog into the channel.
- Party ids stay in the "already announced" set for 45 days, then get pruned.
- At most `batch` announcements go out per cycle; the rest wait for the next one.
- A party whose detail request keeps failing is dropped after 3 attempts with a
  log line instead of blocking the queue.
- Deduplication is per party id, so a bot restart, a re-poll or a renewed
  baseline never double-posts.

Requests are one per configured country per poll, plus one per new party, with a
1 s gap between detail fetches. A descriptive user agent is sent.

## Embed contents

- **Title** → party page, coloured by state: violet normally, amber for
  *rescheduled*, red for *cancelled*, blue for *online events*.
- **Description** → the organiser's blurb, cleaned of CRLF and markdown noise,
  cut to ~320 characters at a word boundary.
- **📅 When** → `Fri 30 Oct 2026 · 21:00 – 06:00 (+1)` in the guild's timezone,
  plus a Discord relative timestamp. Times are formatted server-side on purpose:
  Discord's own `<t:…:t>` renders in the viewer's locale, so a 24-hour clock
  cannot be guaranteed otherwise.
- **📍 Where** → venue · town, country.
- **🎟️ Entry** → `Tickets from 28 CHF` with the ticket shop as its own button.
  goabase encodes "no presale" as an offer pointing back at its own
  `/party/entryfree/<id>` page; that becomes `Box office 20 CHF` and never a
  button, because it is a note about the door, not a shop.
- **🎧 Line-up** → first six acts from the performers blob, with link lines,
  stage labels, time prefixes and emoji stripped.
- **Buttons** → 🎟️ Tickets (only for an external shop), 📍 Map, 🔗 Party page,
  🌐 All parties.
- **Footer** → `goabase.net · <organizer> · CC BY-SA 2.0`. The credit is required:
  goabase data is published under CC BY-SA 2.0.

The flyer is requested as the JPEG variant of the WebP the API links, because
that renders everywhere. A party without a flyer is posted without an image
rather than with a broken one.

## Troubleshooting

| Symptom | Cause / fix |
| --- | --- |
| Nothing is posted after setup | Expected: the first poll is the baseline. `[p]goabase status` shows the id count and the next check; `[p]goabase preview 3` posts immediately |
| `Announcement channel cleared` | The channel was deleted; the cog clears the setting instead of failing every poll. Set it again |
| Missing permissions message | The bot needs View Channel, Send Messages and Embed Links in that channel |
| `Could not read the party list` | goabase.net was unreachable or answered a non-JSON page. Check `[p]goabase status`, then `[p]goabase reseed` |
| Parties from a neighbouring country appear | Country codes are authoritative on the site: a German party in a border town is listed under DE. Widen `countries` |
| Times look shifted | `[p]goabase timezone` decides the rendering; the data itself carries the party's own UTC offset |
| Line-up shows odd fragments | Organisers paste free text there. Link lines, stage labels and sentence fragments are filtered; the party page has the full text |

## Requirements

Red-DiscordBot 3.5.0+, discord.py 2.x, Python 3.9+ (`zoneinfo`). No
`requirements` — `aiohttp` ships with Red. Outbound HTTPS to
`www.goabase.net` and `img.goabase.net` is all it needs.
