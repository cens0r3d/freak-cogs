# kickalerts

Kick.com livestream announcements for Red-DiscordBot, built on Kick's official
public API (`api.kick.com/public/v1`) with OAuth2 app credentials.

One message per stream: it appears when a monitored streamer goes live and is
refreshed while the stream runs. Streams are identified by their start time, so
a bot restart or a flapping API never double-posts.

When a stream ends, the cog posts a summary embed with the stream time, the peak
and the average viewer count (sampled on every poll), plus start and end time.
`[p]kickalert offlinepost` picks how that happens:

- `new` (default) — the live alert is removed and a fresh summary post appears.
- `edit` — the live alert itself turns into the end-of-stream embed, which then
  carries the same statistics. `[p]kickalert autodelete true` deletes it instead.

Setup:

```
[p]kickalert setcreds <client_id> <client_secret>
[p]kickalert setchannel #stream-alerts
[p]kickalert add <kick_username>
```

Credentials come from https://kick.com/settings/developer. `setcreds`
verifies them before saving, deletes the message that contained them, and
stores them in Red's shared `kick` token store — the same place
`[p]set api kick client_id,<id> client_secret,<secret>` writes to. Values from
the pre-3.0 config keys are migrated there automatically on first load.

## Clip announcements

The cog can also watch a channel's **clips** and post new ones. Kick's public API
has no clip list at all, so this half reads the web endpoint
`https://kick.com/api/v2/channels/<slug>/clips` — keyless, no OAuth token, newest
first, 20 clips per answer (the `limit` parameter is ignored). All other traffic
still goes through the official API.

```
[p]kickalert clips true
[p]kickalert clipchannel #clips
[p]kickalert clipsset <kick_username> false     # one streamer off
[p]kickalert clipinterval 300                   # 60-3600 s
```

- The **first check per streamer only records what already exists**, so enabling
  the feature never dumps an archive into the channel.
- Clips are deduplicated by id (last 30 per streamer), private clips are dropped,
  and a backlog is posted oldest-first.
- Channel order: `clipchannel`, then the streamer's own alert channel, then the
  default channel.
- Muted streamers and `clipsset … false` keep their clip list up to date, so
  unmuting later does not announce old clips.
- `[p]kickalert clipscheck <slug>` shows the five newest clips without posting.

### The clip itself as a file

Kick serves clips **only as HLS playlists** (`clips.kick.com/.../playlist.m3u8`,
segments are `.ts`); there is no direct MP4 - `video.mp4`, `clip.mp4` and
`source.mp4` all answer 403. With `[p]kickalert clipfiles true` (default on) the
cog therefore runs `ffmpeg -c copy` on the playlist and posts the resulting MP4
under the announcement, so Discord renders the video with its own download
button. Measured: a 10 s clip becomes 3.0 MB, a 45 s clip 9.5 MB (h264 720p,
~2.3 Mbit/s).

- `[p]kickalert clipmax <1-20>` - size ceiling per file in **MiB**, default 20,
  which is also Discord's upload limit for bots: asking for more only produces a
  failed send, so the setting is capped there. Checked against the guild's own
  upload limit too; anything larger is skipped and only the embed is posted.
- Conversions run one at a time (a backlog should not start ten ffmpeg processes),
  with a 180 s timeout and a 900 s duration cap.
- ffmpeg missing, a broken playlist, a timeout or a size overrun all cost only
  the file, never the announcement.
- Needs **Attach Files** in the target channel; without it the cog logs a warning
  and posts the embed alone.

The full command list, the placeholder set and the permission model
(**Manage Server** for everything except the owner-only `setcreds`) are in the
[repository README](../README.md#kickalerts).

