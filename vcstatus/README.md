# VCStatus

Puts a configurable status line on voice channels and keeps it in sync with how
many people are in them. It updates whenever a member joins, leaves or moves
between voice channels, so a channel can read `🎧 3 connected` without anyone
touching Discord's UI.

```
[p]vcstatus default 🎧 {count} connected
[p]vcstatus channel #gaming 🎮 {count} playing
[p]vcstatus toggle
```

## Install

```
[p]repo add freak-cogs https://github.com/cens0r3d/freak-cogs
[p]cog install freak-cogs vcstatus
[p]load vcstatus
```

No requirements beyond Red itself.

## Commands

Every command needs **Manage Channels** on the server.

| Command | What it does |
| --- | --- |
| `[p]vcstatus toggle` | Enable or disable the automatic status. Enabling re-pushes every channel once |
| `[p]vcstatus default <text>` | Status message for every voice channel that has no override |
| `[p]vcstatus channel <#voice> <text>` | Status message for one specific channel |
| `[p]vcstatus remove [#voice]` | Drop one channel's override; without a channel it resets the default message, the empty-channel setting and every override |
| `[p]vcstatus empty [#voice] [on\|off\|reset]` | What an empty channel shows — server-wide (no channel) or per channel |
| `[p]vcstatus resync` | Force a re-push to every voice channel |
| `[p]vcstatus show` | Current configuration of this server |

## Placeholder and limits

- `{count}` is replaced with the number of **humans** in the channel; bots are
  not counted.
- A message is capped at **500 characters** — Discord's limit for the status
  field. Longer input is refused with the length it would have had.
- Any text is allowed, including emoji and formatting characters.

## Empty channels

By default an empty channel keeps showing the message with a count of 0. With
the empty behaviour switched on, the status is **cleared** instead, so an empty
channel carries no text at all.

- `[p]vcstatus empty` toggles the server-wide default.
- `[p]vcstatus empty #lounge` toggles that channel's own setting.
- `[p]vcstatus empty #lounge on` / `off` sets it explicitly.
- `[p]vcstatus empty #lounge reset` drops the override, so the channel follows
  the server default again.

A per-channel setting always wins over the server-wide default.

## How it works

- Discord has no way to **read** a voice channel status back, and discord.py
  exposes no `voice_status` attribute. The bot therefore writes the status
  through the dedicated route (`PUT /channels/{id}/voice-status`, with a
  nullable `status` field, where `null` clears it) and remembers what it pushed
  in memory. An unchanged value is skipped, so idle channels cost no requests.
- Updates are **debounced by about two seconds**: a burst of joins, leaves and
  moves collapses into one request per channel. The route is rate limited per
  route, and a busy channel would otherwise hit it.
- A move updates **both** channels — the one left behind and the one joined.
- `resync` clears the remembered values and re-pushes everything, spaced a
  quarter second apart. It does not change any setting, which makes it the first
  thing to try when a status looks stale.
- Only channels where the bot has **Manage Channels** are touched; everything
  else is skipped silently.
- Failures never abort the handler: a rejected request is logged (a 403 names
  the missing permission) and the value is not cached, so the next event retries.
- Deleting a voice channel drops its cached entry and its queued update.

## Permissions

- **Set Voice Channel Status** — the permission the status field itself needs.
- **Manage Channels** — additionally required while the bot is *not* connected
  to that channel. Since the cog does not join voice channels, that is the
  normal case.
- The bot needs no permission to *read* members; the count comes from the voice
  channel's cached member list.

A 403 in the log always means one of those two is missing for that channel
(usually because a category overwrites the permissions of the channel).

## Data

Nothing is stored per user. Every setting is guild level and lives in Red's
Config: the enabled flag, the default message, the empty-channel default, the
per-channel messages and the per-channel empty flags. The status cache and the
debounce timers exist only in memory and are gone after a restart — the next
join, leave or `[p]vcstatus resync` fills them again.

## Files

| File | Contents |
| --- | --- |
| `vcstatus.py` | The whole cog: config, commands, listener, debounce and the raw status write |
| `info.json` | Cog metadata |

## Credits

MIT — see the repository's [LICENSE](../LICENSE).
