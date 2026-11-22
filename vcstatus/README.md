# VCStatus

Puts a configurable status line on voice channels and keeps it in sync with how
many people are in them. It updates whenever a member joins, leaves or moves
between voice channels, so a channel can read `🎧 3 connected` without anyone
touching Discord's UI.

It can also show what the music bot is playing: `{track}`, `{author}` and
`{requester}` are filled from Red's Audio cog the moment a track starts or ends,
so a channel reads `🎵 Song — Artist` while something is playing and goes blank
(or falls back to your own idle text) when the queue runs dry.

```
[p]vcstatus default 🎧 {count} connected
[p]vcstatus channel #musik 🎵 {track} — {author}
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
| `[p]vcstatus fallback <text>` | What `{track}` shows while nothing is playing; without text it is cleared |
| `[p]vcstatus remove [#voice]` | Drop one channel's override; without a channel it resets the default message, the empty-channel setting, the fallback and every override |
| `[p]vcstatus empty [#voice] [on\|off\|reset]` | What an empty channel shows — server-wide (no channel) or per channel |
| `[p]vcstatus resync` | Force a re-push to every voice channel — the first thing to try when a status looks stale |
| `[p]vcstatus show` | Current configuration of this server, plus what is playing right now |

## Placeholders and limits

| Placeholder | Replaced with |
| --- | --- |
| `{count}` | Number of **humans** in the channel; bots are not counted |
| `{track}` | Title of the track the music bot is playing |
| `{author}` | Artist / channel of that track |
| `{requester}` | Whoever queued it |

- A message is capped at **500 characters** — Discord's limit for the status
  field. Longer input is refused with the length it would have had.
- The 500 limit applies to the **filled** text as well, because a track title is
  live input, not something you typed. An overlong title is cut off at 500.
- Any text is allowed, including emoji and formatting characters.
- Channels whose text has no track placeholder never touch the music bot and do
  not update when the track changes.

## Now playing

The track comes from the Audio cog that is already running on the same bot — the
cog asks Red's own player directly, so there is no second Lavalink connection,
no extra request and no password to configure. Updates are driven by the
`red_audio_track_start` / `_track_end` / `_queue_end` events and run through the
same two-second debounce as joins and leaves, so a skip only costs one request
per channel.

**Nothing is playing** is the interesting case:

- Without a fallback the whole status is **cleared**. That is deliberate: a text
  like `🎵 {track}` would otherwise leave a bare `🎵` behind as if it were
  content. Set a fallback if you want something to stay.
- With a fallback the placeholder is replaced by it, e.g.
  `[p]vcstatus fallback nichts läuft` turns `🎵 {track}` into `🎵 nichts läuft`.
- A channel the music bot sits in **alone** keeps its now-playing line even with
  empty-channel clearing on — otherwise the title would vanish as soon as the
  last listener left.
- No Audio cog, a dead Lavalink node or a disconnected player all count as
  "nothing playing"; none of them raise or spill into the log.

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
