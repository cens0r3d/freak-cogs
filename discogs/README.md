# Discogs

Search the [Discogs](https://www.discogs.com) database from Discord. Releases,
master releases, artists and labels come back as interactive lists: pick a row
to open its tracklist, credits, identifiers, community rating and marketplace
price, then page through as many pressings or records as you like.

**No API key and no token required** — the API answers anonymously. A free user
token only raises the rate limit.

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

No requirements beyond Red itself.

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
a **name/title**, which is searched first and the first hit used. Use the ID when
you know it — it is exact.

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
| `--limit` | Results per page, 1–25 (default from `[p]discogs set perpage`) |

`top` takes `--genre`, `--style`, `--year`, `--country`; `random` takes those
plus `--decade` (`60s`, `1990s`). Quote anything that contains a space.
Unknown flags are refused by name instead of quietly ending up in the query.

## Genres and styles

Discogs separates 15 **genres** (`Electronic`, `Jazz`, `Rock`, `Funk / Soul`,
`Hip Hop`, `Pop`, `Classical`, …) from hundreds of **styles** (`Techno`, `House`,
`Ambient`, `Synth-pop`, …). A style behind `--genre` would simply return nothing,
so the cog sends a non-genre value as `--style` and says so in the footer.
`--genre Electronic --style Techno` is the precise form.

## Settings

`[p]discogs set` needs **Manage Server**.

| Setting | Default | Meaning |
| --- | --- | --- |
| `perpage <1-20>` | `8` | Entries per result page |
| `currency <code>` | `EUR` | Marketplace currency (`EUR`, `USD`, `GBP`, `CHF`, `CAD`, `AUD`, `JPY`, `SEK`, `NOK`, `DKK`, `PLN`, `BRL`) |
| `thumbnails <true\|false>` | `true` | Cover images in detail views |
| `show` | — | Show the current settings |

## Token and limits

Anonymous requests allow 25 requests per minute, a token 60 — the cog waits
instead of collecting `429`s. Responses are cached in memory (5 minutes for
searches, 30 for releases, artists, labels and masters), so paging costs nothing.

A token is optional: generate one at
<https://www.discogs.com/settings/developers> ("Generate new token"), then
`[p]discogs token` (owner, opens Red's secure form) or
`[p]set api discogs token,<token>`. Both take effect immediately.

Only guild settings are stored; queries go to `api.discogs.com` from the bot host.

## Troubleshooting

- **"Slow down a moment"** — the minute's budget is used up. Wait ~30 seconds.
  `[p]discogs status` shows the window.
- **"Nothing found"** — try fewer filters. `--format Vinyl --country UK` on a
  digital-only release is a guaranteed miss.
- **No cover images** — either `set thumbnails false`, or Discogs has no image on
  that entry.
- **A release sounds wrong** — a master (#96559) is the *record*, a release
  (#249504) is one pressing. `[p]discogs versions` lists them all.
- **A button is dead after a restart** — run the command again; buttons live 10
  minutes and their data is in memory, not on disk.
