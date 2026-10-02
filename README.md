# freak-cogs

Red-DiscordBot cogs.

## Install

```
[p]repo add freak-cogs https://github.com/cens0r3d/freak-cogs
[p]cog install freak-cogs
[p]load vcstatus kickalerts voicesay namint micwatch
```

Or install one at a time:

```
[p]cog install freak-cogs vcstatus
[p]cog install freak-cogs kickalerts
[p]cog install freak-cogs voicesay
[p]cog install freak-cogs namint
[p]cog install freak-cogs micwatch
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

### kickalerts

Announces Kick.com livestreams. Talks to Kick's official public API (`api.kick.com/public/v1`) with OAuth2 app credentials, polls per guild, and keeps one message per stream: it appears when the streamer goes live, gets refreshed while the stream runs, and turns into a "stream ended" message with the duration — or is deleted — when the stream stops.

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
| `[p]kickalert autodelete <true\|false>` | Delete the alert when the stream ends instead of editing it |
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
- The poll loop backs off on rate limits (HTTP 429) and API errors and never dies from them. If the alert channel is gone or the bot lacks **Send Messages** / **Embed Links** there, the alert is skipped and retried on the next tick, so nothing is lost while permissions are fixed.
- Deleting the alerts channel while it is configured clears the reference automatically.
- Upgrading from 2.x: existing settings and monitored streamers are kept. Credentials stored with the old config keys are moved into Red's shared `kick` token store on the first start, and the old keys are cleared.

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
-a, --all                also list misses, unknowns and errors
```

Under the summary of every lookup there is an **Export** button. Pressing it opens a small form that asks for the format (`JSON`, `CSV`, `TXT`) and the scope (*hits only* or *everything*, i.e. including misses, unknown and errors), and replies with the file **ephemerally** — only the person who started the lookup sees it, nothing is posted to the channel. If the API refuses the modal (selects in a modal need components-v2 support), the same two choices appear as a picker message with a **Send file** button instead. The button carries that run's results and stops working after ten minutes or a bot restart, and then tells you so; `--export` stays the non-interactive path for scripts.

Notes:

- Lookups are open to **every member** by default. `[p]naminterset role add @role` restricts them to that role plus server managers, `[p]naminterset role clear` reopens them to everyone. Per-user cooldown and a global concurrency cap apply either way.
- The dataset (~1 MB JSON) is downloaded in the background on load, cached on disk and refreshed after 6 hours; `[p]naminter refresh` forces it.
- Requests are made with browser impersonation (curl-cffi) from the bot's host, so its IP is visible to the sites being checked.
- Only the queried username leaves the host; nothing about it is stored. Hits are not cached or logged.
- Found / partially found / ambiguous results are shown by default, misses and errors only with `--all` or `[p]naminterset showmissing true`.
- `--export` writes every result (site, category, status, URL, HTTP code, timing, error) to a file — use it when a run hits the page cap or the run timeout.
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

## Extra setup

- **vcstatus** — the bot needs the **Set Voice Channel Status** permission, plus **Manage Channels** while it is not connected to that channel.
- **kickalerts** — needs OAuth2 app credentials from https://kick.com/settings/developer. Store them with `[p]kickalert setcreds <client_id> <client_secret>` (or `[p]set api kick client_id,<id> client_secret,<secret>`) and verify with `[p]kickalert authstatus`. The bot needs **View Channel**, **Send Messages** and **Embed Links** in the alert channel.
- **voicesay** — needs **ffmpeg** and **ffprobe** on the host to convert audio that is not already OGG Opus.
- **micwatch** — needs the **Move Members** permission (plus **Connect** for `speak` mode). `speak` mode additionally needs voice support in the bot venv: `pip install -U "Red-DiscordBot[voice]"` (or `[p]pipinstall pynacl davey`), then `[p]reload micwatch`. `pynacl` does the voice transport encryption, `davey` the Discord E2EE (DAVE) session discord.py 2.6+ negotiates.
- **naminter** — nothing to configure, but `[p]cog install` pulls the `naminter` package (curl-cffi, orjson, jsonschema plus the upstream CLI extras: rich, uvloop, weasyprint), so the install step takes noticeably longer than the other cogs. Python 3.11 or newer is required, and the host needs outbound HTTPS to `raw.githubusercontent.com` and to the sites being checked.

## Requirements

Red-DiscordBot 3.5.0+ and discord.py 2.x. **naminter** additionally needs Python 3.11+.

## Credits

- **DeepSeek** (`deepseek-v4.1-flash`) — pair-programmed the cogs.

## License

MIT — see [LICENSE](LICENSE).
