# Bandcamp

Browse [Bandcamp](https://bandcamp.com) from Discord. Search the catalogue, open
a release with its cover, price, tags, credits and a fully linked tracklist, read
a single track, walk an artist's discography, and read the lyrics — from a search
phrase or straight from a `bandcamp.com` link.

**No API key and no token required.** Bandcamp's own mobile endpoints answer
anonymously, and the cog needs no Python package beyond what Red already ships.

```
[p]bandcamp search burial
[p]bandcamp album https://burial.bandcamp.com/album/untrue
[p]bandcamp track archangel
[p]bandcamp artist radiohead
[p]bandcamp discography aphex twin
[p]bandcamp lyrics https://burial.bandcamp.com/track/archangel
[p]bandcamp play https://burial.bandcamp.com/track/archangel
```

Every command exists twice: as `[p]bandcamp …` (short alias `[p]bc`) and as the
slash command `/bandcamp …`.

## Install

```
[p]repo add freak-cogs https://github.com/cens0r3d/freak-cogs
[p]cog install freak-cogs bandcamp
[p]load bandcamp
```

Three names, because they differ: the **folder** is `bandcamp`, `[p]cog install`
takes `bandcamp`, and the commands are `[p]bandcamp` / `[p]bc` and
`/bandcamp`.

`requirements` is empty on purpose — see [Why no library](#why-no-library).

## Commands

| Command | What it does |
| --- | --- |
| `[p]bandcamp search <query>` | Artists, albums and tracks; a menu opens the picked entry |
| `[p]bandcamp album <query\|link>` | A release: cover, released, tracks, price, tags, credits, tracklist |
| `[p]bandcamp track <query\|link>` | One track: album, number, length, label, lyrics hint |
| `[p]bandcamp artist <query\|link>` | Profile: portrait, city, bio, release count, newest releases |
| `[p]bandcamp discography <artist>` | Every release, newest first, in a pickable list |
| `[p]bandcamp lyrics <track\|album>` | The lyrics of one track, or of every track that has them |
| `[p]bandcamp play <track\|link>` | Post a track's preview as a playable file (see [Playback](#playback)) |
| `[p]bandcamp link <url>` | Whatever a link points at — album, track or band page |
| `[p]bandcamp autoembed [on\|off]` | Answer pasted Bandcamp links with an embed (default off) |
| `[p]bandcamp autoembed channel [#channel]` | Limit previews to one channel (`channel remove #channel` drops it) |
| `[p]bandcamp set perpage <1-25>` | Entries per list page (default 10) |
| `[p]bandcamp set play <on\|off>` | Allow posting tracks as playable files (default on) |
| `[p]bandcamp set playlimit <1-20>` | Largest posted track in MiB (default 8) |
| `[p]bandcamp set` | Show this server's settings |
| `[p]bandcamp status` | Endpoint, cache size, library |
| `[p]bandcamp clearcache` | Drop the cache (bot owner) |

`search`, `album`, `track`, `artist`, `discography`, `lyrics` and `link` take a
phrase **or** a link. Nothing else is parsed: no ids to look up, no flags to
remember.

## Links without ids

A Bandcamp URL carries no numeric ids, and the release page answers a datacenter
IP with a JavaScript challenge ("Client Challenge") instead of HTML — scraping
ids out of the page is not an option. So a link is resolved through the search
endpoint: subdomain and slug become the query (`burial` + `untrue`), and the hit
whose URL matches wins.

```
https://burial.bandcamp.com/album/untrue
  -> search "burial untrue" -> album URL match -> band_id 477594255, album 856850876
  -> /api/mobile/24/tralbum_details
```

Consequences:

- A link resolves **to itself or not at all**. A mistyped slug answers
  "Could not resolve that link", never a fuzzy neighbour.
- Front page, `/discover/...` and `daily.bandcamp.com` links carry no entity and
  are refused; use `search` for those.
- `bandcamp.com/...` links without a band subdomain are refused the same way.

## The embeds

- **Album** — cover as the large image, artist as the author line (with avatar),
  title linked to the release page, `Released` / `Tracks` / `Price` inline, tags,
  then the tracklist: every row is `NN` + linked track title + duration. Long
  tracklists and credits spill onto further pages instead of being cut. A label
  release names the performer and the page that published it.
- **Track** — cover, album and track number, length, and a hint when the track
  has lyrics. The footer carries the label.
- **Artist** — portrait as the thumbnail, city, genre, release count, bio, and
  the newest six releases with their years.
- **Lyrics** — one embed per track that has any, paged when a text is long.
- Every detail view carries **Open on Bandcamp**; multi-page views page with
  ‹ Previous / Next ›.

Price lines read like the release page: an amount with its currency, `Name your
price`, `Free`, or `Preorder`.

## Playback

Every release view carries a **▶ Play a track** menu — one entry per track with
its number and duration — and a single track view carries a **▶ Play** button.
Picking one posts that track's **128 kbps preview as an audio file**, which is
the only way Discord shows a player: a bot cannot put playable audio *inside* an
embed, and a bare link to Bandcamp's stream endpoint unfurls to nothing.

What that means in practice:

- **It is a copy, not a stream.** The file is uploaded to the channel, so it can
  be downloaded, and it counts against the upload limit. Playback is therefore
  strictly on demand — nothing is posted automatically, `autoembed` included.
- **One track per action.** No queue, no playlist, no voice channel.
- **128 kbps**, Bandcamp's preview quality. ~17 KB/s, so **1 MiB ≈ 1 minute** of
  audio: the default 8 MiB cap is roughly eight minutes, the 20 MiB hard limit
  about twenty. The size is checked twice — declared `Content-Length` first,
  then while reading — and a track that does not fit is refused with its size
  instead of a broken upload.
- **Fresh link per play.** The `stream_redirect` URL in a cached payload is
  signed with a timestamp, so a play re-reads the track (one cheap request)
  instead of trusting an answer that may be half an hour old.
- **A track without a preview** (an artist can hide one, common on preorders)
  answers with that fact instead of an empty file.
- Missing **Attach Files** permission or a smaller server limit is reported, not
  raised.

`[p]bandcamp set play off` turns the controls off server-wide (the menus stay,
the click answers with a note), `[p]bandcamp set playlimit <1-20>` caps the size.

Note on rights: a posted preview is a copy of the artist's stream hosted by
Discord. Keep it to what people actually ask to hear — on demand, one track at a
time — and point people at the release page (`Open on Bandcamp`) when they like
it.

## Lists and buttons

- A list shows numbered rows (`01` + type marker + linked title + metadata) with
  **Previous/Next** and a **picker**; picking opens the entry **in the same
  message** with a **‹ Results** button to go back.
- Views live for 10 minutes. After a restart or `[p]reload`, a stale button still
  answers — "that Bandcamp view has expired" — instead of failing silently.
- An expected failure (nothing found, unresolved link, rate limit, timeout) is
  an embed with a sentence, never a traceback.

## Caching and rate limits

- Search answers: **10 minutes**. Albums, tracks, artists, discographies and
  lyrics: **30 minutes**. The cache is in memory (`[p]bandcamp status` shows its
  size, `[p]bandcamp clearcache` drops it) and is dropped on restart.
- Paging through a search costs no requests — the endpoint answers up to 50 hits
  at once and they are all kept.
- Lyrics cost **one extra request**; a release where no track reports lyrics
  costs none.
- `429` from Bandcamp becomes "Bandcamp is rate limiting the bot. Try again in
  about N seconds." — the `Retry-After` value is passed through.

## Auto previews

Off by default, because unsolicited bot posts are noise.

```
[p]bandcamp autoembed on
[p]bandcamp autoembed channel #music       # only there
[p]bandcamp autoembed channel remove #music
[p]bandcamp autoembed off
```

- Needs **Manage Server**.
- Up to two links per message, no ping, and the same link in the same channel
  is answered at most once per 5 minutes.
- Works on `on_message_without_command`, so it never fights with commands.
- Respects Red's `[p]command disable` for the cog per guild.

## Settings

`[p]bandcamp set` and `[p]bandcamp autoembed` need **Manage Server**.

| Setting | Default | Meaning |
| --- | --- | --- |
| `perpage <1-25>` | `10` | Entries per list page |
| `play <on\|off>` | `on` | Post tracks as playable files |
| `play_mib <1-20>` | `8` | Largest posted track (clamped to Discord's 20 MiB) |
| `autoembed <on\|off>` | `off` | Preview pasted links |
| `autoembed_channels` | empty | Empty = every channel, otherwise the listed ones |

## Why no library

The obvious candidate, [`bandcamp_async_api`](https://github.com/ALERTua/bandcamp_async_api),
declares `Requires-Python >=3.12` in **every** published release (`0.0.1` through
`0.3.0`). Red 3.5 runs on Python 3.11, and `pip` refuses such a package outright:

```
ERROR: Could not find a version that satisfies the requirement bandcamp-async-api>=0.3.0
       (from versions: none)
```

That is a hard install failure during `[p]cog install`, not a runtime warning, so
the cog carries the four calls it needs itself (`bandcamp/api.py`, `aiohttp`
only). The endpoints are Bandcamp's public JSON API:

| Endpoint | Used for |
| --- | --- |
| `GET /api/fuzzysearch/1/app_autocomplete` | search, and resolving a link |
| `GET /api/mobile/24/tralbum_details` | album and track, with tracklist |
| `POST /api/mobile/24/band_details` | artist profile and its discography |
| `GET /api/mobile/24/tralbum_lyrics` | lyrics (one map per release) |

Field names are parsed from the live answers, including two quirks: search rows
sometimes answer a **doubled URL** (`https://a.bandcamp.comhttps://a.bandcamp.com/track/x`,
the last one is real), and a standalone track's `has_lyrics` lives on the nested
track row, not on the release object.

## Data and privacy

Guild settings only (results per page, auto previews, preview channels, playback
and its size cap) in Red's Config. Search queries and Bandcamp answers are cached **in memory** and dropped
on restart; no Discord user data is stored and none leaves the bot. Queries go to
`bandcamp.com` from the bot host, so that host's IP is visible to Bandcamp — no
login, no cookie, no identity token is used. `[p]mydata` returns nothing for this
cog.

## Troubleshooting

- **"Could not resolve that link"** — the slug does not exist (or the link is a
  `/discover` page). Search for the title instead.
- **"Nothing on Bandcamp matched …"** — the search endpoint needs words from the
  title or the artist. Two words beat one: `burial untrue`, not `untrue`.
- **Wrong album opened** — the top hit wins; a link is the exact way. Same title
  by two artists is common (`Burial` exists four times on Bandcamp).
- **"Bandcamp is rate limiting the bot"** — wait out `Retry-After`; the message
  names the seconds.
- **"has no lyrics on Bandcamp"** — the release really carries none; instrumental
  tracks report `has_lyrics: false`.
- **No cover** — some releases have no `art_id`. The embed ships without an image
  rather than with a broken one.
- **"too long to post as a file"** — the track exceeds the size cap; the message
  names both sizes. Raise it with `[p]bandcamp set playlimit`, or open the track
  on Bandcamp.
- **"Bandcamp has no preview for this track"** — the artist hid it from
  streaming (preorders do this). Nothing to post.
- **No player after picking a track** — the bot needs **Attach Files** in that
  channel; without it Discord refuses the upload and the cog says so.
- **A button says the view expired** — the bot restarted; run the command again.
- **`[p]bandcamp` is unknown** — the cog is installed but not loaded, or another
  cog already claims `bc`. `[p]load bandcamp` and `[p]help Bandcamp` show which.
