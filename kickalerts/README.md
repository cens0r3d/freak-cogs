# kickalerts

Kick.com livestream announcements for Red-DiscordBot, built on Kick's official
public API (`api.kick.com/public/v1`) with OAuth2 app credentials.

One message per stream: it appears when a monitored streamer goes live, is
refreshed while the stream runs, and turns into a stream-ended embed with the
duration — or is deleted — when the stream stops. Streams are identified by
their start time, so a bot restart or a flapping API never double-posts.

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

The full command list, the placeholder set and the permission model
(**Manage Server** for everything except the owner-only `setcreds`) are in the
[repository README](../README.md#kickalerts).

