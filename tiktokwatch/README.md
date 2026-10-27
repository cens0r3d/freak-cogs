# TikTokWatch

Announces TikTok livestreams and new clips in Discord — without an API key, a
login, a developer app or a request signature.

```
[p]repo add freak-cogs https://github.com/cens0r3d/freak-cogs
[p]cog install freak-cogs tiktokwatch
[p]load tiktokwatch
```

The folder, `info.json` and command group carry three different names:
install/load use **`tiktokwatch`**, `[p]help` shows **TikTokWatch**, and the
command is **`[p]tiktok`** (alias `[p]ttwatch`).

## Setup

Every account brings its own channel — there is deliberately no guild-wide
default channel.

```
[p]tiktok add @handle #alerts        # watch a creator, alerts go to #alerts
[p]tiktok list                       # what is watched, and where
[p]tiktok remove @handle
```

Clipping a profile URL works too: `[p]tiktok add tiktok.com/@handle/video/... #alerts`.

## Commands

| Command | What it does |
| --- | --- |
| `[p]tiktok add <handle> <#channel>` | Watch a creator; all alerts for it go to that channel |
| `[p]tiktok remove <handle>` | Stop watching |
| `[p]tiktok list` | Watched accounts, live state and channel |
| `[p]tiktok toggle <handle>` | Mute or unmute one account (state keeps updating silently) |
| `[p]tiktok setchannel <handle> <#channel>` | Move one account's alerts |
| `[p]tiktok setrole <handle> [@role]` | Role pinged for that account (omit to clear) |
| `[p]tiktok message <handle> [text]` | Custom line above the alert; `{handle} {title} {url} {viewers} {started}` |
| `[p]tiktok interval live\|content <seconds>` | Poll interval (live 30–3600 s, clips 120–7200 s) |
| `[p]tiktok alerts live\|content\|all <on\|off>` | Turn a kind of announcement on or off |
| `[p]tiktok videos <on\|off>` | Also post the MP4 of a new clip (needs **Attach Files**) |
| `[p]tiktok videomax <1-20>` | Largest clip file to attach, in MiB (max 20 = Discord's bot upload limit) |
| `[p]tiktok offline new\|edit\|delete` | What happens when a stream ends |
| `[p]tiktok settings` | Current configuration |
| `[p]tiktok check <handle>` | Probe TikTok right now (live state + latest clip) |
| `[p]tiktok test <handle>` | Post a sample live alert, nothing is stored |
| `[p]tiktok debug <handle>` | Raw endpoint data (owner only) |
| `[p]tiktok force` | Run a live and a content pass immediately (owner only) |

Defaults: live alerts every 90 s, clip alerts every 600 s, both on, live embed
refreshed every 300 s, stream end posted as a fresh embed with the duration.

## Data source

Two endpoints of the public web frontend, both keyless:

| Purpose | Request | Fields used |
| --- | --- | --- |
| Live state | `GET https://www.tiktok.com/api-live/user/room/?aid=1988&uniqueId=<handle>&sourceType=54` | `data.user.status` (`2` live, `4` offline), `liveRoom.title`, `startTime`, `streamId`, `coverUrl`, `liveRoomStats.userCount` |
| New clips | `GET https://www.tiktok.com/embed/@<handle>` | embedded `videoList[]` → `id`, `desc`, `coverUrl`, `playCount` |

An unknown handle answers `statusCode 19881007` on the live endpoint and HTTP
400 on the embed page; both are reported as "no such account" instead of looking
like a permanently offline creator.

**Images come from the payloads themselves, no extra request.** The live lookup
carries `user.avatarLarger` (present even in a stub room) and the stream's cover;
the embed page carries `userInfo.avatarThumbUrl` and the clip cover. A live alert shows the stream cover as the
large image with the avatar as a small thumbnail; when a room has no cover (stub
rooms) the avatar itself is the large image. New-clip alerts use the avatar as
the author icon and the clip cover as the image, falling back to the avatar when
the clip has none, and the end-of-stream embed keeps the avatar large. The live
embed has no "Live on TikTok" placeholder title - the watch link already says it. The CDN URLs are signed with an expiry, so
they are re-read on every poll and never stored.

**Viewer counts can be withheld.** For some accounts TikTok answers with a stub
room: live status and a room id, but an empty title, no cover, `startTime: 0` and
`liveRoomStats` full of zeros — even the live page ships `roomInfo: null`. A `0`
there is "not published", not "nobody watching", so the embed omits the viewer
field (and the uptime) instead of printing a fake zero. As soon as a real room
payload arrives, the fields appear.

**A broadcast is identified by a key, not by one field.** The key is built from
`streamId`, then the stream's start time, then TikTok's room id. Not every live
room carries a `streamId`: some payloads ship a sparse room with only the viewer
count, and checking the id alone would treat that as a brand-new broadcast on
every poll — one alert per interval. A creator genuinely restarting is a new key
and announces again; an unknown key never re-announces on its own.

**New clips are detected by id, never by position.** The `videoList` array starts
with the profile's *pinned* clip, which can be months old — announcing index 0
would repost an old video on every poll. Each account keeps the last 30 ids.
The first clip scan after `add` is silent on purpose; `[p]tiktok add` primes the
list when the account is added.

## Clip details and the video file

A clip announcement carries what the mobile app endpoint
(`api22-normal-c-alisg.tiktokv.com/aweme/v1/feed/`) knows and the web payloads do
not: the real upload date, plays, likes, comments and the clip's length. It is
called once per **announced** clip, never during a poll. That endpoint has the
tightest quota of all sources — it answers `ratelimit triggered` (HTTP 429) after
a few dozen calls — so it is backed by the public video page
(`/@i/video/<id>`, no handle needed, ~400 KB): same date and counters, the file
size from `bitrateInfo`, but no MP4 URL, because the page's `playAddr` is
token-gated. A clip therefore still gets its numbers when the mobile endpoint is
throttled, and only the file is skipped. Both paths are best effort: a failure
never costs the alert.

The same answer carries the MP4 URLs (`play_addr`, `download_addr`) and the file
size. With `[p]tiktok videos on` (default) the clip is posted as a file right
after the embed, so the video plays in Discord. The size is checked against
`data_size` **before** downloading, and again while streaming, against the
smaller of `[p]tiktok videomax` (default 20 MiB, which is Discord's upload limit for
bots - MiB because 20 * 1024 * 1024 bytes is the real ceiling) and the guild's own upload limit;
anything larger is skipped with a log line instead of a failed send. A missing,
expired or 404 CDN URL likewise just means "embed only".

## Rate limiting and proxies

The two endpoints tolerate a normal polling load from a single server IP: 30
parallel live lookups from a datacenter IP answer 200 in 0.5 s total. Higher
pressure is real though — after a few hundred requests within minutes the embed
page started answering **HTTP 429**. The cog reacts instead of hammering:

- `403`/`429` parks both loops for a window that grows with each consecutive
  block (30 s × streak, capped at 15 min), stored in Config so a restart does not
  reset it.
- Requests run with a concurrency limit of 4 and jitter of ±5–8 % per pass.
- `[p]tiktok settings` shows the remaining back-off and the reason.

Proxies are **not** required for the designed load (a handful of accounts polled
once or twice a minute). They become necessary for hundreds of accounts, for
sub-30-second intervals, or for reading live *chat* (which needs the WebSocket
and a signing service, not these endpoints).

## Troubleshooting

| Symptom | Cause and fix |
| --- | --- |
| "no-channel", no alerts | The account's channel was deleted or never set: `[p]tiktok setchannel <handle> <#channel>` |
| "handle not found" in `list` | Typo, or the account cannot go live. Re-add it with the exact handle from the profile URL |
| Nothing posted, `settings` shows a back-off | TikTok is rate limiting; wait it out or raise the intervals |
| Live alert appears twice | The creator really restarted the stream (a new broadcast key counts as new), or the account sits in two guilds |
| A clip was never announced | The first scan is silent by design; the clip list was primed at `add` time |
| Alerts work but no ping | `[p]tiktok setrole <handle> @role`; the bot needs Mention @everyone for the default role |

## Pings

Each account has its own ping role (`[p]tiktok setrole <handle> @role`). The
default role is emitted as the literal `@everyone`, never as `<@&guild-id>` —
Discord renders the raw form as `@@everyone`, which pings nobody.
`allowed_mentions.everyone` is derived from the same comparison, so a configured
`@everyone` actually pings, and any other role stays a normal role ping. If the
bot lacks **Mention @everyone** in the channel, `setrole` says so at config time
instead of leaving a silently ping-less alert.

Data: only guild-level configuration in Red's Config — channel and role IDs,
intervals, per-account settings, the last known live state and the last 30 clip
ids. Nothing about individual Discord users is stored.
