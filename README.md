# freak-cogs

Red-DiscordBot cogs.

## Install

```
[p]repo add freak-cogs https://github.com/cens0r3d/freak-cogs
[p]cog install freak-cogs
[p]load vcstatus kickalerts voicesay namint micwatch osint idlerpg discogs deadbydaylight psychonaut sherlockosint twitterfixer
```

Or install one at a time:

```
[p]cog install freak-cogs vcstatus
[p]cog install freak-cogs kickalerts
[p]cog install freak-cogs voicesay
[p]cog install freak-cogs namint
[p]cog install freak-cogs micwatch
[p]cog install freak-cogs osint
[p]cog install freak-cogs idlerpg
[p]cog install freak-cogs discogs
[p]cog install freak-cogs deadbydaylight
[p]cog install freak-cogs psychonaut
[p]cog install freak-cogs sherlockosint
[p]cog install freak-cogs twitterfixer
```

## Cogs

### vcstatus

Puts a configurable status line on voice channels. Updates whenever someone joins, leaves or moves between channels, and can show a live member count with the `{count}` placeholder.

| Command | What it does |
| --- | --- |
| `[p]vcstatus toggle` | Turn the auto status on/off |
| `[p]vcstatus default <text>` | Default status for every voice channel |
| `[p]vcstatus channel <#channel> <text>` | Status for one specific channel |
| `[p]vcstatus remove [#channel]` | Drop a channel override, or reset everything |
| `[p]vcstatus empty [#channel] [on/off/reset]` | What an empty channel shows — server-wide or per channel |
| `[p]vcstatus resync` | Force a re-push to every voice channel |
| `[p]vcstatus show` | Show the current config |

Example:

```
[p]vcstatus default 🎧 {count} connected
[p]vcstatus channel #gaming 🎮 {count} playing
[p]vcstatus toggle
```

Notes:

- Needs the **Set Voice Channel Status** permission, plus **Manage Channels** while the bot is not connected to that channel.
- Messages are capped at 500 characters (Discord's limit).
- `{count}` counts humans only, bots are ignored.
- `[p]vcstatus empty` decides what an empty channel shows: a cleared status, or the message with a count of 0. It works server-wide or per channel — `[p]vcstatus empty #lounge` toggles that channel's own setting, `[p]vcstatus empty #lounge reset` makes it follow the server default again. A per-channel setting always wins.
- Updates are debounced (~2 s), so a burst of joins/leaves becomes one request per channel — the status route is rate limited.
- If a status ever looks stale, `[p]vcstatus resync` re-pushes everything without touching your config.
- Full manual: [`vcstatus/README.md`](vcstatus/README.md).

### kickalerts

Announces Kick.com livestreams. Talks to Kick's official public API (`api.kick.com/public/v1`) with OAuth2 app credentials, polls per guild, and keeps one message per stream: it appears when the streamer goes live and gets refreshed while the stream runs. When the stream stops, a summary embed with the stream time, peak and average viewers follows — either as a fresh post (`offlinepost new`, default) or by turning the live alert into the end-of-stream embed (`offlinepost edit`).

Setup:

```
[p]kickalert setcreds <client_id> <client_secret>
[p]kickalert setchannel #stream-alerts
[p]kickalert add <kick_username>
```

Grab API credentials at https://kick.com/settings/developer. Instead of `setcreds` you can also store them in Red's shared token store: `[p]set api kick client_id,<id> client_secret,<secret>` — both end up in the same place, which is where the cog reads them. When no credentials are set, `[p]kickalert authstatus` offers a button that opens Red's own secure form for this (bot owner only), and credentials changed with `[p]set api` take effect immediately — no reload needed.

| Command | What it does |
| --- | --- |
| `[p]kickalert setcreds <client_id> <client_secret>` | Store API credentials (bot owner, the message is deleted again) |
| `[p]kickalert authstatus` | Check whether the API auth works |
| `[p]kickalert add <username> [#channel]` | Start monitoring a streamer (verifies the channel exists) |
| `[p]kickalert remove <username>` | Stop monitoring a streamer |
| `[p]kickalert list` | Show all monitored streamers and their state |
| `[p]kickalert setchannel <#channel>` | Default alert channel |
| `[p]kickalert clearchannel` | Drop the default alert channel |
| `[p]kickalert setrole <@role> [username]` | Ping role, globally or per streamer |
| `[p]kickalert removerole [username]` | Drop the ping role |
| `[p]kickalert message <username> <text\|clear>` | Custom alert message — `{streamer}` `{title}` `{game}` `{url}` `{viewers}` `{uptime}` |
| `[p]kickalert toggle <username\|all>` | Pause one streamer, or the whole server |
| `[p]kickalert interval <30-3600>` | Poll interval in seconds for this server |
| `[p]kickalert style <detailed\|minimal>` | Full embed, or a one-line alert |
| `[p]kickalert autodelete <true\|false>` | Delete the alert when the stream ends instead of editing it (only with `offlinepost edit`) |
| `[p]kickalert offlinepost <new\|edit>` | Stream end: post a fresh summary embed (removes the live alert) or edit the live alert in place |
| `[p]kickalert clips <true\|false>` | Watch the channels' clips and post new ones |
| `[p]kickalert clipchannel <#channel>` | Where clip alerts go (default: each streamer's channel) |
| `[p]kickalert clipinterval <60-3600>` | Clip check interval in seconds |
| `[p]kickalert clipsset <username> <true\|false>` | Clip alerts for one streamer |
| `[p]kickalert clipscheck <username>` | Show the newest clips without posting |
| `[p]kickalert editlive <true\|false>` | Keep refreshing the alert while the stream runs |
| `[p]kickalert timezone <-12..14>` | Embed timestamp offset for this server |
| `[p]kickalert toggleviewers <true\|false>` | Show or hide the viewer count |
| `[p]kickalert togglecategory <true\|false>` | Show or hide the category |
| `[p]kickalert check <username>` | Look a channel up right now, without alerting |
| `[p]kickalert test <username>` | Post a sample alert with made-up numbers |
| `[p]kickalert debug <username>` | Show the raw API response for a channel |
| `[p]kickalert settings` | Show this server's configuration |
| `[p]kickalert force` | Poll every monitored streamer right now |
| `[p]kickalert clear` | Reset this server's kickalerts config (asks for confirmation) |

Everything except `setcreds` needs **Manage Server**.

Notes:

- A stream is identified by its start time, so a restart of the bot, a long poll interval or a flapping API never double-posts an alert — only a genuinely new stream announces again.
- The end-of-stream summary counts viewers on every poll, so the average is the mean of those samples and the peak is the highest one seen. A stream that ends within one poll interval has a stream time but no viewer statistics. The counters reset with every new broadcast.
- Credentials set with `[p]set api kick …` take effect immediately, without a reload. If none are stored yet, `[p]kickalert authstatus` offers Red's owner-only form so they never have to appear in chat.
- The poll loop backs off on rate limits (HTTP 429) and API errors and never dies from them. If the alert channel is gone or the bot lacks **Send Messages** / **Embed Links** there, the alert is skipped and retried on the next tick, so nothing is lost while permissions are fixed.
- Clip alerts use Kick's keyless web endpoint (`kick.com/api/v2/channels/<slug>/clips`), because the public API exposes no clip list. The first check per streamer is silent by design; after that new clips are detected by id, posted oldest-first and never announced twice.
- Deleting the alerts channel while it is configured clears the reference automatically.
- Upgrading from 2.x: existing settings and monitored streamers are kept. Credentials stored with the old config keys are moved into Red's shared `kick` token store on the first start, and the old keys are cleared.

### tiktokwatch

Announces TikTok livestreams and new clips. TikTok has no public API for this, so the cog reads two keyless endpoints of the web frontend: `/api-live/user/room/` for the live state (`data.user.status` `2` vs `4`, plus title, start time, cover and viewer count) and `/embed/@handle` for the clip list. No API key, no login, no session cookie, no request signature.

Setup — every account brings its own channel, there is no guild-wide default:

```
[p]tiktok add @handle #alerts
[p]tiktok list
```

| Command | What it does |
| --- | --- |
| `[p]tiktok add <handle> <#channel>` | Watch a creator; its alerts go to that channel |
| `[p]tiktok remove <handle>` | Stop watching |
| `[p]tiktok list` | Watched accounts, live state and channel |
| `[p]tiktok toggle <handle>` | Mute or unmute one account |
| `[p]tiktok setchannel <handle> <#channel>` | Move one account's alerts |
| `[p]tiktok setrole <handle> [@role]` | Role pinged for that account |
| `[p]tiktok message <handle> [text]` | Custom line — `{handle}` `{title}` `{url}` `{viewers}` `{started}` |
| `[p]tiktok interval <live\|content> <seconds>` | Poll interval (live 30–3600, clips 120–7200) |
| `[p]tiktok alerts <live\|content\|all> <on\|off>` | Turn a kind of announcement on or off |
| `[p]tiktok offline <new\|edit\|delete>` | Stream end: fresh summary, edit the alert, or delete it |
| `[p]tiktok settings` | Show this server's configuration |
| `[p]tiktok check <handle>` | Look a handle up right now, without alerting |
| `[p]tiktok test <handle>` | Post a sample live alert |
| `[p]tiktok debug <handle>` | Raw endpoint data (bot owner) |
| `[p]tiktok force` | Run a live and a content pass right now (bot owner) |

Notes:

- **New clips are detected by id, never by position.** The clip list starts with the profile's pinned video, which can be months old; announcing index 0 would repost an old clip on every poll. Each account remembers the last 30 ids, and the first scan after `add` is silent on purpose.
- A stream is identified by its `streamId`, so a restart, a missed tick or a flapping endpoint never double-posts. A creator restarting the stream is a new broadcast and announces again.
- Rate limits are handled, not ignored: `403`/`429` parks both loops for a window that grows with each consecutive block (30 s × streak, capped at 15 min), and `settings` shows the remaining time. Measured from one datacenter IP: 30 parallel live lookups answer in 0.5 s; after a few hundred requests the embed page did answer `429` — the cog backs off instead of retrying immediately.
- No proxy is needed for a handful of accounts. Hundreds of accounts, sub-30-second intervals or reading live chat (WebSocket + signing service) are different stories.
- Live alert embeds are refreshed while a stream runs; the end of a stream posts a summary with the duration and removes the stale live alert, so nothing in the channel keeps claiming the stream is live.
- Times are formatted server-side in the configured UTC offset, so a 24-hour clock actually appears as one.

- Full manual: [`tiktokwatch/README.md`](tiktokwatch/README.md).

### voicesay

Sends an attached audio file as a native Discord voice message, to a text channel or a DM.

```
[p]voicesay #channel    (with audio attached)
[p]voicesay @user       (with audio attached)
[p]voicesay 123456789   (raw channel or user ID)
```

Optional text after the destination becomes the message body:

```
[p]voicesay #general listen to this
```

Notes:

- Only the first attachment is used, and it has to be `audio/*`.
- Discord voice messages must be OGG Opus. Other formats get converted with **ffmpeg**, so ffmpeg should be on the host — without it you can still send `.ogg`/`.opus` files directly.
- The waveform preview also comes from ffmpeg; a placeholder is used if it's missing.

### naminter

OSINT username enumeration: checks whether a username exists on 700+ websites from the [WhatsMyName](https://github.com/WebBreacher/WhatsMyName) dataset, through the [Naminter](https://github.com/3xp0rt/Naminter) library.

```
[p]naminter check torvalds
[p]naminter check torvalds -c coding,social -l 100
[p]naminter check neo -x dating -e json
```

| Command | What it does |
| --- | --- |
| `[p]naminter check <username> [options]` | Run the lookup and post the hits |
| `[p]naminter sites [text]` | Search the site list by name or category |
| `[p]naminter categories` | Every category with its site count |
| `[p]naminter stats` | Dataset, engine and per-server settings |
| `[p]naminter refresh` | Re-download the dataset and rebuild the engine |
| `[p]naminterset show` | Show the current settings |
| `[p]naminterset mode <all\|any>` | Strict (AND) or loose (OR) detection |
| `[p]naminterset categories <add\|remove\|clear\|list> [cat]` | Default category filter |
| `[p]naminterset exclude <add\|remove\|clear\|list> [cat]` | Categories to skip |
| `[p]naminterset showmissing <true\|false>` | Show misses and errors without `--all` |
| `[p]naminterset maxpages <1-40>` | Cap how many result pages a lookup posts |
| `[p]naminterset perpage <5-20>` | Entries per result page |
| `[p]naminterset role <add\|remove\|clear\|list> [@role]` | Restrict lookups to these roles (empty = everyone) |
| `[p]naminterset reset` | Reset this server's settings |
| `[p]naminterset concurrency <1-100>` | Parallel site checks (bot owner) |
| `[p]naminterset timeout <5-120>` | HTTP timeout per request (bot owner) |
| `[p]naminterset runtimeout <30-1800>` | Wall-clock limit per lookup (bot owner) |
| `[p]naminterset impersonate <browser>` | Browser profile for requests (bot owner) |
| `[p]naminterset defaultlimit <0-2000>` | Default site cap, `0` = all (bot owner) |

Options of `check`:

```
-s, --sites a,b          only these sites (exact names, quote names with spaces)
-c, --category a,b       only sites in these categories
-x, --exclude-category   skip these categories
-m, --mode all|any       detection mode for this run
-l, --limit <n>          check at most n sites
-e, --export json|csv|txt  attach the full report as a file
-a, --all                also list misses, unsure results and errors
--strict                 only hits where the success text and the status code agreed
```

Under the summary of every lookup there is an **Export** button. Pressing it opens a small form that asks for the format (`JSON`, `CSV`, `TXT`) and the scope — *certain hits*, *hits and candidates*, or *everything* (including misses, unknown and errors) — and replies with the file **ephemerally** — only the person who started the lookup sees it, nothing is posted to the channel. If the API refuses the modal (selects in a modal need components-v2 support), the same three choices appear as a picker message with a **Send file** button instead. The button carries that run's results and stops working after ten minutes or a bot restart, and then tells you so; `--export` stays the non-interactive path for scripts.

Notes:

- Lookups are open to **every member** by default. `[p]naminterset role add @role` restricts them to that role plus server managers, `[p]naminterset role clear` reopens them to everyone. Per-user cooldown and a global concurrency cap apply either way.
- The dataset (~1 MB JSON) is downloaded in the background on load, cached on disk and refreshed after 6 hours; `[p]naminter refresh` forces it.
- Requests are made with browser impersonation (curl-cffi) from the bot's host, so its IP is visible to the sites being checked.
- Only the queried username leaves the host; nothing about it is stored. Hits are not cached or logged.
- **Hits are split by what actually matched.** The dataset gives each site a status code *and* a text string that a real profile has to contain. A site that answers `200` for every username — most do — is only caught by the second half, so when a site changes its markup you get a result where the *status code* matched and the success text was absent. Those are marked `🟡 unsure … only the status code matched — likely a dead link`, left out of the default list and out of the *certain hits* export; `--all` shows them. Measured over all 717 entries with a username that cannot exist, that pattern accounted for **40 of 43** apparent hits, while the reverse case (success text present, code differed) is a plausible real hit and stays in the list. `--strict` narrows a run to hits where both signals agreed.
- Found / partially found / ambiguous results are shown by default, misses and errors only with `--all` or `[p]naminterset showmissing true`.
- `--export` writes every result (site, category, status, **match reason**, URL, HTTP code, timing, error) to a file — use it when a run hits the page cap or the run timeout, and to triage an `unsure` row without re-running the lookup.
- The cog folder is called **`namint`** (`[p]load namint`), not `naminter` — Red puts the cogs directory on `sys.path`, so a folder named `naminter` would shadow the `naminter` package the cog imports. Delete any leftover `naminter/` folder in your cogs directory.

### micwatch

Moves members to another voice channel when they keep their **microphone switched on** — or actually **talk** — for longer than a configurable threshold (default 30 s). Made for the "afk with an open mic" problem: point `target` at a quarantine channel and let it sort itself out.

Two detection modes:

- **`mic`** — reads the mute state from voice state updates. Works without the bot joining any voice channel, and a member who just sits there with an open mic is moved too.
- **`speak`** — reads the voice-gateway speaking events, i.e. real voice activity. Discord only relays those to clients inside the same voice channel, so the bot joins the watched channel (muted and deafened, invisible as a speaker) and follows it while people are in there. Needs **PyNaCl** in the bot venv (`pip install pynacl`).

| Command | What it does |
| --- | --- |
| `[p]micwatch toggle` | Turn it on/off, and list anything still missing |
| `[p]micwatch mode [mic\|speak]` | Show/set the detection mode |
| `[p]micwatch threshold [seconds]` | Seconds before the move (default 30, 3–3600) |
| `[p]micwatch grace [seconds]` | `speak`: tolerated silence before the counter resets (default 2) |
| `[p]micwatch rearm [seconds]` | How long a moved member is left alone (default 60) |
| `[p]micwatch target [#channel\|off]` | Destination channel — `off` clears it |
| `[p]micwatch watch add\|remove\|list\|clear` | Monitored channels (empty = all, `mic` mode) |
| `[p]micwatch immune add\|remove\|list\|clear` | Roles/members that are never moved |
| `[p]micwatch notify` | Toggle the announcement in the target channel |
| `[p]micwatch resetmute` | `mic`: muting resets (on) or pauses (off) the counter |
| `[p]micwatch ignorebots` | Ignore other bots (default on) |
| `[p]micwatch message [text]` | Announcement text — `{user}` `{mention}` `{seconds}` `{channel}` |
| `[p]micwatch join [channel]` | Pin the bot into a channel (for `speak`) |
| `[p]micwatch leave` | Disconnect and drop the pin |
| `[p]micwatch settings` | Show this server's configuration |
| `[p]micwatch status` | Live view: who is being tracked, and for how many seconds |
| `[p]micwatch reset` | Reset this server's settings |

Example:

```
[p]micwatch target #quarantine
[p]micwatch watch add #lobby
[p]micwatch threshold 30
[p]micwatch toggle
```

Notes:

- Needs the **Move Members** permission, and **Connect** for `speak` mode. Members whose highest role is *above* the bot's are skipped (listed in `status` under "Not monitored") — equal roles are fine, so a bot with only `@everyone` can move plain members.
- `[p]micwatch status` is the debugging command: who is counted, who is skipped and why, where the bot sits versus the watched channels, how many voice frames arrived, the last voice error and a pending retry. `Voice frames` must show a non-zero *total* while connected — that proves the voice websocket is being read; a total that stays 0 while people talk means the bot is in the wrong channel or Discord relays nothing to it.
- The bot joins **muted but not deafened**: a deafened client receives no audio stream, and Discord's speaking events ride on that stream.
- After a move the member is ignored for `rearm` seconds, so moving them back does not instantly bounce them again. Do not put the target channel in the watch list.
- `mic` mode cannot tell talking from a silent open microphone: on push-to-talk users it reacts to the mute button, not to speech. Use `speak` mode when that matters.
- A `speak`-mode member who is **already** talking when the bot joins the channel is first picked up at their next start/stop — Discord only relays the transitions.
- The announcement text is capped at 500 characters, like every Discord message.

### osint

Passive reconnaissance for infrastructure and usernames — everything comes from public, key-free sources, nothing is scanned or probed. **No requirements**: it uses `aiohttp`, which ships with Red.

```
[p]osint lookup 8.8.8.8
[p]osint domain example.com
[p]osint subs github.com
[p]osint url https://example.com/login
```

| Command | What it does |
| --- | --- |
| `[p]osint lookup <target>` | Recognise the type by itself (IP, domain, URL, `AS…`, MAC, mail) and route it |
| `[p]osint ip <ip>` | Geolocation, ASN, open ports, hostnames, CVEs, network block, Tor/Spam flags |
| `[p]osint domain <name>` | RDAP registration, DNS records, SPF/DMARC verdict, HTTP security-header grade |
| `[p]osint subs <name>` | Subdomains from certificate transparency logs (certspotter → crt.sh → hackertarget) |
| `[p]osint url <url>` | Live headers, urlscan.io history, Wayback snapshots |
| `[p]osint asn <AS…\|ip>` | RIPEstat holder, announcement status and prefixes |
| `[p]osint mac <addr>` | OUI vendor plus locally-administered/multicast flags |
| `[p]osint gravatar <mail>` | Public Gravatar profile (only the MD5 hash is sent) |
| `[p]osint exif [url]` | EXIF incl. GPS, PNG text chunks, PDF author/producer, dimensions, MD5/SHA-1/SHA-256 |
| `[p]osint sources` | Every external service contacted, with its limits |
| `[p]osint status` | The settings for this server |
| `[p]osint set …` | Channels, cooldown, audit log, user-lookup limits |

Notes:

- Every report carries a **Rohdaten (JSON)** button with the raw payloads and a **Quellen** button. Both answer **ephemerally** — only the person who ran the command sees them — and say so if the data has expired after a restart.
- Targets are restricted to publicly routable infrastructure: private, loopback, CGNAT and reserved ranges are refused before any request is made, and redirect chains are re-validated hop by hop so a public URL cannot bounce the bot into the local network.
- Answers are cached for 30 minutes per target; `[p]osint set clearcache` drops that cache. Lookups can be limited to specific channels and throttled per member, and an audit channel can log every lookup.
- Sources and their limits are listed in-game by `[p]osint sources`; the EXIF command's own caveat is that Discord re-encodes uploaded images and drops their metadata, so use a URL to the original file for a photo.

### discogs

Search the [Discogs](https://www.discogs.com) database from Discord: releases, master
releases, artists and labels, with tracklists, credits, ratings and marketplace prices.
**No API key needed** — the API answers anonymously, and a free user token only raises the
rate limit from 25 to 60 requests per minute.

```
[p]discogs daft punk
[p]discogs search blue monday --format Vinyl --country UK --sort want
[p]discogs release 249504
[p]discogs artist aphex twin
[p]discogs random --genre Jazz --decade 60s
```

| Command | What it does |
| --- | --- |
| `[p]discogs <query>` | Quick release search |
| `[p]discogs search <query> [flags]` | Full search with filters (see below) |
| `[p]discogs release <id>` | One release: facts, tracklist, credits, identifiers, videos, rating, price |
| `[p]discogs master <id>` | The master release behind all pressings, with a **Versions** button |
| `[p]discogs artist <name>` | Artist profile with a **Releases** button (paginated catalogue) |
| `[p]discogs label <name>` | Label profile with a **Releases** button |
| `[p]discogs versions <master>` | Every pressing/version of a master release |
| `[p]discogs top [flags]` | The most wanted records, optionally by genre/style/year |
| `[p]discogs random [flags]` | Draw a random record |
| `[p]discogs token` | Store an optional Discogs user token (bot owner, secure form) |
| `[p]discogs status` | API state, rate-limit window, cache, this server's settings |
| `[p]discogs clearcache` | Drop the in-memory cache (bot owner) |
| `[p]discogs set ...` | Per-server settings (Manage Server) |

Search flags: `--type release|master|artist|label`, `--genre`, `--style`, `--year`,
`--country`, `--format`, `--label`, `--artist`, `--catno`,
`--sort year|artist|title|label|catno|format|rating|added|want`, `--limit 1-25`.
`top` and `random` take `--genre`, `--style`, `--year`, `--country` (`random` also
`--decade 60s|1990s`). Quote values that contain a space: `--style "Detroit Techno"`.

Notes:

- **Every result list is interactive**: the picker opens a detail view, the buttons page
  through results. Detail views page through long tracklists and profiles instead of
  truncating them, and carry an **Open on Discogs** link.
- **Genres vs. styles**: Discogs has 15 genres and hundreds of styles. Typing a style as a
  genre (`--genre Techno`) would return nothing, so the cog maps it and says so in the
  footer.
- **Rate limits are respected, not fought**: the client reads `X-Discogs-Ratelimit` from
  every response, keeps a sliding one-minute window and waits instead of collecting `429`s;
  responses are cached (5 min for searches, 30 min for entries) so paging back and forth
  costs nothing.
- **A missing record, an empty search or a slow API is an embed**, never a traceback.
- **Token (optional)**: create one at <https://www.discogs.com/settings/developers> and
  either press the button in `[p]discogs token` or run
  `[p]set api discogs token,<token>` — it takes effect immediately, no reload.
- **No requirements** — only what Red already ships.
- Settings per server: results per page (default 8), marketplace currency (default `EUR`,
  e.g. `CHF`), thumbnails on/off.
- Full manual: [`discogs/README.md`](discogs/README.md).

### idlerpg

The IRC classic [idlerpg.net](https://www.idlerpg.net/) as a Discord cog. Register a character and it levels purely by idling: every level-up finds an item, and the level-up may start a battle against anyone else present. No IRC server, no password — the Discord account *is* the login, and **every guild is its own realm** with its own settings.

```
[p]irpg set toggle            # turn this server's realm on
[p]irpg panel #games          # post the button panel
[p]irpg register Grimmwald Bard
```

The panel carries four buttons — **Join** (a modal asking for name and class), **Character**, **Leaderboard**, **Realm** — and answers ephemerally. It keeps working after a restart.

| Command | What it does |
| --- | --- |
| `[p]irpg register <name> [class]` | Create a character (name unique per server, 3–24 chars; class free-form, ≤ 30) |
| `[p]irpg rename <name>` | Rename your character |
| `[p]irpg unregister` | Delete your character (asks first) |
| `[p]irpg align <good\|neutral\|evil>` | Alignment: good +10 % item sum but rare crits, evil −10 % but crits at 1/20 |
| `[p]irpg char` | Character sheet: level, progress bar, all ten item slots, record |
| `[p]irpg top [page]` | Leaderboard, paginated |
| `[p]irpg realm` | Realm overview: figures, present players, top three, running quest |
| `[p]irpg quest` | Active quest, its members and remaining time |
| `[p]irpg panel [#channel]` | Post the panel (Manage Server) |
| `[p]irpg set …` | Every rule, per server (Manage Server) — see below |
| `[p]irpg admin hog\|level\|ttl\|remove` | Summon the Hand of God, set a level, shift time, delete a character |

Settings (`[p]irpg set <key> <value>`):

| Setting | Default | Meaning |
| --- | --- | --- |
| `toggle` | off | Enable the realm |
| `channel [#channel]` | none | Where announcements go (falls back to the system channel) |
| `role [@role]` | none | Role handed to registered players |
| `speed <factor>` | `1` | Global pace: `2` = twice as fast, `0.5` = half |
| `base <seconds>` | `300` | Base time for level 1 |
| `growth <factor>` | `1.14` | Growth per level |
| `maxlevel <n>` | `120` | Highest reachable level |
| `battles` / `npc` | on / on | Duels on level-up; fights against the realm's shadow when alone |
| `events` | on | Hand of God (1/20 per day), godsends and calamities (1/8 each), team battles |
| `quests` | on | Automatic quests for idle players |
| `uniques` | on | The eight unique items (1/40 per eligible level-up) |
| `penalty <seconds>` | `1` | Time cost per message character, `0` = talking is free |
| `penaltycap <chars>` | `60` | Characters of a message counted at most |
| `announce` | on | Announce level-ups, fights, quests and fate in the channel |
| `reset` | — | Wipe all figures and settings (asks first) |

Notes:

- **Levelling** follows the IRPG formula `base * growth ^ level`, linear after level 50 (`cap_extra`, one day per level by default). Ten item slots, item rolls weighted `1 / 1.4 ^ n` up to `1.5 × level`, battles compare the sum of item levels.
- **Talking costs time**, exactly like the original: a message adds `penalty × characters × 1.14 ^ level` seconds to your next level. Command invocations are free, so using the cog never hurts you. `[p]irpg set penalty 0` disables the whole mechanic for a chatty server.
- **Only presence counts.** Time accrues while the member is in the guild; leaving pauses the clock and keeps the character. `penalty_leave` (default off) can add a penalty for leaving.
- **Quests** start on their own once enough figures have the required level and idle time. A quest fails as soon as one participant is penalised (a message is enough), and then every present player pays the realm penalty. Succeed and the questers each drop 25 % of their remaining time.
- **Battles** only happen on a level-up: below level 25 with a configurable chance, from 25 on always. Wins cost the loser nothing unless a critical strike lands (1/50 good, 1/35 neutral, 1/20 evil); a win can also steal one better item.
- Data is written every 60 seconds and on unload, not on every tick, so a busy server does not hammer the config. `[p]mydata` returns your character as a file, `[p]mydata forgetme` deletes it.
- **No requirements** — only what Red already ships.
- Full manual, formulas and defaults: [`idlerpg/README.md`](idlerpg/README.md).

### dbd

Dead by Daylight information: killers with move speed and terror radius, survivors, perks
with all three tier values resolved, items, offerings and maps.

The cog folder is called **`deadbydaylight`** (`[p]cog install freak-cogs deadbydaylight`,
`[p]load deadbydaylight`), the command is `[p]dbd`.

```
[p]dbd killer nurse
[p]dbd survivor dwight
[p]dbd perk hex ruin
[p]dbd item toolbox
[p]dbd map greenville
[p]dbd list perks killer
[p]dbd search ruin
```

| Command | What it does |
| --- | --- |
| `[p]dbd killer <name>` | A killer: difficulty, gender, height, move speed, terror radius, personal perks, bio |
| `[p]dbd survivor <name>` | A survivor, same layout |
| `[p]dbd char <name>` | Either of the two, without the role filter |
| `[p]dbd perk <name>` | A perk with its description, owner, teachable level and categories |
| `[p]dbd item <name>` | An item: description, type, rarity |
| `[p]dbd offering <name>` | An offering: description, rarity, type, tags |
| `[p]dbd map <name>` | A map with its realm |
| `[p]dbd lore <name>` | The full backstory, paginated |
| `[p]dbd random [perks\|killers\|survivors]` | Roll a random entry |
| `[p]dbd search <text>` | Search every table at once |
| `[p]dbd list <killers\|survivors\|perks\|items\|offerings\|maps> [role]` | Browse a table, paginated |
| `[p]dbd stats` | Row counts, cache age and how many icons are mapped |
| `[p]dbd refresh` | Force a reload from the data source (bot owner) |

Notes:

- Names are matched loosely: case, punctuation, accents and `&`/`and` do not matter, so
  `hex ruin`, `Hex: Ruin`, `quick and quiet`, `alexs toolbox` and `onryo` all resolve.
  Matching is exact → prefix → whole word → substring, with minimum lengths so that `hex`
  does not hit *The Xenomorph*. An ambiguous query answers with a candidate list instead
  of guessing, an unknown one with the closest names as a hint.
- **Perk descriptions are rendered, not dumped.** The payload stores placeholders such as
  `{Tunable.S02P03.Haste%}` next to a `tunables` table; the cog substitutes them per tier,
  which is why Sprint Burst reads "50 % Haste for 2 s" and Hex: Ruin reads
  "100/125/150 %". Status effects and input prompts are spelled out the same way.
- **Killer numbers come from the game data**, not from text: `MaxWalkSpeed` 460 becomes
  "4.6 m/s (115 %)" against the 4.0 m/s survivor base, `TerrorRadius` 3200 becomes "32 m".
- Data source is the community JSON API behind <https://dbd.tricky.lol> (`/api/characters`,
  `/api/perks`, `/api/items`, `/api/offerings`, `/api/maps`) — no key, no login. The payload
  is cached in memory for 12 hours and on disk under the cog's data directory, so a restart
  or a short outage still answers; `[p]dbd refresh` forces a reload.
- Perk icons, survivor and killer portraits, items, offerings and maps all carry thumbnails
  from one public GitHub mirror (`Zawgalicious/DBDImages`), with a second perk mirror as a
  fallback. The file listing is read once every few days and a name is only linked when the
  file really exists; matching copes with the mirror's squashed and inconsistent file names
  (nicknames, dropped initials, possessive spellings, even a typo) and prefers no image over
  a wrong one. If GitHub is unreachable, everything still renders — just without images.
- A miss, an ambiguous query, a wrong-role request and an unreachable API are all embeds,
  never tracebacks.
- **No requirements** — `aiohttp` ships with Red. Nothing is stored per user, so the cog has
  no data-deletion surface.
- Full manual: [`deadbydaylight/README.md`](deadbydaylight/README.md).

### psychonaut

Psychoactive substance reference from [PsychonautWiki](https://psychonautwiki.org):
class, routes of administration with dose ranges, durations, bioavailability, tolerance,
addiction and toxicity notes, interaction warnings and the documented subjective effects.

```
[p]psychonaut lsd
[p]psychonaut dose ketamine insufflated
[p]psychonaut interactions mdma
[p]psychonaut class dissociative
```

| Command | What it does |
| --- | --- |
| `[p]psychonaut <name>` | Overview: class, street names, routes, tolerance, toxicity, addiction potential, intro and structure image |
| `[p]psychonaut dose <name> [route]` | Threshold/light/common/strong/heavy plus the full duration breakdown and bioavailability, one field per route |
| `[p]psychonaut effects <name>` | Subjective effects grouped as physical, visual, cognitive, auditory, multisensory, transpersonal, after effects |
| `[p]psychonaut interactions <name>` | Dangerous, unsafe and uncertain combinations |
| `[p]psychonaut search <text>` | Search canonical names and street names |
| `[p]psychonaut class [class]` | Every class with a count, or the substances inside one class |
| `[p]psychonaut random [class]` | Random substance, optionally from one class |
| `[p]psychonaut stats` | Index size and age, cached pages, cache size |
| `[p]psychonaut refresh` | Drop the cache and reload the index (bot owner) |

Notes:

- Aliases are honoured, so `acid`, `lucy`, `cid` and `tabs` all reach LSD, `keta` Ketamine,
  `2cb` 2C-B and `shrooms` Psilocybin mushrooms. Matching runs exact → compacted (spaces and
  punctuation ignored) → prefix from three characters → whole word → substring from four,
  and an ambiguous query answers with a candidate list instead of picking one.
- **Two sources, each where it is stronger.** `api.psychonautwiki.org` (GraphQL) supplies the
  structured data — classes, tolerance, toxicity, cross-tolerances and per-route dose ranges,
  bioavailability and duration breakdowns, plus the three interaction lists. The MediaWiki API
  at `/w/api.php` supplies what GraphQL exposes badly or not at all: the article intro, the
  structure image from `Template:SubstanceBox/<name>` (rendered to PNG) and the subjective
  effect lists. GraphQL's own `summary`, `effects` and `erowid` fields are deliberately unused —
  they return template noise, the substance's own name, or nothing.
- The index (373 substances, ~960 names and aliases) is cached for 7 days, detail pages for 30,
  in memory and on disk under the cog's data directory; a failed fetch serves the cached copy.
- Harm reduction reference only. Dosages are copied verbatim from the wiki with their units, and
  every dosing embed says so; the cog never advises a dose.
- **No requirements** — `aiohttp` ships with Red, no key, no login, nothing stored per user.
- Full manual: [`psychonaut/README.md`](psychonaut/README.md).
### sherlockosint

> **Fork / archive.** The original shipped without a real author name (`YourName`
> was never replaced) and its repository is gone. Archived here unchanged apart from
> the command alias and the metadata.

Username lookup across the site list of the
[Sherlock project](https://github.com/sherlock-project/sherlock). Hits are rendered
as paginated embeds.

```
[p]sherlock search torvalds
[p]sherlock quick torvalds
[p]sherlock site GitHub torvalds
```

| Command | What it does |
| --- | --- |
| `[p]sherlock search <username>` | Full search with live progress (aliases: `find`, `lookup`) |
| `[p]sherlock quick <username>` | One-shot search, found accounts only |
| `[p]sherlock site <site> <username>` | Check a single site |
| `[p]sherlock sites` | Loaded site count and a sample |
| `[p]sherlock reload` | Re-fetch the dataset |
| `[p]sherlock settings` | Current server configuration |
| `[p]sherlock set <key> <value>` | `enabled`, `cooldown` (0–300 s), `timeout` (3–30 s), `results_per_page`, `show_not_found`, `nsfw_filter` |
| `[p]sherlock addchannel\|removechannel\|clearchannels` | Channel whitelist |

Notes:

- The group alias is **`[p]sk`**. The original used `osint`, which collides with the
  `osint` cog in this repo — two cogs answering to the same command name means the
  second one refuses to load.
- The site list comes from `raw.githubusercontent.com/sherlock-project/sherlock` and
  is cached in memory; `[p]sherlock reload` fetches it again.
- Every check is a plain request to `https://<site>/<username>` from the bot host, so
  that IP is visible to the sites. Nothing is brute-forced.
- **No requirements** — `aiohttp` ships with Red.
- Full manual: [`sherlockosint/README.md`](sherlockosint/README.md).

### twitterfixer

> **Fork / archive.** Original by **tombly**; that repository has been deleted.
> Archived here so the code is not lost — only `info.json` was rewritten.

Turns Twitter and X links into embeds built from the vxtwitter API, with text,
author, date, likes, retweets and replies. Videos and GIFs are re-encoded with
ffmpeg; if the result is too large for the server, the message is replaced by a
`vxtwitter.com` link instead.

There are **no commands** — the cog is a listener. It skips bots, DMs and members
without *Embed Links*, and needs *Manage Messages* to delete its own placeholder.

Notes:

- Needs `ffmpeg` on the host (`apt install ffmpeg`) and the `ffmpy` package, which
  `[p]cog install` pulls in.
- Metadata comes from `api.vxtwitter.com`; nothing is stored on disk beyond
  temporarily downloaded media.
- The original repository carried no licence file. This mirror only preserves the
  code; rights stay with the original author.
- Full manual: [`twitterfixer/README.md`](twitterfixer/README.md).

## Extra setup

- **dbd** — nothing to configure. The host needs outbound HTTPS to `dbd.tricky.lol` and
  `api.github.com`/`raw.githubusercontent.com` (the latter only for images). The bot needs
  **Embed Links** in the channels where it is used.
- **vcstatus** — the bot needs the **Set Voice Channel Status** permission, plus **Manage Channels** while it is not connected to that channel.
- **kickalerts** — needs OAuth2 app credentials from https://kick.com/settings/developer. Store them with `[p]kickalert setcreds <client_id> <client_secret>` (or `[p]set api kick client_id,<id> client_secret,<secret>`) and verify with `[p]kickalert authstatus`. The bot needs **View Channel**, **Send Messages** and **Embed Links** in the alert channel.
- **voicesay** — needs **ffmpeg** and **ffprobe** on the host to convert audio that is not already OGG Opus.
- **micwatch** — needs the **Move Members** permission (plus **Connect** for `speak` mode). `speak` mode additionally needs voice support in the bot venv: `pip install -U "Red-DiscordBot[voice]"` (or `[p]pipinstall pynacl davey`), then `[p]reload micwatch`. `pynacl` does the voice transport encryption, `davey` the Discord E2EE (DAVE) session discord.py 2.6+ negotiates.
- **idlerpg** — nothing to install. The bot needs **Send Messages** and **Embed Links** in the announcement channel; `[p]irpg set role` additionally needs **Manage Roles**, with the bot's role above the one it hands out.
- **discogs** — nothing to install or configure. Optionally store a user token for a higher rate limit (see above). Data comes from api.discogs.com; the requests carry a `freak-cogs-discogs` user agent and the bot host's IP.
- **naminter** — nothing to configure, but `[p]cog install` pulls the `naminter` package (curl-cffi, orjson, jsonschema plus the upstream CLI extras: rich, uvloop, weasyprint), so the install step takes noticeably longer than the other cogs. Python 3.11 or newer is required, and the host needs outbound HTTPS to `raw.githubusercontent.com` and to the sites being checked.
- **sherlockosint** — nothing to install. Needs outbound HTTPS to `raw.githubusercontent.com` (site list) and to the sites being queried. The bot needs **Embed Links** in the channels where results are posted.
- **twitterfixer** — needs **ffmpeg** on the host for video and GIF re-encoding, and the **Manage Messages** permission so it can delete its own placeholder embed. Without *Manage Messages* the cog stays silent by design.

## Requirements

Red-DiscordBot 3.5.0+ and discord.py 2.x. **naminter** additionally needs Python 3.11+.

## Credits

- **DeepSeek** (`deepseek-v4.1-flash`) — pair-programmed the cogs.
- **tombly** — original author of TwitterFixer (repository deleted, archived here).
- **unnamed** — original author of SherlockOSINT (shipped without a real author name, repository gone).

## License

MIT — see [LICENSE](LICENSE).
