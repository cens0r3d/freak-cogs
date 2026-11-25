# kickalerts

Kick.com livestream announcements. One message per stream: it appears when a
monitored streamer goes live and is refreshed while the stream runs, so a bot
restart never double-posts.

When a stream ends, a summary embed follows with the stream time, the peak and
the average viewer count. `[p]kickalert offlinepost` picks how:

- `new` (default) — the live alert is removed and a fresh summary post appears.
- `edit` — the live alert itself turns into the end-of-stream embed.
  `[p]kickalert autodelete true` deletes it instead.

Setup:

```
[p]kickalert setcreds <client_id> <client_secret>
[p]kickalert setchannel #stream-alerts
[p]kickalert add <kick_username>
```

Credentials come from https://kick.com/settings/developer. `setcreds` verifies
them before saving and deletes the message that contained them. They are stored
in Red's shared `kick` token store — the same place
`[p]set api kick client_id,<id> client_secret,<secret>` writes to.

## Clip announcements

The cog can also watch a channel's **clips** and post new ones.

```
[p]kickalert clips true
[p]kickalert clipchannel #clips
[p]kickalert clipsset <kick_username> false     # one streamer off
[p]kickalert clipinterval 300                   # 60-3600 s
```

- The **first check per streamer only records what already exists**, so enabling
  the feature never dumps an archive into the channel.
- Clips are deduplicated by id, private clips are dropped, and a backlog is
  posted oldest-first.
- Channel order: `clipchannel`, then the streamer's own alert channel, then the
  default channel.
- Muted streamers and `clipsset … false` keep their clip list up to date, so
  unmuting later does not announce old clips.
- `[p]kickalert clipscheck <slug>` shows the five newest clips without posting.

### The clip as a file

With `[p]kickalert clipfiles true` (default on) the clip is posted as a playable
MP4 under the announcement, so it has Discord's own player and download button.

- `[p]kickalert clipmax <1-20>` — size ceiling per file in **MiB**, default 20.
  Anything larger is skipped and only the embed is posted.
- A failed conversion costs only the file, never the announcement.
- Needs **Attach Files** in the target channel; without it the embed is posted
  alone.

## Discord events

The cog can put a **server event** into Discord's Events tab while a streamer is
live, so members see the stream without reading a channel.

```
[p]kickalert events true              # the feature, per server
[p]kickalert eventset <user> false    # opt a single streamer out
[p]kickalert eventtest <user>         # create one now, to check the look
```

- It is an **external** event: the name carries the stream, the description the
  title and category, the location the Kick channel link.
- It is created on go-live and **deleted when the stream ends** — an offline
  streamer leaves nothing behind. `[p]kickalert eventoffline complete` keeps it
  as a finished entry in the past events instead.
- Discord refuses a start time in the past, so the event starts **one minute**
  after the bot noticed the stream and turns *Active* on its own at that moment.
- `[p]kickalert eventhours <1-12>` (default 8) is the nominal length. Discord
  completes an external event at its end time, so a stream that runs longer
  pushes that time out on its own — a 12 hour stream never vanishes mid-stream.
- `[p]kickalert eventcover true|false` uses the stream thumbnail as cover image.
- `[p]kickalert eventname` / `eventdescription` take the same placeholders as a
  custom message (`{streamer}` `{title}` `{game}` `{category}` `{url}`).
  `reset` restores the defaults: `{streamer} is live on Kick` as the title, and
  the stream title followed by `category · link` as the description.
- Per streamer the cog stores `event_id`, `event_stream_key` and `event_end`.
  A restart or reload therefore adopts a running stream instead of creating a
  second event, and an event whose stream ended while the bot was down is
  removed on the first check.
- `[p]kickalert remove` and `[p]kickalert clear` delete the events they own;
  `[p]kickalert eventclear <user>` removes one by hand.

| Command | Argument | What it does |
|---|---|---|
| `events` | `true`/`false` | Enable Discord events for the server |
| `eventset` | `<user> <true/false>` | Turn them off for one streamer |
| `eventtest` | `<user>` | Create one event now (permissions + look) |
| `eventclear` | `<user>` | Delete the stored event |
| `eventhours` | `1-12` | Nominal length on the card |
| `eventname` | `<template>` / `reset` | Event title |
| `eventdescription` | `<template>` / `reset` | Event description |
| `eventcover` | `true`/`false` | Stream thumbnail as cover |
| `eventoffline` | `delete`/`complete` | What happens when the stream ends |

Run `eventtest` while the streamer is **live**: an event for an offline channel
is removed again at the next check.

## Permissions

**Manage Server** for everything except the owner-only `setcreds`. The alert
channel needs **View Channel**, **Send Messages** and **Embed Links**.

Discord events need **Create Events** on the bot's role — without it the cog
logs a warning once and posts nothing to the Events tab (`[p]kickalert settings`
shows `no Create Events`). Deleting an event the bot created itself works with
that same permission.
