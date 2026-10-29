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

## Permissions

**Manage Server** for everything except the owner-only `setcreds`. The alert
channel needs **View Channel**, **Send Messages** and **Embed Links**.
