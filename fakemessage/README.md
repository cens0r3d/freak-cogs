# fakemessage

Turns one member and one sentence into a Discord dark-theme screenshot: real
avatar, real role colour, BOT tag, markdown, mentions, links and emoji.

```
/fakemessage member:@someone message:i never said that
[p]fakemessage @someone i never said that
```

## Commands

| Command | What it does |
| --- | --- |
| `/fakemessage member message [name] [timestamp] [bot_tag]` | Renders the message and posts it as `message.png`. |
| `[p]fakemessage <member> <message>` (alias `fakemsg`) | Same picture, without the optional overrides. |

Slash options: `name` shows a username other than the member's, `timestamp`
replaces `Today at HH:MM` (e.g. `Yesterday at 23:12`), `bot_tag` adds the BOT
badge. Five pictures per minute per user; 2000 characters per message.

## What ends up in the picture

Avatar and role colour come from the member, so the result matches the person
in this server. Markdown is rendered, not printed: `**bold**`, `*italic*`,
`__underline__`, `~~strike~~`, `` `code` ``, `||spoiler||`, `@mention`, `#channel`
and bare links. Emoji are Twemoji - the same set the client draws - and custom
emoji from `<:name:id>` are fetched from Discord's CDN.

## Setup

1. `[p]load fakemessage`, then `[p]slash enablecog fakemessage` and `[p]slash sync`.
2. Give the bot **Attach Files** in the channel.
3. Pillow has to be in the bot's venv: `[p]pipinstall pillow`.

Emoji pictures are cached under the cog's data path, so a repeat render costs
no requests and an offline bot still draws what it has seen.

## Troubleshooting

| Symptom | Cause |
| --- | --- |
| `/fakemessage` does not appear in Discord | Red parks new slash commands disabled: `[p]slash enablecog fakemessage`, then `[p]slash sync`. |
| "I could not draw that message" | Pillow missing in the bot venv, or the log has the traceback: `[p]pipinstall pillow`. |
| Nothing is sent at all | The bot lacks Attach Files in that channel; `embed_links` is not enough. |
| Emoji show as plain symbols | The Twemoji/CDN request failed (no outbound network); cached emoji still render. |
| Avatar is a coloured letter | The avatar download failed; the rest of the picture still renders. |
| Need a person who is not on the server | Pass `member` as any member and override `name`, or use `name` with a placeholder avatar. |
