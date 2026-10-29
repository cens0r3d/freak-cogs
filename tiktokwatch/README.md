# TikTokWatch

Announces TikTok livestreams and new clips in Discord — without an API key, a
login, a developer app or a request signature.

```
[p]repo add freak-cogs https://github.com/cens0r3d/freak-cogs
[p]cog install freak-cogs tiktokwatch
[p]load tiktokwatch
```

Install/load use **`tiktokwatch`**, `[p]help` shows **TikTokWatch**, and the
command is **`[p]tiktok`** (alias `[p]ttwatch`).

## Setup

Every account brings its own channel — there is deliberately no guild-wide
default channel.

```
[p]tiktok add @handle #alerts        # watch a creator, alerts go to #alerts
[p]tiktok list                       # what is watched, and where
[p]tiktok remove @handle
```

Pasting a profile or video URL works too.

## Commands

| Command | What it does |
| --- | --- |
| `[p]tiktok add <handle> <#channel>` | Watch a creator; all alerts for it go to that channel |
| `[p]tiktok remove <handle>` | Stop watching |
| `[p]tiktok list` | Watched accounts, live state and channel |
| `[p]tiktok toggle <handle>` | Mute or unmute one account (state keeps updating silently) |
| `[p]tiktok setchannel <handle> <#channel>` | Move one account's alerts |
| `[p]tiktok setrole <handle> [@role]` | Role pinged for that account (omit to clear) |
| `[p]tiktok message <handle> [text]` | Custom line above the alert; `{handle} {title} {url} {viewers} {started}` |
| `[p]tiktok interval live\|content <seconds>` | Poll interval (live 30–3600 s, clips 120–7200 s) |
| `[p]tiktok alerts live\|content\|all <on\|off>` | Turn a kind of announcement on or off |
| `[p]tiktok videos <on\|off>` | Also post the MP4 of a new clip (needs **Attach Files**) |
| `[p]tiktok videomax <1-20>` | Largest clip file to attach, in MiB |
| `[p]tiktok offline new\|edit\|delete` | What happens when a stream ends |
| `[p]tiktok settings` | Current configuration |
| `[p]tiktok check <handle>` | Probe TikTok right now (live state + latest clip) |
| `[p]tiktok test <handle>` | Post a sample live alert, nothing is stored |
| `[p]tiktok debug <handle>` / `force` | Raw data / run a pass now (owner only) |

Defaults: live alerts every 90 s, clip alerts every 600 s, both on, live embed
refreshed every 300 s, stream end posted as a fresh embed with the duration.

## Notes

- The first scan per account is **silent on purpose** — `add` primes the clip
  list, so enabling an account never dumps old videos into the channel.
- Some accounts answer with a stub room: live status but no title, cover or
  viewer count. The embed then omits the viewer field instead of printing a zero.
- All alerts need **Embed Links**; the clip file needs **Attach Files**. The size
  is checked before downloading, so a too-large clip costs only the file.
- Only guild-level configuration is stored (channel and role ids, intervals, last
  known live state, last 30 clip ids).

## Troubleshooting

| Symptom | Cause and fix |
| --- | --- |
| "no-channel", no alerts | The account's channel was deleted or never set: `[p]tiktok setchannel <handle> <#channel>` |
| "handle not found" in `list` | Typo, or the account cannot go live. Re-add it with the exact handle from the profile URL |
| Nothing posted, `settings` shows a back-off | TikTok is rate limiting; wait it out or raise the intervals |
| A clip was never announced | The first scan is silent by design; the clip list was primed at `add` time |
| Alerts work but no ping | `[p]tiktok setrole <handle> @role`; `@everyone` needs **Mention @everyone** in the channel |

If TikTok starts refusing requests, the cog parks itself for a growing window
(30 s × streak, capped at 15 min) instead of hammering. Proxies are not needed
for a handful of accounts polled once or twice a minute.
