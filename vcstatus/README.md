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
| `[p]vcstatus resync` | Force a re-push to every voice channel — the first thing to try when a status looks stale |
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

## Permissions

- **Set Voice Channel Status** — the permission the status field itself needs.
- **Manage Channels** — additionally required while the bot is *not* connected
  to that channel. Since the cog does not join voice channels, that is the
  normal case.
- The bot needs no permission to *read* members; the count comes from the voice
  channel's cached member list.

A 403 in the log always means one of those two is missing for that channel
(usually because a category overwrites the permissions of the channel).

MIT — see the repository's [LICENSE](../LICENSE).
