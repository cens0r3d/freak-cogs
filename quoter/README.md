# quoter

Reply to a message and the bot answers with an inspirational quote card: the
author's avatar in a coloured ring, the text in a serif face, the attribution
below and an optional watermark, on a gradient that is derived from that
avatar's own colour.

This is the Red equivalent of the [Equicord Quoter
plugin](https://github.com/Equicord/Equicord/tree/main/src/equicordplugins/quoter):
same idea, same three switches (grayscale, watermark, theme), plus a message
context menu.

The cog folder is `quoter`, the cog is called `quoter`, and the command is
`[p]quote` (alias `[p]quoter`).

## Install

```
[p]repo add freak-cogs https://github.com/cens0r3d/freak-cogs
[p]cog install freak-cogs quoter
[p]load quoter
```

The host needs Pillow; if the cog reports it missing, run `[p]pipinstall pillow`
and `[p]reload quoter`. The bot needs **Attach Files** in the channel it posts to.

### The right-click entry (optional)

The message menu entry `Apps -> Quote` is an application command, and Red keeps
new application commands disabled until an owner enables them:

```
[p]slash enablecog quoter
[p]slash sync
```

After the sync, restart the Discord client once (Ctrl+R) so the new entry shows
up. The prefix command works without any of this.

## Commands

| Command | What it does |
| --- | --- |
| `[p]quote` | Quotes the message you replied to. |
| `[p]quote <message link or id>` | Quotes any message the bot can read. |
| `[p]quote set` / `[p]quote set show` | Shows the server's card defaults. |
| `[p]quote set font <serif\|sans\|light\|mono>` | Face of the quote text. |
| `[p]quote set theme <dark\|light>` | Card theme. |
| `[p]quote set grayscale <on\|off>` | Desaturated avatar and background by default. |
| `[p]quote set watermark <on\|off>` | Stamp a small line at the bottom by default. |
| `[p]quote set watermarktext <text>` | Text for the watermark; leave it out to clear (max 40 characters). |
| `[p]quote set accent <#hex\|auto>` | Fix the accent colour, or follow the avatar again. |

Everything under `[p]quote set` needs **Manage Server**.

## Options per card

Options go after the link or reply and override the server defaults for that
one card:

| Option | Effect |
| --- | --- |
| `--light` / `--dark` | Light or dark background. |
| `--gray` / `--colour` | Grayscale (avatar included) or colour. |
| `--watermark` / `--no-watermark` | Watermark on or off. |
| `--font serif\|sans\|light\|mono` | Quote face. |
| `--accent #5865f2` | Fixed accent colour. |

Examples:

```
[p]quote
[p]quote --light --font sans
[p]quote https://discord.com/channels/<guild>/<channel>/<message> --gray --watermark
```

The bot replies to the quoted message, so the card and its source sit together.

## How a card is drawn

- **Canvas**: 1080 px wide, height grows with the text (12 lines at the largest
  face, then the font steps down from 62 px to 34 px before the canvas gets
  taller). A short quote stays square.
- **Colour**: the avatar is averaged to one pixel, then saturation and
  lightness are lifted so a muddy photo still reads as a colour. That colour
  drives the background gradient, the ring and the quote mark. `--gray` drops
  it to grey, `--accent` replaces it with a fixed hex value.
- **Faces**: `serif` is Liberation Serif (Times-like), `sans` and `light` are
  Lato, `mono` is DejaVu Sans Mono. Missing fonts fall back to DejaVu and then
  to Pillow's built-in face, so the cog keeps drawing on a minimal host.
- **Text cleanup**: Discord markdown is stripped (`**`, `*`, `_`, `~~`,
  backticks, spoilers, `>` quotes, `#` headings), links keep their label,
  `<@id>` becomes the member's display name, `<@&id>` the role name, `<#id>`
  the channel name and custom emoji become `:name:`. Text past 600 characters
  is cut at a word boundary.
- **No emoji glyphs**: the bundled faces have no colour emoji, and Pillow does
  no font fallback, so Unicode emoji are removed instead of drawn as empty
  boxes. Same for scripts the installed fonts do not cover (CJK on a host
  without a CJK font).
- **No avatar**: the circle shows the author's initials.

## Troubleshooting

| Symptom | Cause and fix |
| --- | --- |
| `Pillow is missing on the bot host` | `[p]pipinstall pillow`, then `[p]reload quoter`. |
| `I need the Attach Files permission` | The bot needs **Attach Files** in that channel. |
| `That message has no text to quote` | The message is an attachment, sticker, embed or only emoji. Quoter draws text, like the Equicord plugin does. |
| `I could not find that message` | Use a message link (`Copy Message Link`), an ID from the same channel, or reply to the message. The bot must be able to read that channel. |
| `Apps -> Quote` is missing | `[p]slash enablecog quoter` and `[p]slash sync` as owner, then Ctrl+R in the client. |
| Quote text looks wrong | Markdown is stripped on purpose. Mentions are resolved from the server cache, so a member who left shows `@user`. |
| Emoji disappeared | Expected: see *No emoji glyphs* above. |
| Card ignores the server defaults | Per-card options win. Check `[p]quote set show`. |

## Data

The cog stores per-server presentation defaults only (font, theme, grayscale,
watermark on/off and text, accent) in Red's config. No user data is written;
avatars are fetched while a card is drawn and are not cached to disk.
