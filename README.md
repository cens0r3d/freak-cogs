# freak-cogs

Red-DiscordBot cogs.

## Install

```
[p]repo add freak-cogs https://github.com/cens0r3d/freak-cogs
[p]cog install freak-cogs
[p]load vcstatus kickalerts voicesay
```

Or install one at a time:

```
[p]cog install freak-cogs vcstatus
[p]cog install freak-cogs kickalerts
[p]cog install freak-cogs voicesay
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
| `[p]vcstatus show` | Show the current config |

Example:

```
[p]vcstatus default 🎧 {count} connected
[p]vcstatus channel #gaming 🎮 {count} playing
[p]vcstatus toggle
```

Notes:

- Only runs if the bot has **Manage Channels** on the channel.
- Messages are capped at 500 characters (Discord's limit).
- `{count}` counts humans only, bots are ignored.

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

## Requirements

Red-DiscordBot 3.5.0+ and discord.py 2.x.
