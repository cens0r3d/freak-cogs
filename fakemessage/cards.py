"""Card renderers for the chat/timeline platforms fakemessage can fake.

Every card is built from the same small kit: a rich-text engine (inline bold /
italic / strike / mono with the platform's own markers), emoji pictures from the
Twemoji cache, circle avatars, and one downscale at the end. Layout is done in
device pixels (SCALE applied) so nothing drifts between measure and draw.

No ``discord`` imports: the cog passes avatar bytes, name and colours in.
"""

from __future__ import annotations

import io
from dataclasses import dataclass, replace
from typing import Dict, List, Optional, Sequence, Tuple

from PIL import Image, ImageDraw

from .render import (
    UNICODE_EMOJI_RE,
    _circle,
    _emoji_tile,
    _fit_emoji,
    _font,
)

SCALE = 2

# -- the rich text engine -------------------------------------------------


@dataclass(frozen=True)
class Style:
    bold: bool = False
    italic: bool = False
    strike: bool = False
    mono: bool = False

    def with_(self, flag: str) -> "Style":
        return replace(self, **{flag: True})


@dataclass
class Atom:
    text: str = ""
    style: Style = Style()
    image: Optional[Image.Image] = None
    width: int = 0
    newline: bool = False
    link: bool = False


# platform markdown, both the marker and the style it turns into
MARKERS = {
    "whatsapp": (("*", "bold"), ("_", "italic"), ("~", "strike"), ("`", "mono")),
    "telegram": (("**", "bold"), ("__", "italic"), ("~~", "strike"), ("`", "mono")),
    "twitter": (),
    "chat": (),
}


def _parse(text: str, markers: Sequence[Tuple[str, str]], style: Style = Style(), depth: int = 0):
    if not markers:
        return [(text, style)]
    out: List[Tuple[str, Style]] = []
    buffer = ""
    i = 0
    ordered = sorted(markers, key=lambda item: -len(item[0]))
    while i < len(text):
        matched = None
        if depth < 3:
            for marker, flag in ordered:
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
            out.append((buffer, style))
            buffer = ""
        out.extend(_parse(inner, markers, style.with_(flag), depth + 1))
        i = end + len(marker)
    if buffer:
        out.append((buffer, style))
    return out


class Rich:
    """A wrapped text block: measure it, then draw it."""

    def __init__(
        self,
        text: str,
        size: float,
        *,
        markers: Sequence[Tuple[str, str]] = (),
        images: Optional[Dict[str, Image.Image]] = None,
        line_height: Optional[float] = None,
        emoji_px: Optional[float] = None,
    ):
        self.device = int(round(size * SCALE))
        self.line = int(round((line_height or size * 1.33) * SCALE))
        self.emoji_px = int(round((emoji_px or size * 1.28) * SCALE))
        self.images = images or {}
        self.atoms = self._atoms(text, markers)
        self.lines: List[List[Tuple[int, Atom]]] = [[]]
        self.width = 0

    # -- atoms ---------------------------------------------------------
    def _atoms(self, text: str, markers) -> List[Atom]:
        atoms: List[Atom] = []
        for chunk, style in _parse(text, markers):
            pos = 0
            for match in UNICODE_EMOJI_RE.finditer(chunk):
                if match.start() > pos:
                    atoms.extend(self._text_atoms(chunk[pos : match.start()], style))
                image = self._emoji(match.group(0))
                if image is None:
                    atoms.extend(self._text_atoms(match.group(0), style))
                else:
                    atoms.append(Atom(image=image, width=image.width))
                pos = match.end()
            if pos < len(chunk):
                atoms.extend(self._text_atoms(chunk[pos:], style))
        return atoms

    def _emoji(self, cluster: str) -> Optional[Image.Image]:
        provided = _fit_emoji(self.images.get(cluster))
        tile = provided or _emoji_tile(cluster)
        if tile is None:
            return None
        return tile.resize((self.emoji_px, self.emoji_px), Image.Resampling.LANCZOS)

    def _text_atoms(self, text: str, style: Style) -> List[Atom]:
        """Words and spaces as separate atoms, newlines as their own marker."""
        atoms: List[Atom] = []
        for index, segment in enumerate(text.split("\n")):
            if index:
                atoms.append(Atom(newline=True))
            parts = segment.replace("\t", "    ").split(" ")
            for position, word in enumerate(parts):
                if position:
                    atoms.append(self._text_atom(" ", style))
                if word:
                    atoms.append(self._text_atom(word, style))
        return atoms

    def _text_atom(self, text: str, style: Style) -> Atom:
        link = text.startswith(("http://", "https://", "www."))
        return Atom(text, style, width=self._measure(text, style), link=link)

    def _measure(self, text: str, style: Style) -> int:
        font = _font(self.device, mono=style.mono, bold=style.bold, italic=style.italic)
        return int(font.getlength(text))

    # -- layout --------------------------------------------------------
    def wrap(self, limit: int) -> "Rich":
        self.lines = [[]]
        x = 0
        for atom in self.atoms:
            if atom.newline:
                self.lines.append([])
                x = 0
                continue
            if atom.width > limit and atom.text:
                for piece in self._break(atom, limit):
                    if x and x + piece.width > limit:
                        self.lines.append([])
                        x = 0
                    self.lines[-1].append((x, piece))
                    x += piece.width
                continue
            if x and x + atom.width > limit:
                if not atom.text.strip():
                    self.lines.append([])
                    x = 0
                    continue
                self.lines.append([])
                x = 0
            self.lines[-1].append((x, atom))
            x += atom.width
            self.width = max(self.width, x)
        while len(self.lines) > 1 and not self.lines[-1]:
            self.lines.pop()
        return self

    def _break(self, atom: Atom, limit: int) -> List[Atom]:
        pieces: List[Atom] = []
        current = ""
        for ch in atom.text:
            trial = current + ch
            if current and self._measure(trial, atom.style) > limit:
                pieces.append(self._text_atom(current, atom.style))
                current = ch
            else:
                current = trial
        if current:
            pieces.append(self._text_atom(current, atom.style))
        return pieces

    @property
    def height(self) -> int:
        return len(self.lines) * self.line

    # -- draw ----------------------------------------------------------
    def draw(
        self,
        canvas: Image.Image,
        x: int,
        y: int,
        colour,
        *,
        link_colour=None,
        first_line: int = 0,
    ) -> None:
        draw = ImageDraw.Draw(canvas)
        for index, line in enumerate(self.lines):
            top = y + (index + first_line) * self.line
            for offset, atom in line:
                if atom.image is not None:
                    canvas.paste(
                        atom.image,
                        (x + offset, top + int(self.line * 0.86) - atom.image.height),
                        atom.image,
                    )
                    continue
                style = atom.style
                font = _font(self.device, mono=style.mono, bold=style.bold, italic=style.italic)
                ascent, descent = font.getmetrics()
                base = top + int((self.line - (ascent + descent)) / 2) + ascent
                fill = link_colour if (atom.link and link_colour) else colour
                draw.text((x + offset, base), atom.text, font=font, fill=fill, anchor="ls")
                if style.strike:
                    mid = base - int(ascent * 0.32)
                    draw.line(
                        (x + offset, mid, x + offset + atom.width, mid),
                        fill=fill,
                        width=max(1, SCALE),
                    )


# -- shared bits ---------------------------------------------------------


def _png(canvas: Image.Image, width: int, height: int) -> bytes:
    final = canvas.resize((int(width / SCALE), int(height / SCALE)), Image.Resampling.LANCZOS)
    buffer = io.BytesIO()
    final.save(buffer, format="PNG", optimize=True)
    return buffer.getvalue()


def _avatar(image: Optional[bytes], name: str, size: int, colour=(88, 101, 242)) -> Image.Image:
    if image:
        try:
            loaded = Image.open(io.BytesIO(image))
            loaded.load()
            return _circle(loaded, size)
        except (OSError, ValueError):
            pass
    tile = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(tile)
    draw.ellipse((0, 0, size - 1, size - 1), fill=colour)
    letter = (name or "?").strip()[:1].upper() or "?"
    draw.text(
        (size / 2, size / 2),
        letter,
        font=_font(int(size * 0.45), bold=True),
        fill=(255, 255, 255),
        anchor="mm",
    )
    return tile


def _bubble(draw: ImageDraw.ImageDraw, box, fill, radius, tail_side: Optional[str] = None):
    x0, y0, x1, y1 = box
    draw.rounded_rectangle((x0, y0, x1, y1), radius=radius, fill=fill)
    if tail_side == "left":
        draw.polygon([(x0, y1 - radius), (x0 - radius, y1), (x0 + radius // 2, y1)], fill=fill)
    elif tail_side == "right":
        draw.polygon([(x1, y1 - radius), (x1 + radius, y1), (x1 - radius // 2, y1)], fill=fill)


# -- WhatsApp ------------------------------------------------------------

WA_BG = (11, 20, 26)
WA_HEADER = (32, 44, 51)
WA_IN = (32, 44, 51)
WA_OUT = (0, 92, 75)
WA_TEXT = (233, 237, 239)
WA_MUTED = (134, 150, 160)
WA_TICK = (83, 189, 235)


def whatsapp(
    *,
    name: str,
    message: str,
    time: str = "21:07",
    outgoing: bool = False,
    avatar: Optional[bytes] = None,
    status: str = "online",
    images: Optional[Dict[str, Image.Image]] = None,
) -> bytes:
    """One chat bubble under a WhatsApp header. Width ~ 420 css px."""
    width = 420 * SCALE
    header = 60 * SCALE
    size = 14.5
    body = Rich(message, size, markers=MARKERS["whatsapp"], images=images, line_height=19.5)
    text_limit = 300 * SCALE
    body.wrap(text_limit)

    pad_x = 9 * SCALE
    pad_y = 7 * SCALE
    time_font = _font(int(10.5 * SCALE))
    stamp_w = int(time_font.getlength(time)) + (16 * SCALE if outgoing else 0)
    last_w = (body.lines[-1][-1][0] + body.lines[-1][-1][1].width) if body.lines and body.lines[-1] else 0
    others = max([line[-1][0] + line[-1][1].width for line in body.lines[:-1]] or [0])
    # WhatsApp appends the time to the last line and widens the bubble for it;
    # when the last line already fills the column the stamp gets its own row
    # instead of sitting on the letters.
    tight = last_w + stamp_w + 6 * SCALE > text_limit
    extra = 16 * SCALE if tight else 0
    text_w = body.width if tight else max(others, last_w + stamp_w + 8 * SCALE)
    bubble_w = text_w + pad_x * 2
    bubble_h = body.height + pad_y * 2 + extra

    date_h = 34 * SCALE
    bubble_top = header + date_h
    height = bubble_top + bubble_h + 18 * SCALE

    canvas = Image.new("RGB", (width, height), WA_BG)
    draw = ImageDraw.Draw(canvas)

    # header
    draw.rectangle((0, 0, width, header), fill=WA_HEADER)
    back_x = 26 * SCALE
    draw.line(
        (back_x + 6 * SCALE, header / 2 - 7 * SCALE, back_x - 4 * SCALE, header / 2),
        fill=WA_TEXT,
        width=2 * SCALE,
    )
    draw.line(
        (back_x - 4 * SCALE, header / 2, back_x + 6 * SCALE, header / 2 + 7 * SCALE),
        fill=WA_TEXT,
        width=2 * SCALE,
    )
    avatar_size = 38 * SCALE
    tile = _avatar(avatar, name, avatar_size, (0, 168, 132))
    canvas.paste(tile, (52 * SCALE, (header - avatar_size) // 2), tile)
    name_font = _font(int(16 * SCALE), bold=True)
    draw.text((100 * SCALE, header / 2 - 6 * SCALE), name, font=name_font, fill=WA_TEXT, anchor="ls")
    draw.text((100 * SCALE, header / 2 + 12 * SCALE), status, font=_font(int(12 * SCALE)), fill=WA_MUTED, anchor="ls")

    # video, call and overflow, so the header does not read as cropped
    icon_y = header / 2
    video_x = width - 118 * SCALE
    draw.rounded_rectangle(
        (video_x, icon_y - 6 * SCALE, video_x + 13 * SCALE, icon_y + 6 * SCALE),
        radius=3 * SCALE,
        outline=WA_TEXT,
        width=2 * SCALE,
    )
    draw.polygon(
        [
            (video_x + 15 * SCALE, icon_y - 5 * SCALE),
            (video_x + 21 * SCALE, icon_y),
            (video_x + 15 * SCALE, icon_y + 5 * SCALE),
        ],
        fill=WA_TEXT,
    )
    phone_x = width - 76 * SCALE
    draw.arc(
        (phone_x, icon_y - 10 * SCALE, phone_x + 20 * SCALE, icon_y + 10 * SCALE),
        start=210,
        end=330,
        fill=WA_TEXT,
        width=2 * SCALE,
    )
    draw.line(
        (phone_x + 3 * SCALE, icon_y - 8 * SCALE, phone_x + 3 * SCALE, icon_y - 3 * SCALE),
        fill=WA_TEXT,
        width=4 * SCALE,
    )
    draw.line(
        (phone_x + 17 * SCALE, icon_y - 8 * SCALE, phone_x + 17 * SCALE, icon_y - 3 * SCALE),
        fill=WA_TEXT,
        width=4 * SCALE,
    )
    for i in range(3):
        dot_y = icon_y - 6 * SCALE + i * 6 * SCALE
        draw.ellipse(
            (width - 28 * SCALE, dot_y, width - 24 * SCALE, dot_y + 4 * SCALE),
            fill=WA_TEXT,
        )

    # date chip
    chip_font = _font(int(10.5 * SCALE), bold=True)
    chip_w = int(chip_font.getlength("TODAY")) + 16 * SCALE
    chip_x = (width - chip_w) // 2
    draw.rounded_rectangle(
        (chip_x, header + 10 * SCALE, chip_x + chip_w, header + 10 * SCALE + 20 * SCALE),
        radius=5 * SCALE,
        fill=(24, 34, 41),
    )
    draw.text(
        (width / 2, header + 20 * SCALE),
        "TODAY",
        font=chip_font,
        fill=WA_MUTED,
        anchor="mm",
    )

    # bubble
    if outgoing:
        left = width - 12 * SCALE - bubble_w
        fill = WA_OUT
        tail = "right"
    else:
        left = 12 * SCALE
        fill = WA_IN
        tail = "left"
    box = (left, bubble_top, left + bubble_w, bubble_top + bubble_h)
    _bubble(draw, box, fill, 8 * SCALE, tail)
    body.draw(canvas, left + pad_x, bubble_top + pad_y, WA_TEXT, link_colour=(83, 189, 235))

    # time + ticks, bottom right of the bubble
    stamp_x = (
        left + bubble_w - pad_x - stamp_w if tight else left + pad_x + last_w + 8 * SCALE
    )
    stamp_y = bubble_top + pad_y + body.height + extra - 3 * SCALE
    draw.text((stamp_x, stamp_y), time, font=time_font, fill=WA_MUTED, anchor="ls")
    if outgoing:
        tick_x = stamp_x + int(time_font.getlength(time)) + 5 * SCALE
        tick_y = stamp_y - 4 * SCALE
        for shift in (0, 4 * SCALE):
            draw.line(
                (tick_x + shift, tick_y - 4 * SCALE, tick_x + shift + 4 * SCALE, tick_y),
                fill=WA_TICK,
                width=2 * SCALE,
            )
            draw.line(
                (tick_x + shift + 4 * SCALE, tick_y, tick_x + shift + 10 * SCALE, tick_y - 7 * SCALE),
                fill=WA_TICK,
                width=2 * SCALE,
            )
    return _png(canvas, width, height)


# -- Telegram ------------------------------------------------------------

TG_BG = (14, 22, 33)
TG_HEADER = (23, 33, 43)
TG_IN = (24, 37, 51)
TG_OUT = (43, 82, 120)
TG_TEXT = (255, 255, 255)
TG_MUTED = (125, 139, 153)


def telegram(
    *,
    name: str,
    message: str,
    time: str = "21:07",
    outgoing: bool = False,
    avatar: Optional[bytes] = None,
    status: str = "online",
    images: Optional[Dict[str, Image.Image]] = None,
) -> bytes:
    """One Telegram bubble under the chat header. Width ~ 420 css px."""
    width = 420 * SCALE
    header = 56 * SCALE
    size = 14.5
    body = Rich(message, size, markers=MARKERS["telegram"], images=images, line_height=19.5)
    body.wrap(300 * SCALE)

    pad_x = 10 * SCALE
    pad_y = 8 * SCALE
    time_font = _font(int(10.5 * SCALE))
    stamp_w = int(time_font.getlength(time)) + (14 * SCALE if outgoing else 0)
    last_w = (body.lines[-1][-1][0] + body.lines[-1][-1][1].width) if body.lines and body.lines[-1] else 0
    others = max([line[-1][0] + line[-1][1].width for line in body.lines[:-1]] or [0])
    tight = last_w + stamp_w + 6 * SCALE > 300 * SCALE
    extra = 15 * SCALE if tight else 0
    text_w = body.width if tight else max(others, last_w + stamp_w + 8 * SCALE)
    bubble_w = text_w + pad_x * 2
    bubble_h = body.height + pad_y * 2 + extra + 2 * SCALE

    bubble_top = header + 14 * SCALE
    height = bubble_top + bubble_h + 20 * SCALE

    canvas = Image.new("RGB", (width, height), TG_BG)
    draw = ImageDraw.Draw(canvas)

    draw.rectangle((0, 0, width, header), fill=TG_HEADER)
    draw.line((0, header, width, header), fill=(11, 18, 27), width=SCALE)
    avatar_size = 38 * SCALE
    tile = _avatar(avatar, name, avatar_size, (82, 136, 193))
    canvas.paste(tile, (14 * SCALE, (header - avatar_size) // 2), tile)
    name_font = _font(int(16 * SCALE), bold=True)
    draw.text((62 * SCALE, header / 2 - 5 * SCALE), name, font=name_font, fill=TG_TEXT, anchor="ls")
    draw.text((62 * SCALE, header / 2 + 12 * SCALE), status, font=_font(int(12 * SCALE)), fill=TG_MUTED, anchor="ls")
    for i in range(3):
        y = header / 2 - 6 * SCALE + i * 6 * SCALE
        draw.ellipse(
            (width - 24 * SCALE, y, width - 21 * SCALE, y + 3 * SCALE),
            fill=TG_MUTED,
        )

    if outgoing:
        left = width - 10 * SCALE - bubble_w
        fill = TG_OUT
        tail = "right"
    else:
        left = 10 * SCALE
        fill = TG_IN
        tail = "left"
    box = (left, bubble_top, left + bubble_w, bubble_top + bubble_h)
    _bubble(draw, box, fill, 12 * SCALE, tail)
    body.draw(canvas, left + pad_x, bubble_top + pad_y, TG_TEXT, link_colour=(106, 178, 242))

    stamp_x = (
        left + bubble_w - pad_x - stamp_w if tight else left + pad_x + last_w + 8 * SCALE
    )
    stamp_y = bubble_top + pad_y + body.height + extra - 2 * SCALE
    draw.text((stamp_x, stamp_y), time, font=time_font, fill=(160, 176, 190), anchor="ls")
    if outgoing:
        tick_x = stamp_x + int(time_font.getlength(time)) + 4 * SCALE
        tick_y = stamp_y - 3 * SCALE
        for shift in (0, 4 * SCALE):
            draw.line(
                (tick_x + shift, tick_y - 4 * SCALE, tick_x + shift + 4 * SCALE, tick_y),
                fill=(120, 170, 220),
                width=2 * SCALE,
            )
            draw.line(
                (tick_x + shift + 4 * SCALE, tick_y, tick_x + shift + 9 * SCALE, tick_y - 7 * SCALE),
                fill=(120, 170, 220),
                width=2 * SCALE,
            )
    return _png(canvas, width, height)


# -- X / Twitter ---------------------------------------------------------


def _icon(draw: ImageDraw.ImageDraw, kind: str, x: int, y: int, colour, unit: int) -> None:
    """The engagement row icons, drawn as outlines so they read at small size."""
    width = max(1, int(unit * 0.16))
    if kind == "reply":
        draw.rounded_rectangle((x, y, x + unit - 4, y + unit - 6), radius=unit // 4, outline=colour, width=width)
        draw.polygon(
            [(x + unit // 3, y + unit - 6), (x + unit // 3, y + unit), (x + unit // 2, y + unit - 6)],
            fill=colour,
        )
    elif kind == "retweet":
        top = y + 2
        bottom = y + unit - 2
        draw.line((x + 2, top + unit // 5, x + unit - 6, top + unit // 5), fill=colour, width=width)
        draw.polygon(
            [(x + unit - 2, top + unit // 5), (x + unit - 8, top), (x + unit - 8, top + unit * 0.4)],
            fill=colour,
        )
        draw.line((x + 2, bottom - unit // 5, x + unit - 6, bottom - unit // 5), fill=colour, width=width)
        draw.polygon(
            [(x + 2, bottom - unit // 5), (x + 8, bottom - unit * 0.4), (x + 8, bottom)],
            fill=colour,
        )
    elif kind == "like":
        points = []
        cx, cy = x + unit / 2, y + unit / 2
        radius = unit / 2
        import math

        for step in range(48):
            angle = math.pi * 2 * step / 48
            hx = 16 * math.sin(angle) ** 3
            hy = 13 * math.cos(angle) - 5 * math.cos(2 * angle) - 2 * math.cos(3 * angle) - math.cos(4 * angle)
            points.append((cx + hx * radius / 18, cy - hy * radius / 18))
        points.append(points[0])
        draw.line(points, fill=colour, width=width, joint="curve")
    elif kind == "bookmark":
        draw.line(
            [(x + 3, y + 1), (x + unit - 3, y + 1), (x + unit - 3, y + unit), (x + unit / 2, y + unit - 5), (x + 3, y + unit), (x + 3, y + 1)],
            fill=colour,
            width=width,
            joint="curve",
        )
    elif kind == "share":
        draw.line((x + 1, y + unit - 4, x + unit - 4, y + 1), fill=colour, width=width)
        draw.polygon(
            [(x + unit - 1, y), (x + unit - 1, y + 8), (x + unit - 9, y)],
            fill=colour,
        )
        draw.line((x + 1, y + unit - 4, x + unit // 2, y + unit - 4), fill=colour, width=width)
    elif kind == "views":
        for index in range(3):
            bar_x = x + index * (unit // 3)
            bar_h = unit * (0.4 + 0.25 * index)
            draw.line((bar_x, y + unit - bar_h, bar_x, y + unit), fill=colour, width=max(1, width))


def tweet(
    *,
    name: str,
    handle: str,
    message: str,
    time: str = "21:07",
    date: str = "",
    avatar: Optional[bytes] = None,
    verified: bool = False,
    replies: str = "128",
    retweets: str = "1.2K",
    likes: str = "8.4K",
    views: str = "412K",
    images: Optional[Dict[str, Image.Image]] = None,
) -> bytes:
    """One post on X's lights-out dark theme. Width ~ 600 css px."""
    width = 600 * SCALE
    pad = 16 * SCALE
    gutter = 76 * SCALE
    text_limit = width - gutter - pad

    body = Rich(message, 15, markers=MARKERS["twitter"], images=images, line_height=20)
    body.wrap(text_limit)

    header_y = pad
    text_y = header_y + 26 * SCALE
    meta_y = text_y + body.height + 12 * SCALE
    row_y = meta_y + 30 * SCALE
    height = row_y + 20 * SCALE + pad

    canvas = Image.new("RGB", (width, height), (0, 0, 0))
    draw = ImageDraw.Draw(canvas)

    avatar_size = 48 * SCALE
    tile = _avatar(avatar, name, avatar_size, (29, 155, 240))
    canvas.paste(tile, (pad, header_y), tile)

    name_font = _font(int(15 * SCALE), bold=True)
    x = gutter
    draw.text((x, header_y + 18 * SCALE), name, font=name_font, fill=(231, 233, 234), anchor="ls")
    x += int(name_font.getlength(name)) + 3 * SCALE
    if verified:
        check_size = 15 * SCALE
        draw.ellipse(
            (x, header_y + 4 * SCALE, x + check_size, header_y + 4 * SCALE + check_size),
            fill=(29, 155, 240),
        )
        cx, cy = x + check_size / 2, header_y + 4 * SCALE + check_size / 2
        draw.line(
            (cx - check_size * 0.28, cy, cx - check_size * 0.06, cy + check_size * 0.22),
            fill=(255, 255, 255),
            width=max(1, int(check_size * 0.14)),
        )
        draw.line(
            (cx - check_size * 0.06, cy + check_size * 0.22, cx + check_size * 0.30, cy - check_size * 0.20),
            fill=(255, 255, 255),
            width=max(1, int(check_size * 0.14)),
        )
        x += check_size + 4 * SCALE
    handle_font = _font(int(15 * SCALE))
    draw.text((x, header_y + 18 * SCALE), "@" + handle.lstrip("@"), font=handle_font, fill=(113, 118, 123), anchor="ls")

    body.draw(canvas, gutter, text_y, (231, 233, 234), link_colour=(29, 155, 240))

    meta_font = _font(int(13 * SCALE))
    meta = time + (" · " + date if date else "")
    draw.text((gutter, meta_y + 14 * SCALE), meta, font=meta_font, fill=(113, 118, 123), anchor="ls")

    unit = 17 * SCALE
    colour = (113, 118, 123)
    entries = (("reply", replies), ("retweet", retweets), ("like", likes), ("views", views))
    step = (width - pad * 2 - 2 * unit) / len(entries)
    for index, (kind, count) in enumerate(entries):
        base_x = int(pad + index * step)
        _icon(draw, kind, base_x, row_y, colour, unit)
        draw.text(
            (base_x + unit + 7 * SCALE, row_y + unit / 2),
            count,
            font=_font(int(13 * SCALE)),
            fill=colour,
            anchor="lm",
        )
    _icon(draw, "bookmark", width - pad - 2 * unit - 10 * SCALE, row_y, colour, unit)
    _icon(draw, "share", width - pad - unit, row_y, colour, unit)

    # the separator belongs under a post, not above it
    draw.line((0, height - 1, width, height - 1), fill=(47, 51, 54), width=SCALE)
    return _png(canvas, width, height)


# -- Twitch / Kick chat --------------------------------------------------


TWITCH_COLOURS = (
    (255, 69, 0),
    (0, 255, 127),
    (30, 144, 255),
    (218, 165, 32),
    (255, 105, 180),
    (157, 126, 255),
    (0, 255, 255),
    (255, 127, 80),
)


def _name_colour(name: str, fallback: Tuple[int, int, int]) -> Tuple[int, int, int]:
    if not name:
        return fallback
    return TWITCH_COLOURS[sum(ord(ch) for ch in name) % len(TWITCH_COLOURS)]


def chat(
    *,
    name: str,
    message: str,
    platform: str = "twitch",
    colour: Optional[Tuple[int, int, int]] = None,
    badges: Sequence[str] = (),
    width: int = 340,
    images: Optional[Dict[str, Image.Image]] = None,
    avatar: Optional[bytes] = None,  # chat columns have no avatars; kept for the caller
) -> bytes:
    """Chat lines for Twitch or Kick: one line per message line."""
    brand = (0, 255, 127) if platform == "kick" else (145, 71, 255)
    text_colour = (239, 239, 241)
    bg = (24, 24, 27) if platform == "twitch" else (19, 19, 22)
    username_colour = colour or (_name_colour(name, (255, 255, 255)) if platform == "twitch" else brand)

    device_w = width * SCALE
    pad = 8 * SCALE
    line_h = 18 * SCALE
    name_font = _font(int(13 * SCALE), bold=True)
    badge_font = _font(int(9 * SCALE), bold=True)
    badge_labels = {
        "mod": ("MOD", (0, 173, 3)),
        "sub": ("SUB", (145, 71, 255) if platform == "twitch" else (83, 252, 24)),
        "vip": ("VIP", (224, 5, 145)),
        "founder": ("FOUNDER", (145, 71, 255)),
    }

    name_w = int(name_font.getlength(name + ": "))
    # wrapped lines continue under the message, like the real chat column
    limit = max(int(device_w * 0.45), device_w - pad * 2 - name_w)

    lines: List[Rich] = []
    for raw in message.split("\n")[:6]:
        if raw.strip():
            lines.append(Rich(raw, 13, markers=(), images=images, line_height=18).wrap(limit))
    if not lines:
        lines = [Rich(" ", 13, line_height=18).wrap(limit)]

    rows = [max(18 * SCALE, rich.height) + 3 * SCALE for rich in lines]
    height = pad * 2 + sum(rows)

    canvas = Image.new("RGB", (device_w, height), bg)
    draw = ImageDraw.Draw(canvas)
    ascent, descent = name_font.getmetrics()

    y = pad
    for rich, row in zip(lines, rows):
        x = pad
        for badge in badges:
            label, badge_colour = badge_labels.get(badge, (badge.upper()[:7], (90, 90, 96)))
            badge_w = int(badge_font.getlength(label)) + 8 * SCALE
            draw.rounded_rectangle(
                (x, y + 3 * SCALE, x + badge_w, y + 3 * SCALE + 14 * SCALE),
                radius=3 * SCALE,
                fill=badge_colour,
            )
            draw.text(
                (x + badge_w / 2, y + 10 * SCALE),
                label,
                font=badge_font,
                fill=(12, 12, 14) if platform == "kick" else (255, 255, 255),
                anchor="mm",
            )
            x += badge_w + 4 * SCALE
        base = y + int((line_h - (ascent + descent)) / 2) + ascent
        draw.text((x, base), name + ":", font=name_font, fill=username_colour, anchor="ls")
        rich.draw(canvas, x + name_w, y, text_colour)
        y += row
    return _png(canvas, device_w, height)
