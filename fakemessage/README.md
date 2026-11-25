# fakemessage

Turns a member and a sentence into a screenshot of another app: Discord,
WhatsApp, Telegram, X, Facebook, Reddit, Twitch chat or Kick chat. Real avatar,
real role colour, the platform's own bubble and its own markdown.

```
/fakemessage service:whatsapp message:*nein* hab ich nicht geschrieben member:@someone outgoing:true
/fakemessage service:x message:das war ich nicht name:Max handle:max
[p]fakemessage whatsapp *nein* hab ich nicht geschrieben
[p]fakemessage discord @someone i never said that
```

## Commands

| Command | What it does |
| --- | --- |
| `/fakemessage service message [member] [name] [timestamp] [outgoing] [handle] [verified] [colour] [badge]` | Renders one card and posts it as `message.png`. |
| `[p]fakemessage <service> [member] <text>` (alias `fake`) | Same card, from you; `discord` wants a member after the service. |

Five pictures per minute per user, 2000 characters per message.

## Services

| `service` | Looks like | Extra options |
| --- | --- | --- |
| `discord` | Message row: role colour, BOT tag, mentions, spoilers, `code`. | `member` (required), `timestamp` |
| `whatsapp` | Chat header, TODAY chip, bubble, time, blue ticks. | `member`, `name`, `timestamp`, `outgoing` |
| `telegram` | Chat header, bubble, time, check marks. | `member`, `name`, `timestamp`, `outgoing` |
| `x` | Post: handle, blue check, meta line, engagement row. | `member`, `name`, `handle`, `verified`, `timestamp` |
| `twitch` | Chat line(s) with badge and coloured name. | `member`, `name`, `colour`, `badge` |
| `kick` | Chat line(s) with badge, brand-green name. | `member`, `name`, `colour`, `badge` |
| `facebook` | Post: header with globe and menu, reaction bubbles, Like/Comment/Share bar. | `member`, `name`, `timestamp` |
| `reddit` | Comment: thread rail, u/name, vote pill with arrows, Reply/Share/Save. | `member`, `name`, `timestamp` |

`colour` is `#rrggbb`, `badge` is none/mod/sub/vip/founder, and every line break
becomes another chat line on Twitch and Kick.

## Markdown, per platform

WhatsApp uses `*bold*`, `_italic_`, `~strike~` and `` `mono` ``; Telegram uses
`**bold**`, `__italic__`, `~~strike~~` and backticks; X and the chat platforms
render text as typed. Emoji are Twemoji, the set the client itself draws, and
custom emoji from `<:name:id>` come from Discord's CDN.

## Setup

1. `[p]load fakemessage`, then `[p]slash enablecog fakemessage` and `[p]slash sync`.
2. Give the bot **Attach Files** in the channel.
3. Pillow has to be in the bot's venv: `[p]pipinstall pillow`.

Emoji pictures are cached under the cog's data path, so a repeat render costs no
requests and an offline bot still draws what it has seen.

## Troubleshooting

| Symptom | Cause |
| --- | --- |
| The slash command does not appear | Red parks new slash commands disabled: `[p]slash enablecog fakemessage`, then `[p]slash sync`. |
| "I could not draw that" | Pillow missing in the bot venv, or the log has the traceback: `[p]pipinstall pillow`. |
| Nothing is sent at all | The bot lacks Attach Files in that channel; `embed_links` is not enough. |
| Emoji show as plain symbols | The Twemoji/CDN request failed (no outbound network); cached emoji still render. |
| Avatar is a coloured letter | The avatar download failed; the rest of the picture still renders. |
| Bold or strike does not apply | Wrong marker for the platform: WhatsApp `*bold*`, Telegram `**bold**`, X has no markdown. |

## Ideas for later

Instagram DM, iMessage, YouTube live chat, Steam chat, Discord thread reply -
each is one renderer in `cards.py` plus one `service` entry.
