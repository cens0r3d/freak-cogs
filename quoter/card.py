"""Drawing and text cleanup for quoter.

Turns a message into a square quote card: the author's avatar in a ring, a
decorative quote mark, the text in a chosen face, the attribution below and
an optional watermark. The background is a gradient derived from the
avatar's own colour, so every card carries a hint of who said it.

No discord or redbot imports here on purpose: the cog fetches the avatar and
this module draws, so a card can be rendered and inspected without a bot.
"""

from __future__ import annotations

import colorsys
import io
import re
from typing import Any, Callable, Dict, List, Optional, Tuple

try:  # Pillow is a cog requirement, but the cog must still load without it
    from PIL import Image, ImageDraw, ImageFont, ImageOps

    PIL_AVAILABLE = True
except ImportError:  # pragma: no cover - only on a host without Pillow
    Image = ImageDraw = ImageFont = ImageOps = None
    PIL_AVAILABLE = False

SCALE = 2
WIDTH = 1080
PAD = 96
MAX_CHARS = 600
MAX_LINES = 12
FONT_SIZES = (62, 58, 54, 50, 46, 42, 38, 34)

RGB = Tuple[int, int, int]
Resolver = Callable[[str, str], Optional[str]]

QUOTE_FONTS: Dict[str, Tuple[str, ...]] = {
    "serif": (
        "/usr/share/fonts/truetype/liberation/LiberationSerif-Regular.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSerif.ttf",
    ),
    "sans": (
        "/usr/share/fonts/truetype/lato/Lato-Regular.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    ),
    "light": (
        "/usr/share/fonts/truetype/lato/Lato-Light.ttf",
        "/usr/share/fonts/truetype/lato/Lato-Regular.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    ),
    "mono": (
        "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationMono-Regular.ttf",
    ),
}

MARK_FONTS = (
    "/usr/share/fonts/truetype/liberation/LiberationSerif-Bold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSerif-Bold.ttf",
)

SANS_FONTS = (
    "/usr/share/fonts/truetype/lato/Lato-Semibold.ttf",
    "/usr/share/fonts/truetype/lato/Lato-Regular.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
)

THEMES = ("dark", "light")


def available() -> bool:
    """True when Pillow can be imported, i.e. a card can be drawn at all."""
    return PIL_AVAILABLE


# --------------------------------------------------------------------------
# text
# --------------------------------------------------------------------------

_CUSTOM_EMOJI_RE = re.compile(r"<a?:([A-Za-z0-9_]+):\d+>")
_USER_RE = re.compile(r"<@!?(\d+)>")
_ROLE_RE = re.compile(r"<@&(\d+)>")
_CHANNEL_RE = re.compile(r"<#(\d+)>")
_TIMESTAMP_RE = re.compile(r"<t:\d+(?::[tTdDfFR])?>")
_EMOJI_RE = re.compile(
    "[\U0001f000-\U0001faff\u2600-\u27bf\u2b00-\u2bff\ufe0e\ufe0f\u200d\u20e3]"
)

_MARKUP = (
    (re.compile(r"\|\|(.+?)\|\|", re.S), r"\1"),
    (re.compile(r"\*\*\*(.+?)\*\*\*", re.S), r"\1"),
    (re.compile(r"\*\*(.+?)\*\*", re.S), r"\1"),
    (re.compile(r"__(.+?)__", re.S), r"\1"),
    (re.compile(r"(?<![A-Za-z0-9])_(.+?)_(?![A-Za-z0-9])", re.S), r"\1"),
    (re.compile(r"\*(.+?)\*", re.S), r"\1"),
    (re.compile(r"~~(.+?)~~", re.S), r"\1"),
    (re.compile(r"`{1,3}(.+?)`{1,3}", re.S), r"\1"),
    (re.compile(r"\[(.+?)\]\((?:https?://[^)]+)\)"), r"\1"),
    (re.compile(r"<(https?://[^>]+)>"), r"\1"),
)


def clean_message(content: str, resolver: Optional[Resolver] = None) -> str:
    """Turn raw message markdown into plain text a serif font can draw.

    Mentions are replaced by the names the caller resolves (``@user``,
    ``@role`` and ``#channel`` when nothing is known), custom emoji become
    ``:name:``, and Unicode emoji are dropped: the bundled faces carry no
    colour emoji glyphs, and a row of empty boxes reads as a broken card.
    """
    text = content or ""

    if resolver is not None:
        text = _USER_RE.sub(
            lambda m: "@" + (resolver("user", m.group(1)) or "user"), text
        )
        text = _ROLE_RE.sub(
            lambda m: "@" + (resolver("role", m.group(1)) or "role"), text
        )
        text = _CHANNEL_RE.sub(
            lambda m: "#" + (resolver("channel", m.group(1)) or "channel"), text
        )
    else:
        text = _USER_RE.sub("@user", text)
        text = _ROLE_RE.sub("@role", text)
        text = _CHANNEL_RE.sub("#channel", text)

    text = _CUSTOM_EMOJI_RE.sub(lambda m: ":" + m.group(1) + ":", text)
    text = _TIMESTAMP_RE.sub("", text)

    for pattern, replacement in _MARKUP:
        text = pattern.sub(replacement, text)

    text = _EMOJI_RE.sub("", text)

    lines = []
    for line in text.splitlines():
        line = line.strip()
        line = re.sub(r"^>{1,3}\s*", "", line)
        line = re.sub(r"^#{1,3}\s*", "", line)
        lines.append(line)
    text = "\n".join(lines)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()

    if len(text) > MAX_CHARS:
        cut = text[:MAX_CHARS]
        if " " in cut:
            cut = cut.rsplit(" ", 1)[0]
        text = cut.rstrip(" ,.;:") + "…"
    return text


_FLAG_ALIASES = {
    "--gray": ("grayscale", True),
    "--grayscale": ("grayscale", True),
    "--colour": ("grayscale", False),
    "--color": ("grayscale", False),
    "--light": ("theme", "light"),
    "--dark": ("theme", "dark"),
    "--watermark": ("watermark", True),
    "--no-watermark": ("watermark", False),
}
_VALUE_FLAGS = {"--font": "font", "--theme": "theme", "--accent": "accent"}


def parse_invocation(
    raw: str, fonts: Tuple[str, ...] = tuple(QUOTE_FONTS)
) -> Tuple[Optional[str], Dict[str, object], Optional[str]]:
    """Split ``<link or id> --font serif --light`` into target and options.

    Returns ``(target, options, error)``. Only options the user actually
    typed are in the dict, so the guild defaults survive anything unset.
    """
    tokens = (raw or "").split()
    options: Dict[str, object] = {}
    target: Optional[str] = None
    index = 0

    if tokens and not tokens[0].startswith("-"):
        target = tokens[0]
        index = 1

    while index < len(tokens):
        token = tokens[index]
        name, _, inline = token.partition("=")
        name = name.lower()
        if name in _FLAG_ALIASES:
            key, value = _FLAG_ALIASES[name]
            options[key] = value
            index += 1
            continue
        if name in _VALUE_FLAGS:
            key = _VALUE_FLAGS[name]
            value = inline or (tokens[index + 1] if index + 1 < len(tokens) else "")
            index += 1 if inline else 2
            if not value or value.startswith("-"):
                return target, options, f"`{name}` needs a value."
            value = value.strip().lower()
            if name == "--font" and value not in fonts:
                return (
                    target,
                    options,
                    f"Unknown font `{value}` - pick one of {_list(fonts)}.",
                )
            if name == "--theme" and value not in THEMES:
                return (
                    target,
                    options,
                    f"Unknown theme `{value}` - pick one of {_list(THEMES)}.",
                )
            if name == "--accent":
                if not re.fullmatch(r"#?[0-9a-f]{6}", value):
                    return (
                        target,
                        options,
                        f"`{value}` is not a hex colour like `#5865f2`.",
                    )
                value = value.lstrip("#")
            options[key] = value
            continue
        return target, options, f"Unknown option `{token}`."

    return target, options, None


def _list(values) -> str:
    return ", ".join(f"`{value}`" for value in values)


# --------------------------------------------------------------------------
# drawing helpers
# --------------------------------------------------------------------------

_font_cache: Dict[Tuple[Tuple[str, ...], int], Any] = {}


def _load_font(paths: Tuple[str, ...], size: int):
    cached = _font_cache.get((paths, size))
    if cached is not None:
        return cached
    for path in paths:
        try:
            font = ImageFont.truetype(path, size)
        except OSError:
            continue
        _font_cache[(paths, size)] = font
        return font
    try:
        font = ImageFont.load_default(size)
    except TypeError:  # Pillow < 10.1
        font = ImageFont.load_default()
    _font_cache[(paths, size)] = font
    return font


def _clamp(value: float) -> int:
    return max(0, min(255, int(round(value))))


def _mix(first: RGB, second: RGB, ratio: float) -> RGB:
    return (
        _clamp(first[0] + (second[0] - first[0]) * ratio),
        _clamp(first[1] + (second[1] - first[1]) * ratio),
        _clamp(first[2] + (second[2] - first[2]) * ratio),
    )


def _hex_to_rgb(value: str) -> RGB:
    value = value.lstrip("#")
    return (int(value[0:2], 16), int(value[2:4], 16), int(value[4:6], 16))


def _tune_accent(rgb: RGB, grayscale: bool) -> RGB:
    """Lift saturation and lightness so a muddy average still reads as a colour."""
    red, green, blue = (channel / 255 for channel in rgb)
    hue, saturation, value = colorsys.rgb_to_hsv(red, green, blue)
    saturation = 0.0 if grayscale else max(saturation, 0.55)
    value = min(max(value, 0.55), 0.85)
    red, green, blue = colorsys.hsv_to_rgb(hue, saturation, value)
    return (_clamp(red * 255), _clamp(green * 255), _clamp(blue * 255))


def _accent_colour(
    avatar: Optional[bytes], grayscale: bool, override: Optional[str]
) -> RGB:
    if override:
        return _tune_accent(_hex_to_rgb(override), grayscale)
    rgb = (99, 102, 241)
    if avatar:
        try:
            with Image.open(io.BytesIO(avatar)) as image:
                small = image.convert("RGB").resize((1, 1), Image.Resampling.BILINEAR)
                red, green, blue = small.tobytes()
                rgb = (red, green, blue)
        except Exception:
            pass
    return _tune_accent(rgb, grayscale)


def _wrap(draw, text: str, font, max_width: float) -> List[str]:
    lines: List[str] = []
    for paragraph in text.split("\n"):
        if not paragraph.strip():
            lines.append("")
            continue
        current = ""
        for word in paragraph.split(" "):
            candidate = f"{current} {word}".strip()
            if draw.textlength(candidate, font=font) <= max_width or not current:
                current = candidate
            else:
                lines.append(current)
                current = word
        lines.append(current)

    wrapped: List[str] = []
    for line in lines:
        while draw.textlength(line, font=font) > max_width and len(line) > 1:
            cut = len(line)
            while cut > 1 and draw.textlength(line[:cut], font=font) > max_width:
                cut -= 1
            wrapped.append(line[:cut])
            line = line[cut:]
        wrapped.append(line)
    return wrapped


def _circular(avatar: bytes, diameter: int, grayscale: bool):
    with Image.open(io.BytesIO(avatar)) as image:
        image = image.convert("RGB")
        side = min(image.size)
        left = (image.width - side) // 2
        top = (image.height - side) // 2
        image = image.crop((left, top, left + side, top + side))
        image = image.resize((diameter, diameter), Image.Resampling.LANCZOS)
        if grayscale:
            image = image.convert("L").convert("RGB")
        picture = image.convert("RGBA")

    mask = Image.new("L", (diameter * 4, diameter * 4), 0)
    ImageDraw.Draw(mask).ellipse((0, 0, diameter * 4 - 1, diameter * 4 - 1), fill=255)
    mask = mask.resize((diameter, diameter), Image.Resampling.LANCZOS)
    picture.putalpha(mask)
    return picture


def _initials(name: str) -> str:
    letters = [part[0] for part in re.split(r"[\s._-]+", name) if part]
    return "".join(letters[:2]).upper() or "?"


# --------------------------------------------------------------------------
# render
# --------------------------------------------------------------------------


def render(
    *,
    text: str,
    author: str,
    avatar: Optional[bytes] = None,
    theme: str = "dark",
    font: str = "serif",
    grayscale: bool = False,
    watermark: Optional[str] = None,
    accent: Optional[str] = None,
) -> bytes:
    """Draw one quote card and return it as PNG bytes."""
    if not PIL_AVAILABLE:
        raise RuntimeError("Pillow is not installed")

    theme = theme if theme in THEMES else "dark"
    font = font if font in QUOTE_FONTS else "serif"
    text = text.strip() or "…"

    scale = SCALE
    width = WIDTH * scale
    inner = (WIDTH - PAD * 2) * scale

    probe = ImageDraw.Draw(Image.new("RGB", (8, 8)))
    quote_font = None
    lines: List[str] = [text]
    size = FONT_SIZES[0]
    for size in FONT_SIZES:
        quote_font = _load_font(QUOTE_FONTS[font], size * scale)
        lines = _wrap(probe, text, quote_font, inner)
        if len(lines) <= MAX_LINES:
            break

    mark_font = _load_font(MARK_FONTS, 150 * scale)
    name_font = _load_font(SANS_FONTS, 30 * scale)
    mark_height = int(mark_font.getbbox("“")[3] * 0.82)

    avatar_d = 150 * scale
    line_height = int(size * scale * 1.44)
    text_height = len(lines) * line_height
    attribution = f"— {author}"

    gap_top = 88 * scale
    gap_avatar = 34 * scale
    gap_mark = 4 * scale
    gap_text = 34 * scale
    gap_watermark = 26 * scale
    watermark_size = 20 * scale
    bottom_pad = 76 * scale

    group = avatar_d + gap_avatar + mark_height + gap_mark + text_height
    group += gap_text + 30 * scale
    if watermark:
        group += gap_watermark + watermark_size

    height = max(width, gap_top + group + bottom_pad)
    if height % scale:
        height += scale - (height % scale)

    dark = theme == "dark"
    base = _accent_colour(avatar, grayscale, accent)
    if dark:
        top_colour = _mix(base, (8, 8, 12), 0.80)
        bottom_colour = (10, 10, 14)
        text_colour: RGB = (245, 245, 247)
        muted: RGB = (168, 173, 181)
    else:
        top_colour = _mix(base, (255, 255, 255), 0.86)
        bottom_colour = (243, 243, 246)
        text_colour = (22, 22, 26)
        muted = (100, 104, 112)

    gradient = Image.new("RGB", (1, height))
    pixels = gradient.load()
    for y in range(height):
        pixels[0, y] = _mix(top_colour, bottom_colour, y / max(height - 1, 1))
    canvas = gradient.resize((width, height), Image.Resampling.BILINEAR).convert("RGBA")

    if dark:
        glow_mask = ImageOps.invert(Image.radial_gradient("L")).resize(
            (width, height), Image.Resampling.BILINEAR
        )
        glow_mask = glow_mask.point(lambda value: int(value * 0.16))
        glow = Image.new("RGBA", (width, height), base + (0,))
        glow.putalpha(glow_mask)
        canvas = Image.alpha_composite(canvas, glow)

    draw = ImageDraw.Draw(canvas)
    overlay = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    over = ImageDraw.Draw(overlay)

    centre = width // 2
    y = (height - group) // 2

    avatar_top = y
    avatar_left = centre - avatar_d // 2
    picture = None
    if avatar:
        try:
            picture = _circular(avatar, avatar_d, grayscale)
        except Exception:
            picture = None
        if picture is not None:
            canvas.alpha_composite(picture, (avatar_left, avatar_top))
    if not avatar or picture is None:
        over.ellipse(
            (avatar_left, avatar_top, avatar_left + avatar_d, avatar_top + avatar_d),
            fill=_mix(base, (12, 12, 16) if dark else (255, 255, 255), 0.35),
        )
        initials_font = _load_font(SANS_FONTS, 54 * scale)
        over.text(
            (centre, avatar_top + avatar_d // 2),
            _initials(author),
            font=initials_font,
            fill=text_colour + (255,),
            anchor="mm",
        )

    over.ellipse(
        (
            avatar_left - 7 * scale,
            avatar_top - 7 * scale,
            avatar_left + avatar_d + 7 * scale,
            avatar_top + avatar_d + 7 * scale,
        ),
        outline=base + (200,),
        width=3 * scale,
    )
    canvas = Image.alpha_composite(canvas, overlay)
    draw = ImageDraw.Draw(canvas)

    y = avatar_top + avatar_d + gap_avatar

    mark_colour = base if dark else _mix(base, (30, 30, 34), 0.25)
    draw.text(
        (centre, y - mark_height * 0.15),
        "“",
        font=mark_font,
        fill=mark_colour,
        anchor="ma",
    )
    y += mark_height + gap_mark

    for line in lines:
        y += line_height
        if line:
            draw.text(
                (centre, y - line_height // 2),
                line,
                font=quote_font,
                fill=text_colour,
                anchor="mm",
            )

    y += gap_text
    draw.text((centre, y), attribution, font=name_font, fill=muted, anchor="ma")
    y += 30 * scale

    if watermark:
        y += gap_watermark
        small = _load_font(SANS_FONTS, watermark_size)
        stamp = Image.new("RGBA", (width, height), (0, 0, 0, 0))
        ImageDraw.Draw(stamp).text(
            (centre, y), watermark.upper(), font=small, fill=muted + (150,), anchor="ma"
        )
        canvas = Image.alpha_composite(canvas, stamp)

    output = canvas.convert("RGB").resize(
        (WIDTH, height // scale), Image.Resampling.LANCZOS
    )
    buffer = io.BytesIO()
    output.save(buffer, format="PNG", optimize=True)
    return buffer.getvalue()
