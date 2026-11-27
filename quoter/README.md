# quoter

Reply to a message and the bot answers with a quote card. Two layouts:

- **gradient** (default) - a square card with the author's avatar in a coloured
  ring, a gradient derived from that avatar's colour, the quote in a serif face
  and the attribution below. This is the Red version of the [Equicord Quoter
  plugin](https://github.com/Equicord/Equicord/tree/main/src/equicordplugins/quoter).
- **split** - the wide format people post on Discord: the author's avatar on
  one side, a solid panel on the other with the quote in a bold sans face, an
  italic `- name` and the `@handle` in grey.

Everything about both layouts is per-server configurable, and a single card can
override the defaults with flags.

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
| `[p]quote set style <gradient\|split>` | Card layout. |
| `[p]quote set side <left\|right>` | Which side the picture takes in a split card. |
| `[p]quote set font <serif\|sans\|light\|mono>` | Face of the quote in a gradient card. |
| `[p]quote set theme <dark\|light>` | Card theme. |
| `[p]quote set grayscale <on\|off>` | Desaturated picture/avatar and background by default. |
| `[p]quote set watermark <on\|off>` | Stamp a small line at the bottom by default. |
| `[p]quote set watermarktext <text>` | Text for the watermark; leave it out to clear (max 40 characters). |
| `[p]quote set accent <#hex\|auto>` | Fix the accent colour of a gradient card, or follow the avatar again. |

Everything under `[p]quote set` needs **Manage Server**.

## Options per card

Options go after the link or reply and override the server defaults for that
one card:

| Option | Effect |
| --- | --- |
| `--split` / `--gradient` | Switch the layout for this card. |
| `--flip` | Put the picture on the other side than the server default. |
| `--light` / `--dark` | Light or dark panel. |
| `--gray` / `--colour` | Grayscale (picture and avatar included) or colour. |
| `--watermark` / `--no-watermark` | Watermark on or off. |
| `--font serif\|sans\|light\|mono` | Quote face. |
| `--accent #5865f2` | Fixed accent colour (gradient cards). |

Examples:

```
[p]quote
[p]quote --light --font sans
[p]quote --split
[p]quote --split --flip --gray
[p]quote https://discord.com/channels/<guild>/<channel>/<message> --split
```

The bot replies to the quoted message, so the card and its source sit together.

## The picture in a split card

A split card shows the **avatar of the member who wrote the quoted message**,
fetched at 1024 px, cropped to fill its half and blended into the panel. There
is no option to pass a different picture: the card always belongs to its author.

If that avatar cannot be fetched (CDN hiccup, deleted account), the card falls
back to the gradient layout when `split` is only the server default. An explicit
`--split` answers with a hint instead of silently drawing the other layout.

## How a card is drawn

**gradient**

- **Canvas**: 1080 px wide, height grows with the text (12 lines at the largest
  face, then the font steps down from 62 px to 34 px before the canvas gets
  taller). A short quote stays square.
- **Colour**: the avatar is averaged to one pixel, then saturation and
  lightness are lifted so a muddy photo still reads as a colour. That colour
  drives the background gradient, the ring and the quote mark. `--gray` drops it
  to grey, `--accent` replaces it with a fixed hex value.
- **Faces**: `serif` is Liberation Serif (Times-like), `sans` and `light` are
  Lato, `mono` is DejaVu Sans Mono. Missing fonts fall back to DejaVu and then
  to Pillow's built-in face, so the cog keeps drawing on a minimal host.
- **No avatar**: the circle shows the author's initials.

**split**

- **Canvas**: 1200 px wide, panel 600 px, picture 600 px. Height starts at
  600 px and grows with the text; the quote face steps down from 40 px to 24 px
  before the card gets taller.
- **Picture**: the author's avatar, fetched at 1024 px, cropped to fill its half
  (cover crop, centred), then blended into the panel with a 130 px fade so there
  is no hard edge.
- **Type**: the quote in Lato Bold (the `font` setting is ignored here unless
  you pass `--font`), the author as italic `- Display Name`, the handle in grey
  below it. An explicit `--font serif|mono|light` overrides the bold sans.
- **Panel**: near-black `#0b0b0d` on the dark theme, near-white `#f6f6f8` on the
  light theme.

**Both layouts** clean the text first: Discord markdown is stripped (`**`, `*`,
`_`, `~~`, backticks, spoilers, `>` quotes, `#` headings), links keep their
label, `<@id>` becomes the member's display name, `<@&id>` the role name, `<#id>`
the channel name and custom emoji become `:name:`. Text past 600 characters is
cut at a word boundary.

**No emoji glyphs**: the bundled faces have no colour emoji, and Pillow does no
font fallback, so Unicode emoji are removed instead of drawn as empty boxes.
Same for scripts the installed fonts do not cover (CJK on a host without a CJK
font).

## Troubleshooting

| Symptom | Cause and fix |
| --- | --- |
| `Pillow is missing on the bot host` | `[p]pipinstall pillow`, then `[p]reload quoter`. |
| `I need the Attach Files permission` | The bot needs **Attach Files** in that channel. |
| `That message has no text to quote` | The message is an attachment, sticker, embed or only emoji. Quoter draws text, like the Equicord plugin does. |
| `The split style puts the author's avatar next to the text` | The avatar could not be fetched. Try again; with `split` only as the server default the card falls back to the gradient layout instead. |
| `I could not find that message` | Use a message link (`Copy Message Link`), an ID from the same channel, or reply to the message. The bot must be able to read that channel. |
| `Apps -> Quote` is missing | `[p]slash enablecog quoter` and `[p]slash sync` as owner, then Ctrl+R in the client. |
| Split card came out as a gradient card | The server default is `split` but the author's avatar could not be fetched, so it fell back. `--split` shows the hint instead. |
| Quote text looks wrong | Markdown is stripped on purpose. Mentions are resolved from the server cache, so a member who left shows `@user`. |
| Emoji disappeared | Expected: see *No emoji glyphs* above. |
| Card ignores the server defaults | Per-card options win. Check `[p]quote set show`. |

## Data

The cog stores per-server presentation defaults only (style, picture side, font,
theme, grayscale, watermark on/off and text, accent) in Red's config. No user
data is written; avatars and pictures are fetched while a card is drawn and are
not cached to disk.
