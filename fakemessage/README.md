# fakemessage

Turns a member and a sentence into a screenshot of another app: Discord,
WhatsApp, Telegram, X, Twitch chat or Kick chat. Real avatar, real role colour,
the platform's own bubble and its own markdown.

```
/fakemessage member:@someone message:i never said that
/fakewhatsapp message:*nein* hab ich nicht geschrieben member:@someone outgoing:true
/faketweet message:https://nwo.monster ist down name:Drangsal handle:drangsalinator
[p]fakemessage @someone i never said that
```

## Commands

| Slash | Prefix | Renders |
| --- | --- | --- |
| `/fakemessage` | `[p]fakemessage` (`fakemsg`) | Discord message: role colour, BOT tag, mentions, spoilers, `code`. |
| `/fakewhatsapp` | `[p]fakewa` | WhatsApp chat: header, TODAY chip, bubble, time, blue ticks when outgoing. |
| `/faketelegram` | `[p]faketg` | Telegram chat: header, bubble, time and check marks. |
| `/faketweet` | `[p]faketweet` (`faketw`) | X post: name, handle, blue check, meta line, engagement row. |
| `/faketwitch` | `[p]faketwitch` | Twitch chat line(s): badge, coloured name, message. |
| `/fakekick` | `[p]fakekick` | Kick chat line(s): badge, brand-green name, message. |

Every command takes `message`, plus `member` (whose avatar and name) and `name`
to override. The chat commands add `time`; WhatsApp and Telegram also take
`outgoing`. The post adds `handle`, `verified`, `time`, `date` and the four
counts. Twitch and Kick take `colour` (#rrggbb) and `badge`
(none/mod/sub/vip/founder), and every line break becomes another chat line.

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
| The slash commands do not appear | Red parks new slash commands disabled: `[p]slash enablecog fakemessage`, then `[p]slash sync`. |
| "I could not draw that" | Pillow missing in the bot venv, or the log has the traceback: `[p]pipinstall pillow`. |
| Nothing is sent at all | The bot lacks Attach Files in that channel; `embed_links` is not enough. |
| Emoji show as plain symbols | The Twemoji/CDN request failed (no outbound network); cached emoji still render. |
| Avatar is a coloured letter | The avatar download failed; the rest of the picture still renders. |
| Bold or strike does not apply | Wrong marker for the platform: WhatsApp `*bold*`, Telegram `**bold**`, X has no markdown. |

## Ideas for later

Instagram DM, iMessage, Reddit comment, YouTube live chat, Steam chat - each is
one renderer in `cards.py` plus one thin command.
