"""PNG card renderer for the diskinfo cog.

Pillow only, no discord/redbot imports. The image is drawn from the same
numbers the embed shows, so a screenshot and the message never disagree.
"""

from __future__ import annotations

import io
import os

WIDTH = 1000
PAD = 44
ROW = 108
HEADER = 118

BG = (43, 45, 49)
BAR_BG = (30, 31, 34)
TEXT = (242, 243, 245)
MUTED = (181, 186, 193)
FAINT = (120, 125, 133)

WARN = (250, 166, 26)
CRIT = (237, 66, 69)

_REGULAR = (
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/TTF/DejaVuSans.ttf",
    "/usr/share/fonts/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
    "/Library/Fonts/Arial.ttf",
    "C:\\Windows\\Fonts\\arial.ttf",
)
_BOLD = (
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/TTF/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
    "/Library/Fonts/Arial Bold.ttf",
    "C:\\Windows\\Fonts\\arialbd.ttf",
)


def available() -> bool:
    try:
        import PIL  # noqa: F401
    except Exception:
        return False
    return True


def _font(size: int, bold: bool = False):
    from PIL import ImageFont

    for path in _BOLD if bold else _REGULAR:
        if os.path.exists(path):
            try:
                return ImageFont.truetype(path, size)
            except OSError:
                continue
    try:
        return ImageFont.load_default(size=size)
    except TypeError:  # Pillow < 10.1
        return ImageFont.load_default()


def _level_colour(percent: float, accent: tuple[int, int, int]) -> tuple[int, int, int]:
    if percent >= 92:
        return CRIT
    if percent >= 85:
        return WARN
    return accent


def render(hostname: str, filesystems, accent=(123, 104, 238)) -> bytes:
    """Horizontal bar card, one row per filesystem, returns PNG bytes."""
    from PIL import Image, ImageDraw

    from . import diskspace

    accent = (int(accent[0]), int(accent[1]), int(accent[2]))
    filesystems = list(filesystems)
    height = PAD + HEADER + max(len(filesystems), 1) * ROW + 24

    image = Image.new("RGBA", (WIDTH, height), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle((0, 0, WIDTH, height), radius=22, fill=BG + (255,))

    title_font = _font(30, bold=True)
    sub_font = _font(17)
    name_font = _font(23, bold=True)
    pct_font = _font(23, bold=True)
    detail_font = _font(16)

    draw.text((PAD, PAD - 6), "Disk usage", font=title_font, fill=TEXT)
    draw.text((PAD, PAD + 34), hostname, font=sub_font, fill=MUTED)

    if not filesystems:
        draw.text(
            (PAD, PAD + HEADER),
            "no filesystem to show",
            font=name_font,
            fill=MUTED,
        )
        buffer = io.BytesIO()
        image.save(buffer, "PNG")
        return buffer.getvalue()

    y = PAD + HEADER
    bar_width = WIDTH - PAD * 2

    for fs in filesystems:
        percent = fs.percent
        colour = _level_colour(percent, accent)

        draw.text((PAD, y), fs.mountpoint, font=name_font, fill=TEXT)
        percent_text = f"{percent:.0f}%"
        pct_w = draw.textlength(percent_text, font=pct_font)
        draw.text((WIDTH - PAD - pct_w, y - 1), percent_text, font=pct_font, fill=colour)

        bar_y = y + 38
        draw.rounded_rectangle(
            (PAD, bar_y, PAD + bar_width, bar_y + 16), radius=8, fill=BAR_BG
        )
        filled = int(bar_width * min(max(percent, 0.0), 100.0) / 100)
        if filled > 0:
            draw.rounded_rectangle(
                (PAD, bar_y, PAD + max(filled, 8), bar_y + 16), radius=8, fill=colour
            )

        detail = (
            f"{diskspace.human_size(fs.used)} used  ·  "
            f"{diskspace.human_size(fs.free)} free  ·  "
            f"{diskspace.human_size(fs.total)} total"
        )
        draw.text((PAD, y + 62), detail, font=detail_font, fill=MUTED)

        kind = fs.fstype
        if fs.inode_percent is not None and fs.inode_percent >= 80:
            kind = f"{kind}  ·  inodes {fs.inode_percent:.0f}%"
        kind_w = draw.textlength(kind, font=detail_font)
        draw.text((WIDTH - PAD - kind_w, y + 62), kind, font=detail_font, fill=FAINT)

        if fs is not filesystems[-1]:
            draw.line((PAD, y + ROW - 16, WIDTH - PAD, y + ROW - 16), fill=(60, 63, 65, 255))
        y += ROW

    buffer = io.BytesIO()
    image.save(buffer, "PNG")
    return buffer.getvalue()
