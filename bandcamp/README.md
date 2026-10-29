# Bandcamp

Browse [Bandcamp](https://bandcamp.com) from Discord. Search the catalogue, open
a release with its cover, price, tags, credits and a fully linked tracklist, read
a single track, walk an artist's discography, and read the lyrics — from a search
phrase or straight from a `bandcamp.com` link.

**No API key and no token required.** No Python package beyond what Red ships.

```
[p]bandcamp search burial
[p]bandcamp album https://burial.bandcamp.com/album/untrue
[p]bandcamp track archangel
[p]bandcamp artist radiohead
[p]bandcamp discography aphex twin
[p]bandcamp lyrics https://burial.bandcamp.com/track/archangel
[p]bandcamp play https://burial.bandcamp.com/track/archangel
```

Every command exists twice: as `[p]bandcamp …` (alias `[p]bc`) and as
`/bandcamp …`.

## Install

```
[p]repo add freak-cogs https://github.com/cens0r3d/freak-cogs
[p]cog install freak-cogs bandcamp
[p]load bandcamp
```

## Commands

| Command | What it does |
| --- | --- |
| `[p]bandcamp search <query>` | Artists, albums and tracks; a menu opens the picked entry |
| `[p]bandcamp album <query\|link>` | A release: cover, released, tracks, price, tags, credits, tracklist |
| `[p]bandcamp track <query\|link>` | One track: album, number, length, label, lyrics hint |
| `[p]bandcamp artist <query\|link>` | Profile: portrait, city, bio, release count, newest releases |
| `[p]bandcamp discography <artist>` | Every release, newest first, in a pickable list |
| `[p]bandcamp lyrics <track\|album>` | The lyrics of one track, or of every track that has them |
| `[p]bandcamp play <track\|link>` | Post a track's preview as a playable file |
| `[p]bandcamp follow <artist\|label>` | Watch an artist and announce new releases |
| `[p]bandcamp unfollow <artist\|id>` | Stop watching one |
| `[p]bandcamp following` | What this server watches, and where |
| `[p]bandcamp link <url>` | Whatever a link points at — album, track or band page |
| `[p]bandcamp autoembed [on\|off]` | Answer pasted Bandcamp links with an embed (default off) |
| `[p]bandcamp autoembed channel [#channel]` | Limit previews to one channel |
| `[p]bandcamp set perpage <1-25>` | Entries per list page (default 10) |
| `[p]bandcamp set play <on\|off>` | Allow posting tracks as playable files (default on) |
| `[p]bandcamp set playlimit <1-20>` | Largest posted track in MiB (default 8) |
| `[p]bandcamp notifications channel <#channel>` | Where new releases are announced |
| `[p]bandcamp notifications off` | Stop announcing (watchlist stays) |
| `[p]bandcamp notifications role <@role\|none>` | Ping a role with each announcement |
| `[p]bandcamp notifications interval <15-360>` | Poll interval in minutes (default 30) |
| `[p]bandcamp notifications check` | Poll right now and report what is new |
| `[p]bandcamp set` / `status` | This server's settings / endpoint, cache size, library |
| `[p]bandcamp clearcache` | Drop the cache (bot owner) |

`search`, `album`, `track`, `artist`, `discography`, `lyrics` and `link` take a
phrase **or** a link. Nothing else is parsed: no ids to look up, no flags to
remember.

## Watchlist

Follow an artist or a label and the bot announces what they publish:

```
[p]bandcamp follow burial
[p]bandcamp notifications role @releases
[p]bandcamp following
```

Run `follow` where the announcements should go: **as long as no channel is
configured, the first `follow` takes the channel it was used in.** A channel set
with `notifications channel` always wins.

- **Following seeds the list silently.** Releases the page already lists are
  recorded, so following a label with 225 releases announces nothing until
  something genuinely new appears.
- **One card per release**, at most three per band per poll, with cover, released,
  tracks, price and a Play menu — plus a ping if `notifications role` is set.
- **Cap:** 50 followed pages per server. Labels work like artists — they are just
  band pages with a long discography.
- A release whose details will not load is still announced from the discography
  row alone (title, date, cover).

## Playback

Every release view carries a **▶ Play a track** menu, and a single track view a
**▶ Play** button. Picking one posts that track's 128 kbps preview as an audio
file — the only way Discord shows a player.

- **It is a copy, not a stream.** The file counts against the upload limit, and
  nothing is posted automatically.
- **One track per action.** No queue, no playlist, no voice channel.
- 128 kbps, so **1 MiB ≈ 1 minute**. `set playlimit` caps the size (default 8 MiB);
  a track that does not fit is refused with its size instead of a broken upload.
- A track without a preview, a missing **Attach Files** permission or a smaller
  server limit is reported, never raised.

`set play off` turns the controls off server-wide.

## Notes

- Search answers are cached for 10 minutes, everything else for 30, in memory.
  Paging through a result costs no requests.
- Rate limiting from Bandcamp is passed through as "try again in about N seconds".
- `autoembed` is off by default, needs **Manage Server**, answers at most two
  links per message and never pings.
- Only guild settings are stored (per page, playback cap, auto preview channels,
  watchlist with channel, role and interval). No Discord user data.
- An expected failure (nothing found, unresolved link, rate limit, timeout) is an
  embed with a sentence, never a traceback.

## Troubleshooting

- **"Could not resolve that link"** — the slug does not exist, or the link is a
  `/discover` page. Search for the title instead.
- **"Nothing on Bandcamp matched …"** — two words beat one: `burial untrue`, not
  `untrue`.
- **Wrong album opened** — the top hit wins; a link is the exact way. Same titles
  by different artists are common.
- **"has no lyrics on Bandcamp"** — the release really carries none.
- **"too long to post as a file"** — raise `set playlimit`, or open the track on
  Bandcamp.
- **"Bandcamp has no preview for this track"** — the artist hid it from streaming
  (preorders do this).
- **No player after picking a track** — the bot needs **Attach Files** there.
- **Followed a band and nothing was announced** — existing releases were recorded
  on purpose; the next genuine upload announces.
- **Announcements land in the wrong channel** — the first `follow` adopted the
  channel it ran in. `notifications channel #right-one` corrects it.
- **`notifications check` says "nothing new"** — the release is already known or
  the page has not published it.
- **A button says the view expired** — the bot restarted; run the command again.
