"""Pillow renderer for a fake Discord message screenshot.

No ``discord``/``redbot`` imports: the cog hands in the avatar bytes, the
display name and (optionally) custom emoji images, so this module renders
identically in a smoke test. Dark theme only, because that is what the
screenshot is supposed to look like.

Layout numbers follow the desktop client closely enough to survive a second
look: 40 px avatar in a 16 px gutter, 16 px body text on a 22 px line,
username + timestamp on the header line, mentions and code spans drawn as the
pill/box the client draws. Everything is laid out at 2x and downsampled once,
so avatars and emoji have clean edges.
"""

from __future__ import annotations

import io
import re
from dataclasses import dataclass, replace
from typing import Any, Dict, List, Optional, Sequence, Tuple

from PIL import Image, ImageDraw, ImageFont

SCALE = 2
WIDTH = 720
PAD = 16
AVATAR = 40
GAP = 16
TEXT_X = PAD + AVATAR + GAP
RIGHT = 20
HEADER = 22
LINE = 22
BODY = 16
EMOJI_PX = 20

BG = (49, 51, 56)
TEXT = (219, 222, 225)
MUTED = (148, 155, 164)
WHITE = (255, 255, 255)
LINK = (0, 168, 252)
MENTION_BG = (60, 66, 112)
MENTION_FG = (201, 205, 251)
CODE_BG = (30, 31, 34)
SPOILER_BG = (30, 31, 34)
BOT_TAG = (88, 101, 242)

FONT_FILES = {
    ("sans", False, False): (
        "/usr/share/fonts/truetype/lato/Lato-Regular.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/TTF/DejaVuSans.ttf",
    ),
    ("sans", True, False): (
        "/usr/share/fonts/truetype/lato/Lato-Bold.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    ),
    ("sans", False, True): (
        "/usr/share/fonts/truetype/lato/Lato-Italic.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Oblique.ttf",
    ),
    ("sans", True, True): (
        "/usr/share/fonts/truetype/lato/Lato-BoldItalic.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-BoldOblique.ttf",
    ),
    ("mono", False, False): (
        "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf",
        "/usr/share/fonts/truetype/noto/NotoSansMono-Regular.ttf",
    ),
    ("mono", True, False): (
        "/usr/share/fonts/truetype/dejavu/DejaVuSansMono-Bold.ttf",
    ),
}
WIDE_FONTS = (
    "/usr/share/fonts/truetype/droid/DroidSansFallbackFull.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
)
SYMBOL_FONTS = (
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/home/stoned/.local/share/fonts/Symbola_hint.ttf",
)
EMOJI_FONTS = (
    "/home/stoned/.local/share/fonts/NotoColorEmoji.ttf",
    "/usr/share/fonts/truetype/noto/NotoColorEmoji.ttf",
)

# A base emoji, an optional variation selector and skin tone, and any number
# of ZWJ-joined continuations. The continuation must match one base at a time:
# a character class run would glue two neighbouring emoji into one cluster.
_EMOJI_BASE = (
    "["
    "\U0001f000-\U0001faff"
    "\u2600-\u27bf"
    "\u2b00-\u2bff"
    "\u2190-\u21ff"
    "\u2300-\u23ff"
    "\u25a0-\u25ff"
    "\u2934\u2935\u3030\u303d\u3297\u3299"
    "]"
)
_EMOJI_SINGLE = _EMOJI_BASE + "(?:\ufe0f|\ufe0e)?(?:[\U0001f3fb-\U0001f3ff])?"
UNICODE_EMOJI_RE = re.compile(_EMOJI_SINGLE + "(?:\u200d" + _EMOJI_SINGLE + ")*")
CUSTOM_EMOJI_RE = re.compile(r"<(a?):(\w+):(\d+)>")
_MENTION_RE = re.compile(r"[@#][\w.\-]{1,64}")
_URL_RE = re.compile(r"https?://\S+|www\.\S+")


@dataclass(frozen=True)
class Style:
    bold: bool = False
    italic: bool = False
    underline: bool = False
    strike: bool = False
    code: bool = False
    spoiler: bool = False
    mention: bool = False
    link: bool = False

    def with_(self, flag: str) -> "Style":
        return replace(self, **{flag: True})


@dataclass
class Run:
    text: str = ""
    style: Style = Style()
    image: Optional[Image.Image] = None


@dataclass
class Atom:
    text: str = ""
    style: Style = Style()
    image: Optional[Image.Image] = None
    width: int = 0
    newline: bool = False


_fonts: Dict[Tuple[str, bool, bool, int], ImageFont.FreeTypeFont] = {}


_font_cache: Dict[Tuple[Tuple[str, ...], int], ImageFont.FreeTypeFont] = {}


def _load_first(paths: Sequence[str], size: int) -> Optional[ImageFont.FreeTypeFont]:
    key = (tuple(paths), size)
    if key in _font_cache:
        return _font_cache[key]
    font = None
    for path in paths:
        try:
            font = ImageFont.truetype(path, size)
            break
        except OSError:
            continue
    if font is not None:
        _font_cache[key] = font
    return font


def _font(size: int, *, bold: bool = False, italic: bool = False, mono: bool = False):
    shape = ("mono" if mono else "sans", bool(bold and not mono), bool(italic and not mono))
    key = shape + (size,)
    font = _fonts.get(key)
    if font is None:
        font = _load_first(FONT_FILES[shape], size)
        if font is None:
            font = ImageFont.load_default(size)
        _fonts[key] = font  # type: ignore[assignment]
        _fonts[key] = font  # type: ignore[assignment]
    return font


_emoji_font: Any = None
_emoji_tiles: Dict[str, Image.Image] = {}


def _emoji_tile(cluster: str) -> Optional[Image.Image]:
    """One colour emoji as an RGBA tile, or None when no emoji font is usable."""
    global _emoji_font
    cached = _emoji_tiles.get(cluster)
    if cached is not None:
        return cached
    if _emoji_font is None:
        # Noto Color Emoji ships a single 109 px bitmap strike; other sizes raise.
        _emoji_font = _load_first(EMOJI_FONTS, 109) or False
    if not _emoji_font:
        return None
    tile = Image.new("RGBA", (180, 180), (0, 0, 0, 0))
    try:
        ImageDraw.Draw(tile).text((10, 10), cluster, font=_emoji_font, embedded_color=True)
    except (OSError, ValueError):
        return None
    bbox = tile.getbbox()
    if not bbox:
        return None
    tile = tile.crop(bbox)
    side = max(tile.size)
    square = Image.new("RGBA", (side, side), (0, 0, 0, 0))
    square.paste(tile, ((side - tile.width) // 2, (side - tile.height) // 2))
    square = square.resize((EMOJI_PX * SCALE, EMOJI_PX * SCALE), Image.Resampling.LANCZOS)
    _emoji_tiles[cluster] = square
    return square


def _fit_emoji(image: Optional[Image.Image]) -> Optional[Image.Image]:
    """Scale a caller-supplied emoji picture (Twemoji 72 px, custom 128 px)."""
    if image is None:
        return None
    if image.mode != "RGBA":
        image = image.convert("RGBA")
    return image.resize((EMOJI_PX * SCALE, EMOJI_PX * SCALE), Image.Resampling.LANCZOS)


def _script_font(ch: str, style: Style, size: int):
    """Pick a face that can actually draw this character instead of tofu."""
    code = ord(ch)
    if 0x2E80 <= code <= 0x9FFF or 0xAC00 <= code <= 0xD7AF or 0xF900 <= code <= 0xFAFF:
        wide = _load_first(WIDE_FONTS, size)
        if wide is not None:
            return wide
    if 0x2000 <= code <= 0x2BFF or 0x1F000 <= code <= 0x1FAFF:
        symbol = _load_first(SYMBOL_FONTS, size)
        if symbol is not None:
            return symbol
    return _font(size, bold=style.bold, italic=style.italic, mono=style.code)


# -- markdown ------------------------------------------------------------

_MARKERS: Tuple[Tuple[str, str], ...] = (
    ("**", "bold"),
    ("__", "underline"),
    ("~~", "strike"),
    ("||", "spoiler"),
    ("*", "italic"),
    ("_", "italic"),
    ("`", "code"),
)


def _parse_markdown(text: str, style: Style = Style(), depth: int = 0) -> List[Run]:
    runs: List[Run] = []
    buffer = ""
    i = 0
    while i < len(text):
        matched = None
        if depth < 4:
            for marker, flag in _MARKERS:
                if not text.startswith(marker, i):
                    continue
                end = text.find(marker, i + len(marker))
                if end <= i + len(marker):
                    continue
                inner = text[i + len(marker) : end]
                if not inner.strip():
                    continue
                matched = (marker, flag, end, inner)
                break
        if matched is None:
            buffer += text[i]
            i += 1
            continue
        marker, flag, end, inner = matched
        if buffer:
            runs.append(Run(buffer, style))
            buffer = ""
        runs.extend(_parse_markdown(inner, style.with_(flag), depth + 1))
        i = end + len(marker)
    if buffer:
        runs.append(Run(buffer, style))
    return runs


def _decorate(runs: List[Run]) -> List[Run]:
    """Colour mentions and links inside plain runs the way the client does."""
    out: List[Run] = []
    for run in runs:
        if run.image is not None or run.style.code or run.style.spoiler:
            out.append(run)
            continue
        text = run.text
        pos = 0
        for match in _URL_RE.finditer(text):
            if match.start() > pos:
                out.extend(_split_mentions(text[pos : match.start()], run.style))
            out.append(Run(match.group(0), run.style.with_("link")))
            pos = match.end()
        if pos < len(text):
            out.extend(_split_mentions(text[pos:], run.style))
    return out


def _split_mentions(text: str, style: Style) -> List[Run]:
    out: List[Run] = []
    pos = 0
    for match in _MENTION_RE.finditer(text):
        if match.start() > pos:
            out.append(Run(text[pos : match.start()], style))
        out.append(Run(match.group(0), style.with_("mention")))
        pos = match.end()
    if pos < len(text):
        out.append(Run(text[pos:], style))
    return out


def _split_custom_emoji(text: str, images: Dict[str, Image.Image]) -> List[Run]:
    """<:name:id> becomes the emoji picture; an unknown id stays as :name:."""
    runs: List[Run] = []
    pos = 0
    for match in CUSTOM_EMOJI_RE.finditer(text):
        if match.start() > pos:
            runs.append(Run(text[pos : match.start()]))
        image = _fit_emoji(images.get(match.group(3)))
        if image is not None:
            runs.append(Run(image=image))
        else:
            runs.append(Run(":{}:".format(match.group(2))))
        pos = match.end()
    if pos < len(text):
        runs.append(Run(text[pos:]))
    return runs


# -- layout --------------------------------------------------------------


def _merge(runs: List[Run]) -> List[Run]:
    merged: List[Run] = []
    for run in runs:
        if run.image is None and merged and merged[-1].image is None and merged[-1].style == run.style:
            merged[-1] = Run(merged[-1].text + run.text, run.style)
        else:
            merged.append(run)
    return merged


def _atoms(runs: List[Run], emoji_images: Optional[Dict[str, Image.Image]] = None) -> List[Atom]:
    atoms: List[Atom] = []
    provided = emoji_images or {}
    for run in runs:
        if run.image is not None:
            atoms.append(Atom(image=run.image, width=run.image.width + SCALE * 2))
            continue
        text = run.text
        pos = 0
        for match in UNICODE_EMOJI_RE.finditer(text):
            if match.start() > pos:
                atoms.extend(_text_atoms(text[pos : match.start()], run.style))
            tile = _fit_emoji(provided.get(match.group(0))) or _emoji_tile(match.group(0))
            if tile is None:
                atoms.extend(_text_atoms(match.group(0), run.style))
            else:
                atoms.append(Atom(image=tile, width=tile.width + SCALE * 2))
            pos = match.end()
        if pos < len(text):
            atoms.extend(_text_atoms(text[pos:], run.style))
    return atoms


def _text_atoms(text: str, style: Style) -> List[Atom]:
    atoms: List[Atom] = []
    for token in re.findall(r"\n|[^\S\n]+|[^\s]+", text):
        if token == "\n":
            atoms.append(Atom(newline=True))
        elif token.strip():
            atoms.append(Atom(token, style, width=_measure(token, style)))
        else:
            atoms.append(Atom(token, style, width=_measure(token, style)))
    return atoms


def _measure(text: str, style: Style) -> int:
    if style.code:
        return int(_font(BODY * SCALE, mono=True).getlength(text))
    font = _font(BODY * SCALE, bold=style.bold, italic=style.italic)
    if all(ord(ch) < 0x2000 for ch in text):
        return int(font.getlength(text))
    # mixed scripts: measure per character with the face that draws it
    total = 0
    for ch in text:
        total += int(_script_font(ch, style, BODY * SCALE).getlength(ch))
    return total


def _break_long(atom: Atom, limit: int) -> List[Atom]:
    """A url longer than the text column has to wrap mid-word."""
    pieces: List[Atom] = []
    current = ""
    for ch in atom.text:
        trial = current + ch
        if current and _measure(trial, atom.style) > limit:
            pieces.append(Atom(current, atom.style, width=_measure(current, atom.style)))
            current = ch
        else:
            current = trial
    if current:
        pieces.append(Atom(current, atom.style, width=_measure(current, atom.style)))
    return pieces


def _layout(atoms: List[Atom], limit: int) -> List[List[Tuple[int, Atom]]]:
    lines: List[List[Tuple[int, Atom]]] = [[]]
    x = 0
    for atom in atoms:
        if atom.newline:
            if lines[-1]:
                lines.append([])
            x = 0
            continue
        if atom.width > limit and atom.text and not atom.image:
            for piece in _break_long(atom, limit):
                if x and x + piece.width > limit:
                    lines.append([])
                    x = 0
                lines[-1].append((x, piece))
                x += piece.width
            continue
        if x and x + atom.width > limit:
            if atom.text and not atom.text.strip():
                lines.append([])
                x = 0
                continue
            lines.append([])
            x = 0
        if not lines[-1] and atom.text and not atom.text.strip():
            continue
        lines[-1].append((x, atom))
        x += atom.width
    while len(lines) > 1 and not lines[-1]:
        lines.pop()
    return lines


# -- drawing -------------------------------------------------------------


def _circle(image: Image.Image, size: int) -> Image.Image:
    image = image.convert("RGBA").resize((size, size), Image.Resampling.LANCZOS)
    mask = Image.new("L", (size * 4, size * 4), 0)
    ImageDraw.Draw(mask).ellipse((0, 0, size * 4 - 1, size * 4 - 1), fill=255)
    mask = mask.resize((size, size), Image.Resampling.LANCZOS)
    out = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    out.paste(image, (0, 0), mask)
    return out


def _rounded_picture(data: Optional[bytes], max_w: int, max_h: int, radius: float):
    """An attachment scaled into the message column, corners rounded."""
    if not data:
        return None
    try:
        picture = Image.open(io.BytesIO(data))
        picture.load()
    except (OSError, ValueError):
        return None
    picture = picture.convert("RGB")
    if picture.width <= 0 or picture.height <= 0:
        return None
    scale = min(max_w / picture.width, max_h / picture.height, 2.0)
    picture = picture.resize(
        (max(1, int(picture.width * scale)), max(1, int(picture.height * scale))),
        Image.Resampling.LANCZOS,
    )
    mask = Image.new("L", (picture.width * 4, picture.height * 4), 0)
    ImageDraw.Draw(mask).rounded_rectangle(
        (0, 0, picture.width * 4 - 1, picture.height * 4 - 1), radius=int(radius * 4), fill=255
    )
    mask = mask.resize(picture.size, Image.Resampling.LANCZOS)
    out = Image.new("RGBA", picture.size, (0, 0, 0, 0))
    out.paste(picture.convert("RGBA"), (0, 0), mask)
    return out


def _initials_avatar(name: str, size: int) -> Image.Image:
    image = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    draw.ellipse((0, 0, size - 1, size - 1), fill=(88, 101, 242))
    letter = (name or "?").strip()[:1].upper() or "?"
    font = _font(int(size * 0.45), bold=True)
    draw.text((size / 2, size / 2), letter, font=font, fill=WHITE, anchor="mm")
    return image


def _text_baseline(font, top: int, height: int) -> int:
    ascent, descent = font.getmetrics()
    return int(top + (height - (ascent + descent)) / 2 + ascent)


def render(
    *,
    name: str,
    message: str,
    timestamp: str,
    avatar: Optional[bytes] = None,
    bot: bool = False,
    name_color: Optional[Tuple[int, int, int]] = None,
    emoji_images: Optional[Dict[str, Image.Image]] = None,
    attachment: Optional[bytes] = None,
    width: int = WIDTH,
) -> bytes:
    """Render one message row as a PNG and return the file bytes."""
    colour = name_color or WHITE
    limit = (width - TEXT_X - RIGHT) * SCALE

    image_runs = _split_custom_emoji(message, emoji_images or {})
    runs: List[Run] = []
    for run in image_runs:
        if run.image is not None:
            runs.append(run)
        else:
            runs.extend(_decorate(_parse_markdown(run.text)))
    atoms = _atoms(_merge(runs), emoji_images)
    lines = _layout(atoms, limit)

    top = 12
    header_top = top
    body_top = header_top + HEADER + 2
    picture = _rounded_picture(attachment, min(400, width - TEXT_X - RIGHT) * SCALE, 300 * SCALE, 8)
    picture_h = (picture.height + 8 * SCALE) if picture is not None else 0
    height = body_top + max(len(lines), 1) * LINE + picture_h + 12

    canvas = Image.new("RGB", (width * SCALE, height * SCALE), BG)
    draw = ImageDraw.Draw(canvas)

    # avatar
    size = AVATAR * SCALE
    avatar_image = None
    if avatar:
        try:
            avatar_image = Image.open(io.BytesIO(avatar))
            avatar_image.load()
        except (OSError, ValueError):
            avatar_image = None
    if avatar_image is None:
        tile = _initials_avatar(name, size)
    else:
        tile = _circle(avatar_image, size)
    canvas.paste(tile, (PAD * SCALE, top * SCALE + SCALE * 2), tile)

    # header: name, BOT tag, timestamp
    header_size = BODY * SCALE
    name_font = _font(header_size, bold=True)
    x = TEXT_X * SCALE
    baseline = _text_baseline(name_font, header_top * SCALE, HEADER * SCALE)
    draw.text((x, baseline), name, font=name_font, fill=colour, anchor="ls")
    x += int(name_font.getlength(name)) + 6 * SCALE
    if bot:
        tag_font = _font(9 * SCALE, bold=True)
        tag_w = int(tag_font.getlength("BOT")) + 8 * SCALE
        tag_h = 15 * SCALE
        tag_y = baseline - int(tag_font.getmetrics()[0]) - 1 * SCALE
        draw.rounded_rectangle(
            (x, tag_y, x + tag_w, tag_y + tag_h), radius=4 * SCALE, fill=BOT_TAG
        )
        draw.text(
            (x + tag_w / 2, tag_y + tag_h / 2),
            "BOT",
            font=tag_font,
            fill=WHITE,
            anchor="mm",
        )
        x += tag_w + 6 * SCALE
    time_font = _font(11 * SCALE)
    time_baseline = _text_baseline(time_font, header_top * SCALE, HEADER * SCALE)
    draw.text((x, time_baseline + SCALE), timestamp, font=time_font, fill=MUTED, anchor="ls")

    # body
    for index, line in enumerate(lines or [[]]):
        line_top = (body_top + index * LINE) * SCALE
        blocks: List[Tuple[int, int, Tuple[int, int, int], int]] = []
        placements: List[Tuple[int, Atom, Any, int, Tuple[int, int, int]]] = []
        for x, atom in line:
            if atom.image is not None:
                # emoji sit on the baseline like the client draws them, so they
                # reach a little above the line box instead of floating centred
                y = line_top + int(LINE * SCALE * 0.82) - atom.image.height
                canvas.paste(atom.image, (TEXT_X * SCALE + x, y), atom.image)
                continue
            style = atom.style
            font = (
                _font(BODY * SCALE, mono=True, bold=style.bold)
                if style.code
                else _font(BODY * SCALE, bold=style.bold, italic=style.italic)
            )
            colour_text = TEXT
            if style.link:
                colour_text = LINK
            elif style.mention:
                colour_text = MENTION_FG
            base = _text_baseline(font, line_top, LINE * SCALE)
            draw_x = TEXT_X * SCALE + x
            if style.mention:
                blocks.append((draw_x, draw_x + atom.width, MENTION_BG, 4))
            elif style.code:
                blocks.append((draw_x, draw_x + atom.width, CODE_BG, 5))
            placements.append((draw_x, atom, font, base, colour_text))
            if style.underline or style.strike:
                start = TEXT_X * SCALE + x
                end = start + atom.width
                width_line = max(1, SCALE)
                if style.underline:
                    draw.line(
                        (start, base + 2 * SCALE, end, base + 2 * SCALE),
                        fill=colour_text,
                        width=width_line,
                    )
                if style.strike:
                    mid = base - int(font.getmetrics()[0] * 0.3)
                    draw.line((start, mid, end, mid), fill=colour_text, width=width_line)
        for start, end, fill, radius in _merge_blocks(blocks):
            draw.rounded_rectangle(
                (
                    start - SCALE * 2,
                    line_top + SCALE * 2,
                    end + SCALE * 2,
                    line_top + LINE * SCALE - SCALE * 2,
                ),
                radius=radius * SCALE,
                fill=fill,
            )
        for draw_x, atom, font, base, colour_text in placements:
            style = atom.style
            for ch in atom.text:
                face = font if ord(ch) < 0x2000 else _script_font(ch, style, BODY * SCALE)
                draw.text((draw_x, base), ch, font=face, fill=colour_text, anchor="ls")
                draw_x += int(face.getlength(ch))
        # spoilers: cover the glyphs the way the client does
        for x, atom in line:
            if atom.image is None and atom.style.spoiler:
                draw.rounded_rectangle(
                    (
                        TEXT_X * SCALE + x - SCALE,
                        line_top + SCALE * 2,
                        TEXT_X * SCALE + x + atom.width,
                        line_top + LINE * SCALE - SCALE * 2,
                    ),
                    radius=3 * SCALE,
                    fill=SPOILER_BG,
                )

    if picture is not None:
        canvas.paste(
            picture,
            (TEXT_X * SCALE, body_top + max(len(lines), 1) * LINE + 8 * SCALE),
            picture,
        )

    final = canvas.resize((width, height), Image.Resampling.LANCZOS)
    buffer = io.BytesIO()
    final.save(buffer, format="PNG", optimize=True)
    return buffer.getvalue()


def _merge_blocks(blocks: List[Tuple[int, int, Tuple[int, int, int], int]]):
    """Join adjacent mention/code boxes on one line into a single pill."""
    out: List[Tuple[int, int, Tuple[int, int, int], int]] = []
    for start, end, fill, radius in blocks:
        if out and out[-1][2] == fill and start - out[-1][1] <= SCALE * 3:
            out[-1] = (out[-1][0], end, fill, radius)
        else:
            out.append((start, end, fill, radius))
    return out