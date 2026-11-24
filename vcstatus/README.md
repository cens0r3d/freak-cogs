# VCStatus

Puts a configurable status line on voice channels and keeps it in sync with how
many people are in them. It updates whenever a member joins, leaves or moves
between voice channels, so a channel can read `🎧 3 connected` without anyone
touching Discord's UI.

Each channel has **two states, each with its own text**:

- **Audio** — the music bot is playing *in that channel*: the audio text, filled
  with the current title (`🎵 Song — Artist`).
- **Default** — anything else: the normal `{count}` text (`🎧 3 connected`).

The whole template is swapped between the two states, so nothing of the audio
text can survive into the idle state. When the music stops, the channel simply
shows its normal text again (or nothing, if you clear empty channels).

```
[p]vcstatus default 🎧 {count} connected
[p]vcstatus audio 🎵 {track} — {author}
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
| `[p]vcstatus default <text>` | Status message for every voice channel that has no override — the idle text |
| `[p]vcstatus audio <text>` | Status message shown while the music bot plays in that channel; without text the feature is switched off |
| `[p]vcstatus channel <#voice> <text>` | Status message for one specific channel |
| `[p]vcstatus remove [#voice]` | Drop one channel's override; without a channel it resets the default message, the audio text, the empty-channel setting and every override |
| `[p]vcstatus empty [#voice] [on\|off\|reset]` | What an empty channel shows — server-wide (no channel) or per channel |
| `[p]vcstatus resync` | Force a re-push to every voice channel — the first thing to try when a status looks stale |
| `[p]vcstatus show` | Current configuration of this server, plus what is playing right now |

## Placeholders and limits

`{count}` works everywhere. The track placeholders only work in the **audio**
text — `default` and `channel` refuse them with a pointer to `vcstatus audio`,
because a half-filled track placeholder in the idle text is exactly what leaves
a bare `🎵` behind.

| Placeholder | Where | Replaced with |
| --- | --- | --- |
| `{count}` | everywhere | Number of **humans** in the channel; bots are not counted |
| `{track}` | audio text | Title of the track the music bot is playing |
| `{author}` | audio text | Artist / channel of that track |
| `{requester}` | audio text | Whoever queued it |

- A message is capped at **500 characters** — Discord's limit for the status
  field. Longer input is refused with the length it would have had.
- The 500 limit applies to the **filled** text as well, because a track title is
  live input, not something you typed. An overlong title is cut off at 500.
- With no audio text configured, nothing ever touches the music bot.

## How the two states are chosen

```
music bot playing in channel X  ->  channel X shows the audio text
everything else                 ->  the channel's own text / the default text
```

- Only the channel the bot is actually sitting in switches to the audio text.
  Other channels keep their normal text, even while music plays.
- A player that is loaded but paused still counts as playing — Red sends no
  event for pause/resume, so the status would otherwise be stuck either way.
- The bot sitting **alone** in its channel keeps the audio text even with
  empty-channel clearing on; otherwise the title would vanish the moment the
  last listener left.
- No Audio cog, a dead Lavalink node or a disconnected player all count as
  "nothing playing"; none of them raise or spill into the log.

## Where the track comes from

Red's Audio cog already runs on this bot, so VCStatus asks Red's own player
(`lavalink.get_player(guild_id)`) instead of opening a second Lavalink
connection — no password, no extra request. Updates are driven by the
`red_audio_track_start` / `_track_end` / `_queue_end` / `_audio_disconnect`
events and run through the same two-second debounce as joins and leaves, so a
skip costs at most one request per channel.

## Empty channels

By default an empty channel keeps showing the message with a count of 0. With
the empty behaviour switched on, the status is **cleared** instead, so an empty
channel carries no text at all. The audio state is exempt while a track is
loaded, see above.

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
