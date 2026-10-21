# Discogs

Search the [Discogs](https://www.discogs.com) database from Discord. Releases,
master releases, artists and labels come back as interactive lists: pick a row
to open its tracklist, credits, identifiers, community rating and marketplace
price, then page through as many pressings or records as you like.

**No API key and no token required** — the API answers anonymously. A free user
token only raises the rate limit from 25 to 60 requests per minute.

```
[p]discogs daft punk
[p]discogs search blue monday --format Vinyl --country UK --sort want
[p]discogs release 249504
[p]discogs artist aphex twin
[p]discogs random --genre Jazz --decade 60s
```

## Install

```
[p]repo add freak-cogs https://github.com/cens0r3d/freak-cogs
[p]cog install freak-cogs discogs
[p]load discogs
```

No requirements beyond Red itself — the cog uses `aiohttp`, which ships with Red.

## Commands

| Command | What it does |
| --- | --- |
| `[p]discogs <query>` | Quick release search |
| `[p]discogs search <query> [flags]` | Full search with filters |
| `[p]discogs release <id>` | One release: facts, tracklist, credits, identifiers, videos, rating, price |
| `[p]discogs master <id>` | The master release behind all pressings, with a **Versions** button |
| `[p]discogs artist <name\|id>` | Artist profile (real name, aliases, groups, links) with a **Releases** button |
| `[p]discogs label <name\|id>` | Label profile (parent, sublabels, links) with a **Releases** button |
| `[p]discogs versions <master>` | Every pressing and version of a master release |
| `[p]discogs top [flags]` | The most wanted records, optionally by genre, style, year or country |
| `[p]discogs random [flags]` | Draw a random record |
| `[p]discogs token` | Store an optional Discogs user token (bot owner) |
| `[p]discogs status` | API state, rate-limit window, cache and this server's settings |
| `[p]discogs clearcache` | Drop the in-memory cache (bot owner) |
| `[p]discogs set …` | Per-server settings (Manage Server) |

`release`, `master`, `artist`, `label` and `versions` accept a **numeric ID** or
a **name/title**, which is searched first and the first hit used. An ID is exact
and costs one request, a name is a guess — use the ID when you know it.

## Search flags

| Flag | Values |
| --- | --- |
| `--type` | `release` (default), `master`, `artist`, `label` |
| `--genre` | A Discogs genre (see below) |
| `--style` | A Discogs style, e.g. `Techno`, `House`, `Ambient` |
| `--year` | `1995`, `95`, `1995-1999` (first year wins) |
| `--country` | `UK`, `US`, `Germany`, `Europe` … |
| `--format` | `Vinyl`, `CD`, `Cassette`, `File` … |
| `--label` | Label name as text |
| `--artist` | Artist name as text |
| `--catno` | Catalogue number |
| `--sort` | `year`, `artist`, `title`, `label`, `catno`, `format`, `rating`, `added`, `want` |
| `--limit` | Results per page, 1–25 (default comes from `[p]discogs set perpage`) |

`top` takes `--genre`, `--style`, `--year`, `--country`; `random` takes those
plus `--decade` (`60s`, `1990s`). Quote anything that contains a space:

```
[p]discogs search --style "Detroit Techno" --year 1993
[p]discogs top --genre Techno --year 1995
```

Unknown flags are refused by name instead of quietly ending up in the query.

## Genres and styles

Discogs separates **15 genres** (`Electronic`, `Jazz`, `Rock`, `Funk / Soul`,
`Folk, World, & Country`, `Hip Hop`, `Pop`, `Classical`, `Latin`, `Reggae`,
`Blues`, `Stage & Screen`, `Non-Music`, `Brass & Military`, `Children's`) from
hundreds of **styles** (`Techno`, `House`, `Ambient`, `Synth-pop`, …).

Filtering `genre=Techno` does not error — it returns zero results. So when the
value behind `--genre` is not one of the 15, the cog sends it as `--style`
instead and says so in the footer. `--genre Electronic --style Techno` is the
precise form.

## The interactive views

- Result lists show numbered rows with year, country, format, label and
  want/have counts. **Previous/Next** page through the results, the **picker**
  opens the highlighted entry, **Open on Discogs** leaves Discord.
- Detail views page through long tracklists, credits and profiles instead of
  truncating them, and carry the same Discogs link.
- An artist or label detail has a **Releases** button (its whole catalogue, page
  by page); a master has **Versions** (every pressing with country, format and
  catalogue number).
- Buttons answer for 10 minutes. After a restart or reload a stale button still
  replies — with "that list has expired, run the command again" — instead of
  failing silently.

## Rate limits, caching and the token

- Anonymous requests are limited to **25 per minute**, a user token raises that
  to **60**. The cog reads `X-Discogs-Ratelimit` from every response, keeps a
  sliding one-minute window and **waits** instead of collecting `429`s; a `429`
  that does arrive triggers a backoff and one retry.
- Responses are cached in memory: **5 minutes** for searches, **30 minutes** for
  releases, artists, labels and masters. Paging back and forth costs nothing.
  `[p]discogs clearcache` drops it.
- A token is optional. Generate one at
  <https://www.discogs.com/settings/developers> ("Generate new token"), then:
  - `[p]discogs token` (bot owner) opens Red's secure form, or
  - `[p]set api discogs token,<token>`.
  Both take effect immediately — no reload. `[p]discogs status` shows whether a
  token is in use and how much of the minute's budget is left.

## Settings

`[p]discogs set` needs **Manage Server**.

| Setting | Default | Meaning |
| --- | --- | --- |
| `perpage <1-20>` | `8` | Entries per result page |
| `currency <code>` | `EUR` | Marketplace currency (`EUR`, `USD`, `GBP`, `CHF`, `CAD`, `AUD`, `JPY`, `SEK`, `NOK`, `DKK`, `PLN`, `BRL`) |
| `thumbnails <true\|false>` | `true` | Cover images in detail views |
| `show` | — | Show the current settings |

## Data and privacy

Guild settings only (results per page, currency, thumbnails) are stored, in
Red's Config. Search queries and Discogs responses are cached **in memory** and
dropped when the bot restarts; nothing about Discord users is stored. Queries go
to `api.discogs.com` with a `freak-cogs-discogs` user agent from the bot host,
so that host's IP is visible to Discogs. An optional token lives in Red's shared
API token store. `[p]mydata` therefore returns nothing for this cog.

## Troubleshooting

- **"Slow down a moment"** — the minute's budget is used up. Wait ~30 seconds;
  a token triples the budget. `[p]discogs status` shows the window.
- **"Nothing found"** — try fewer filters. `--format Vinyl --country UK` on a
  digital-only release is a guaranteed miss, and a style typed as `--genre` is
  auto-corrected but still narrow.
- **No cover images** — either `[p]discogs set thumbnails false`, or Discogs has
  no image on that entry. Images are signed URLs; they cannot be rebuilt.
- **A release sounds wrong** — a master (#96559) is the *record*, a release
  (#249504) is one pressing. `[p]discogs versions` lists them all.
- **The bot restarted and a button is dead** — run the command again; the view
  data lives in memory, not on disk.
