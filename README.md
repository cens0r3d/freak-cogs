# freak-cogs

Red-DiscordBot cogs.

## Install

```
[p]repo add freak-cogs https://github.com/cens0r3d/freak-cogs
[p]cog install freak-cogs
[p]load vcstatus kickalerts voicesay naminter
```

Or install one at a time:

```
[p]cog install freak-cogs vcstatus
[p]cog install freak-cogs kickalerts
[p]cog install freak-cogs voicesay
[p]cog install freak-cogs naminter
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

Posts an embed when a monitored Kick.com streamer goes live. Uses Kick's official API v2 with OAuth2 client credentials.

Setup:

```
[p]kickalert setcreds <client_id> <client_secret>
[p]kickalert setchannel #stream-alerts
[p]kickalert add <kick_username>
```

Grab API credentials at https://kick.com/settings/developer.

| Command | What it does |
| --- | --- |
| `[p]kickalert setcreds <client_id> <client_secret>` | Store API credentials (bot owner only) |
| `[p]kickalert authstatus` | Check whether the API auth works |
| `[p]kickalert add <username> [#channel]` | Start monitoring a streamer |
| `[p]kickalert remove <username>` | Stop monitoring a streamer |
| `[p]kickalert list` | Show all monitored streamers |
| `[p]kickalert setchannel <#channel>` | Default alert channel |
| `[p]kickalert setrole <@role> [username]` | Ping role, globally or per streamer |
| `[p]kickalert message <username> <text>` | Custom alert message — `{streamer}` `{game}` `{title}` `{url}` `{viewers}` |

There's more: `removerole`, `interval`, `style`, `autodelete`, `timezone`, `toggleviewers`, `togglecategory`, `test`, `check`, `debug`, `settings`, `clear`, `force`. See `[p]help kickalert`.

Requires `aiohttp`, which `[p]cog install` handles.

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
| `[p]naminterset role <add\|remove\|clear\|list> [@role]` | Roles allowed to run lookups |
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

Notes:

- Lookups are limited to **Manage Server** holders and the bot owner. Grant a role access with `[p]naminterset role add @role`.
- The dataset (~1 MB JSON) is downloaded in the background on load, cached on disk and refreshed after 6 hours; `[p]naminter refresh` forces it.
- Requests are made with browser impersonation (curl-cffi) from the bot's host, so its IP is visible to the sites being checked.
- Only the queried username leaves the host; nothing about it is stored. Hits are not cached or logged.
- Found / partially found / ambiguous results are shown by default, misses and errors only with `--all` or `[p]naminterset showmissing true`.
- `--export` writes every result (site, category, status, URL, HTTP code, timing, error) to a file — use it when a run hits the page cap or the run timeout.

## Extra setup

- **vcstatus** — the bot needs the **Set Voice Channel Status** permission, plus **Manage Channels** while it is not connected to that channel.
- **kickalerts** — needs OAuth2 client credentials from https://kick.com/settings/developer. Store them with `[p]kickalert setcreds <client_id> <client_secret>` and verify with `[p]kickalert authstatus`.
- **voicesay** — needs **ffmpeg** and **ffprobe** on the host to convert audio that is not already OGG Opus.
- **naminter** — nothing to configure, but `[p]cog install` pulls the `naminter` package (curl-cffi, orjson, jsonschema plus the upstream CLI extras: rich, uvloop, weasyprint), so the install step takes noticeably longer than the other cogs. Python 3.11 or newer is required, and the host needs outbound HTTPS to `raw.githubusercontent.com` and to the sites being checked.

## Requirements

Red-DiscordBot 3.5.0+ and discord.py 2.x. **naminter** additionally needs Python 3.11+.

## Credits

- **DeepSeek** (`deepseek-v4.1-flash`) — pair-programmed the cogs.

## License

MIT — see [LICENSE](LICENSE).
