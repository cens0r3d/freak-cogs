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


def _fit_image(
    data: Optional[bytes], max_w: int, max_h: int, radius: float = 6
) -> Optional[Image.Image]:
    """Decode an attachment and fit it into the card, corners rounded."""
    if not data:
        return None
    try:
        image = Image.open(io.BytesIO(data))
        image.load()
    except (OSError, ValueError):
        return None
    image = image.convert("RGB")
    if image.width <= 0 or image.height <= 0:
        return None
    scale = min(max_w / image.width, max_h / image.height, 2.0)
    image = image.resize(
        (max(1, int(image.width * scale)), max(1, int(image.height * scale))),
        Image.Resampling.LANCZOS,
    )
    mask = Image.new("L", (image.width * 4, image.height * 4), 0)
    ImageDraw.Draw(mask).rounded_rectangle(
        (0, 0, image.width * 4 - 1, image.height * 4 - 1), radius=int(radius * 4), fill=255
    )
    mask = mask.resize(image.size, Image.Resampling.LANCZOS)
    out = Image.new("RGBA", image.size, (0, 0, 0, 0))
    out.paste(image.convert("RGBA"), (0, 0), mask)
    return out


def _top_rounded(image: Image.Image, radius: float) -> Image.Image:
    """Round only the top corners, so a photo sits flush in a bubble."""
    mask = Image.new("L", (image.width * 4, image.height * 4), 0)
    ImageDraw.Draw(mask).rounded_rectangle(
        (0, 0, image.width * 4 - 1, image.height * 4 - 1),
        radius=int(radius * 4),
        fill=255,
    )
    ImageDraw.Draw(mask).rectangle(
        (0, int(radius * 4), image.width * 4, image.height * 4), fill=255
    )
    mask = mask.resize(image.size, Image.Resampling.LANCZOS)
    out = Image.new("RGBA", image.size, (0, 0, 0, 0))
    out.paste(image.convert("RGBA"), (0, 0), mask)
    return out


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
    image: Optional[bytes] = None,
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
    photo = _fit_image(image, text_limit, 320 * SCALE)
    if photo is not None:
        target = max(body.width + pad_x * 2, photo.width)
        tall = int(photo.height * target / photo.width)
        photo = _top_rounded(photo.resize((target, tall), Image.Resampling.LANCZOS), 8)
        photo_h = tall + 6 * SCALE
        bubble_w = target
        tight, extra = True, 16 * SCALE
    else:
        photo_h = 0
        bubble_w = text_w + pad_x * 2
    bubble_h = body.height + pad_y * 2 + extra + photo_h

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
    if photo is not None:
        canvas.paste(photo, (left, bubble_top), photo)
    body.draw(
        canvas,
        left + pad_x,
        bubble_top + pad_y + photo_h,
        WA_TEXT,
        link_colour=(83, 189, 235),
    )

    # time + ticks, bottom right of the bubble
    stamp_x = (
        left + bubble_w - pad_x - stamp_w if tight else left + pad_x + last_w + 8 * SCALE
    )
    stamp_y = bubble_top + pad_y + photo_h + body.height + extra - 3 * SCALE
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
    image: Optional[bytes] = None,
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
    photo = _fit_image(image, 300 * SCALE, 320 * SCALE)
    if photo is not None:
        target = max(body.width + pad_x * 2, photo.width)
        tall = int(photo.height * target / photo.width)
        photo = _top_rounded(photo.resize((target, tall), Image.Resampling.LANCZOS), 12)
        photo_h = tall + 6 * SCALE
        bubble_w = target
        tight, extra = True, 15 * SCALE
    else:
        photo_h = 0
        bubble_w = text_w + pad_x * 2
    bubble_h = body.height + pad_y * 2 + extra + 2 * SCALE + photo_h

    bubble_top = header + 14 * SCALE
    height = bubble_top + bubble_h + 20 * SCALE

    canvas = Image.new("RGB", (width, height), TG_BG)
    draw = ImageDraw.Draw(canvas)

    draw.rectangle((0, 0, width, header), fill=TG_HEADER)
    draw.line((0, header, width, header), fill=(11, 18, 27), width=SCALE)
    draw.line(
        (24 * SCALE, header / 2 - 6 * SCALE, 16 * SCALE, header / 2),
        fill=TG_MUTED,
        width=2 * SCALE,
    )
    draw.line(
        (16 * SCALE, header / 2, 24 * SCALE, header / 2 + 6 * SCALE),
        fill=TG_MUTED,
        width=2 * SCALE,
    )
    avatar_size = 38 * SCALE
    tile = _avatar(avatar, name, avatar_size, (82, 136, 193))
    canvas.paste(tile, (34 * SCALE, (header - avatar_size) // 2), tile)
    name_font = _font(int(16 * SCALE), bold=True)
    draw.text((82 * SCALE, header / 2 - 5 * SCALE), name, font=name_font, fill=TG_TEXT, anchor="ls")
    draw.text((82 * SCALE, header / 2 + 12 * SCALE), status, font=_font(int(12 * SCALE)), fill=TG_MUTED, anchor="ls")
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
    if photo is not None:
        canvas.paste(photo, (left, bubble_top), photo)
    body.draw(
        canvas,
        left + pad_x,
        bubble_top + pad_y + photo_h,
        TG_TEXT,
        link_colour=(106, 178, 242),
    )

    stamp_x = (
        left + bubble_w - pad_x - stamp_w if tight else left + pad_x + last_w + 8 * SCALE
    )
    stamp_y = bubble_top + pad_y + photo_h + body.height + extra - 2 * SCALE
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
    image: Optional[bytes] = None,
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
    photo = _fit_image(image, text_limit, 300 * SCALE, radius=16)
    photo_h = (photo.height + 12 * SCALE) if photo is not None else 0
    meta_y = text_y + body.height + photo_h + 12 * SCALE
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
    if photo is not None:
        canvas.paste(photo, (gutter, text_y + body.height + 12 * SCALE), photo)

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
    image: Optional[bytes] = None,  # nor pictures; accepted so one dispatcher fits all
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

# -- Facebook ------------------------------------------------------------

FB_BG = (36, 37, 38)
FB_TEXT = (228, 230, 235)
FB_MUTED = (176, 179, 184)
FB_LINE = (62, 64, 66)
FB_BLUE = (24, 119, 242)


def _emoji_pic(images, cluster: str, size: int) -> Optional[Image.Image]:
    supplied = (images or {}).get(cluster)
    tile = _fit_emoji(supplied) or _emoji_tile(cluster)
    if tile is None:
        return None
    return tile.resize((size, size), Image.Resampling.LANCZOS)


def _thumb(draw: ImageDraw.ImageDraw, x: float, y: float, size: float, fill) -> None:
    """A tiny thumbs-up: palm, thumb and a wrist, readable at 16 px."""
    unit = size / 16
    draw.rounded_rectangle(
        (x + 1 * unit, y + 7 * unit, x + 9 * unit, y + 15 * unit),
        radius=2 * unit,
        fill=fill,
    )
    draw.rounded_rectangle(
        (x + 3 * unit, y + 2 * unit, x + 7 * unit, y + 8 * unit),
        radius=2 * unit,
        fill=fill,
    )
    draw.rounded_rectangle(
        (x + 9 * unit, y + 8 * unit, x + 15 * unit, y + 15 * unit),
        radius=2 * unit,
        fill=fill,
    )


def _heart(draw: ImageDraw.ImageDraw, x: float, y: float, size: float, fill) -> None:
    unit = size / 16
    draw.ellipse((x + 1 * unit, y + 2 * unit, x + 8 * unit, y + 9 * unit), fill=fill)
    draw.ellipse((x + 8 * unit, y + 2 * unit, x + 15 * unit, y + 9 * unit), fill=fill)
    draw.polygon(
        [(x + 1.5 * unit, y + 7 * unit), (x + 14.5 * unit, y + 7 * unit), (x + 8 * unit, y + 15 * unit)],
        fill=fill,
    )


def _fb_icon(draw: ImageDraw.ImageDraw, kind: str, x: float, y: float, unit: float, fill) -> None:
    width = max(1, int(unit * 0.11))
    if kind == "like":
        draw.rounded_rectangle(
            (x + unit * 0.08, y + unit * 0.55, x + unit * 0.62, y + unit),
            radius=unit * 0.12,
            outline=fill,
            width=width,
        )
        draw.rounded_rectangle(
            (x + unit * 0.22, y + unit * 0.12, x + unit * 0.48, y + unit * 0.62),
            radius=unit * 0.12,
            outline=fill,
            width=width,
        )
    elif kind == "comment":
        draw.rounded_rectangle(
            (x + unit * 0.05, y + unit * 0.12, x + unit * 0.95, y + unit * 0.78),
            radius=unit * 0.22,
            outline=fill,
            width=width,
        )
        draw.polygon(
            [
                (x + unit * 0.28, y + unit * 0.76),
                (x + unit * 0.28, y + unit),
                (x + unit * 0.52, y + unit * 0.76),
            ],
            fill=fill,
        )
    elif kind == "share":
        draw.line(
            (x + unit * 0.08, y + unit * 0.92, x + unit * 0.55, y + unit * 0.92),
            fill=fill,
            width=width,
        )
        draw.line(
            (x + unit * 0.55, y + unit * 0.92, x + unit * 0.55, y + unit * 0.28),
            fill=fill,
            width=width,
        )
        draw.line(
            (x + unit * 0.55, y + unit * 0.28, x + unit * 0.30, y + unit * 0.52),
            fill=fill,
            width=width,
        )
        draw.line(
            (x + unit * 0.55, y + unit * 0.28, x + unit * 0.80, y + unit * 0.52),
            fill=fill,
            width=width,
        )


def facebook(
    *,
    name: str,
    message: str,
    time: str = "Just now",
    avatar: Optional[bytes] = None,
    privacy: str = "Public",
    reactions: str = "1.4K",
    comments: str = "86",
    shares: str = "12",
    image: Optional[bytes] = None,
    images: Optional[Dict[str, Image.Image]] = None,
) -> bytes:
    """One Facebook post: header, text, reaction bubbles and the action bar."""
    width = 500 * SCALE
    pad = 16 * SCALE
    gutter = 68 * SCALE
    text_limit = width - gutter - pad

    body = Rich(message, 15, markers=MARKERS["chat"], images=images, line_height=20)
    body.wrap(text_limit)

    header_y = pad
    text_y = header_y + 58 * SCALE
    photo = _fit_image(image, text_limit, 300 * SCALE, radius=8)
    photo_h = (photo.height + 10 * SCALE) if photo is not None else 0
    react_y = text_y + body.height + photo_h + 12 * SCALE
    divider_y = react_y + 28 * SCALE
    height = divider_y + 44 * SCALE

    canvas = Image.new("RGB", (width, height), FB_BG)
    draw = ImageDraw.Draw(canvas)

    size = 40 * SCALE
    tile = _avatar(avatar, name, size, FB_BLUE)
    canvas.paste(tile, (pad, header_y), tile)

    name_font = _font(int(15 * SCALE), bold=True)
    draw.text((gutter, header_y + 18 * SCALE), name, font=name_font, fill=FB_TEXT, anchor="ls")
    meta_font = _font(int(12 * SCALE))
    meta = "{} · ".format(time)
    draw.text((gutter, header_y + 38 * SCALE), meta, font=meta_font, fill=FB_MUTED, anchor="ls")
    globe_x = gutter + int(meta_font.getlength(meta)) + 2 * SCALE
    globe_y = header_y + 38 * SCALE
    radius = 5 * SCALE
    draw.ellipse(
        (globe_x, globe_y - radius * 2, globe_x + radius * 2, globe_y),
        outline=FB_MUTED,
        width=max(1, SCALE),
    )
    draw.ellipse(
        (globe_x + radius * 0.35, globe_y - radius * 2, globe_x + radius * 1.65, globe_y),
        outline=FB_MUTED,
        width=max(1, SCALE),
    )
    draw.line(
        (globe_x, globe_y - radius, globe_x + radius * 2, globe_y - radius),
        fill=FB_MUTED,
        width=SCALE,
    )
    draw.text((globe_x + radius * 2 + 6 * SCALE, globe_y), privacy, font=meta_font, fill=FB_MUTED, anchor="ls")

    # overflow menu
    for index in range(3):
        cx = width - pad - 8 * SCALE
        cy = header_y + 10 * SCALE + index * 5 * SCALE
        draw.ellipse((cx, cy, cx + 3 * SCALE, cy + 3 * SCALE), fill=FB_MUTED)

    body.draw(canvas, gutter, text_y, FB_TEXT, link_colour=(69, 155, 240))
    if photo is not None:
        canvas.paste(photo, (gutter, text_y + body.height + 10 * SCALE), photo)

    # reaction bubbles: like in blue, love in red, overlapping
    bubble = 18 * SCALE
    x = gutter
    for kind, colour in (("like", FB_BLUE), ("heart", (244, 51, 74))):
        draw.ellipse((x, react_y, x + bubble, react_y + bubble), fill=colour)
        if kind == "like":
            _thumb(draw, x + bubble * 0.18, react_y + bubble * 0.16, bubble * 0.64, (255, 255, 255))
        else:
            _heart(draw, x + bubble * 0.18, react_y + bubble * 0.16, bubble * 0.64, (255, 255, 255))
        x += bubble - 4 * SCALE
    draw.text(
        (x + 6 * SCALE, react_y + bubble - 3 * SCALE),
        reactions,
        font=_font(int(13 * SCALE)),
        fill=FB_MUTED,
        anchor="ls",
    )
    draw.text(
        (width - pad, react_y + bubble - 3 * SCALE),
        "{} comments · {} shares".format(comments, shares),
        font=_font(int(13 * SCALE)),
        fill=FB_MUTED,
        anchor="rs",
    )

    draw.line((pad, divider_y, width - pad, divider_y), fill=FB_LINE, width=SCALE)

    labels = (("like", "Like"), ("comment", "Comment"), ("share", "Share"))
    step = (width - pad * 2) / len(labels)
    bar_font = _font(int(14 * SCALE), bold=True)
    unit = 16 * SCALE
    for index, (kind, label) in enumerate(labels):
        centre = pad + step * index + step / 2
        label_w = bar_font.getlength(label)
        group = unit + 6 * SCALE + label_w
        start = centre - group / 2
        _fb_icon(draw, kind, start, divider_y + 14 * SCALE, unit, FB_MUTED)
        draw.text(
            (start + unit + 6 * SCALE, divider_y + 30 * SCALE),
            label,
            font=bar_font,
            fill=FB_MUTED,
            anchor="ls",
        )
    return _png(canvas, width, height)


# -- Reddit --------------------------------------------------------------

RD_BG = (26, 26, 27)
RD_TEXT = (215, 218, 220)
RD_MUTED = (129, 131, 132)
RD_PILL = (39, 39, 41)
RD_LINE = (73, 74, 76)


def reddit(
    *,
    name: str,
    message: str,
    time: str = "5h ago",
    avatar: Optional[bytes] = None,
    score: str = "1.2k",
    image: Optional[bytes] = None,
    images: Optional[Dict[str, Image.Image]] = None,
) -> bytes:
    """One Reddit comment: thread rail, u/name, text, vote pill and actions."""
    width = 480 * SCALE
    pad = 16 * SCALE
    rail_x = pad + 8 * SCALE
    gutter = rail_x + 22 * SCALE
    text_limit = width - gutter - pad

    body = Rich(message, 14, markers=MARKERS["chat"], images=images, line_height=19)
    body.wrap(text_limit)

    header_y = pad
    text_y = header_y + 30 * SCALE
    photo = _fit_image(image, text_limit, 300 * SCALE, radius=6)
    photo_h = (photo.height + 10 * SCALE) if photo is not None else 0
    actions_y = text_y + body.height + photo_h + 12 * SCALE
    height = actions_y + 26 * SCALE + pad

    canvas = Image.new("RGB", (width, height), RD_BG)
    draw = ImageDraw.Draw(canvas)

    draw.line((rail_x, 0, rail_x, height), fill=RD_LINE, width=SCALE)

    size = 24 * SCALE
    tile = _avatar(avatar, name, size, (255, 69, 0))
    canvas.paste(tile, (gutter, header_y), tile)

    handle = "u/" + name.strip().lstrip("u/")
    name_font = _font(int(12 * SCALE), bold=True)
    draw.text((gutter + size + 8 * SCALE, header_y + 20 * SCALE), handle, font=name_font, fill=RD_TEXT, anchor="ls")
    x = gutter + size + 8 * SCALE + int(name_font.getlength(handle)) + 6 * SCALE
    draw.text(
        (x, header_y + 20 * SCALE),
        "· {}".format(time),
        font=_font(int(12 * SCALE)),
        fill=RD_MUTED,
        anchor="ls",
    )

    body.draw(canvas, gutter, text_y, RD_TEXT, link_colour=(69, 155, 240))
    if photo is not None:
        canvas.paste(photo, (gutter, text_y + body.height + 10 * SCALE), photo)

    # vote pill: measure the contents, draw the pill, then the contents on top
    score_font = _font(int(12 * SCALE), bold=True)
    arrow_w = 9 * SCALE
    gap = 6 * SCALE
    score_w = int(score_font.getlength(score))
    pill_h = 24 * SCALE
    inner = arrow_w + gap + score_w + gap + arrow_w
    pill_w = inner + 16 * SCALE
    pill_x = gutter - 8 * SCALE
    pill_y = actions_y

    draw.rounded_rectangle(
        (pill_x, pill_y, pill_x + pill_w, pill_y + pill_h),
        radius=pill_h / 2,
        fill=RD_PILL,
        outline=RD_LINE,
        width=SCALE,
    )

    def arrow(cx: float, up: bool, colour) -> None:
        if up:
            draw.polygon(
                [(cx, pill_y + 5 * SCALE), (cx - arrow_w / 2, pill_y + 11 * SCALE), (cx + arrow_w / 2, pill_y + 11 * SCALE)],
                fill=colour,
            )
            draw.line((cx, pill_y + 10 * SCALE, cx, pill_y + 17 * SCALE), fill=colour, width=max(1, SCALE))
        else:
            draw.polygon(
                [(cx, pill_y + 19 * SCALE), (cx - arrow_w / 2, pill_y + 13 * SCALE), (cx + arrow_w / 2, pill_y + 13 * SCALE)],
                fill=colour,
            )
            draw.line((cx, pill_y + 7 * SCALE, cx, pill_y + 14 * SCALE), fill=colour, width=max(1, SCALE))

    cursor = pill_x + 8 * SCALE + arrow_w / 2
    arrow(cursor, True, RD_MUTED)
    cursor += arrow_w / 2 + gap
    draw.text((cursor, pill_y + 16 * SCALE), score, font=score_font, fill=RD_TEXT, anchor="ls")
    cursor += score_w + gap + arrow_w / 2
    arrow(cursor, False, RD_MUTED)

    action_font = _font(int(12 * SCALE), bold=True)
    x = pill_x + pill_w + 14 * SCALE
    for label in ("Reply", "Share", "Save"):
        draw.text((x, pill_y + 16 * SCALE), label, font=action_font, fill=RD_MUTED, anchor="ls")
        x += int(action_font.getlength(label)) + 14 * SCALE
    for index in range(3):
        cx = x + index * 5 * SCALE
        cy = pill_y + 12 * SCALE
        draw.ellipse((cx, cy, cx + 3 * SCALE, cy + 3 * SCALE), fill=RD_MUTED)
    return _png(canvas, width, height)

# -- Instagram / Snapchat ------------------------------------------------


def _messenger(
    *,
    name: str,
    message: str,
    time: str,
    outgoing: bool,
    avatar: Optional[bytes],
    status: str,
    bg,
    header_bg,
    bubble_in,
    bubble_out,
    text_colour,
    muted,
    radius: float,
    accent,
    mark: Optional[str] = None,
    width: int = 420,
    images: Optional[Dict[str, Image.Image]] = None,
    image: Optional[bytes] = None,
) -> bytes:
    """Shared mobile-messenger layout: media bubble above, text bubble, time below."""
    device_w = width * SCALE
    header_h = 56 * SCALE
    pad_x = 12 * SCALE
    pad_y = 9 * SCALE

    body = Rich(message, 14.5, markers=MARKERS["chat"], images=images, line_height=19.5)
    body.wrap(288 * SCALE)

    text_w = body.width + pad_x * 2
    text_h = body.height + pad_y * 2
    photo = _fit_image(image, 288 * SCALE, 300 * SCALE)
    photo_h = 0
    if photo is not None:
        photo = _top_rounded(photo, radius)
        photo_h = photo.height

    gap = 4 * SCALE
    top = header_h + 16 * SCALE
    photo_top = top
    text_top = top + (photo_h + gap if photo is not None else 0)
    time_font = _font(int(10 * SCALE))
    if mark == "opened":
        stamp = "Opened · " + time
    elif mark == "read":
        stamp = "Read " + time
    elif mark == "delivered":
        stamp = "Delivered"
    else:
        stamp = time
    stamp_y = text_top + text_h + 5 * SCALE
    height = stamp_y + 14 * SCALE + 12 * SCALE

    canvas = Image.new("RGB", (device_w, height), bg)
    draw = ImageDraw.Draw(canvas)

    draw.rectangle((0, 0, device_w, header_h), fill=header_bg)
    draw.line((0, header_h, device_w, header_h), fill=(38, 38, 38), width=SCALE)

    # back chevron, then the avatar clear of it
    draw.line(
        (30 * SCALE, header_h / 2 - 7 * SCALE, 21 * SCALE, header_h / 2),
        fill=text_colour,
        width=2 * SCALE,
    )
    draw.line(
        (21 * SCALE, header_h / 2, 30 * SCALE, header_h / 2 + 7 * SCALE),
        fill=text_colour,
        width=2 * SCALE,
    )

    avatar_size = 36 * SCALE
    tile = _avatar(avatar, name, avatar_size, accent)
    canvas.paste(tile, (42 * SCALE, (header_h - avatar_size) // 2), tile)

    name_font = _font(int(15 * SCALE), bold=True)
    label_x = 42 * SCALE + avatar_size + 10 * SCALE
    draw.text((label_x, header_h / 2 - 5 * SCALE), name, font=name_font, fill=text_colour, anchor="ls")
    draw.text(
        (label_x, header_h / 2 + 12 * SCALE),
        status,
        font=_font(int(11.5 * SCALE)),
        fill=muted,
        anchor="ls",
    )

    icon_y = header_h / 2
    video_x = device_w - 66 * SCALE
    draw.rounded_rectangle(
        (video_x, icon_y - 6 * SCALE, video_x + 12 * SCALE, icon_y + 5 * SCALE),
        radius=3 * SCALE,
        outline=text_colour,
        width=2 * SCALE,
    )
    draw.polygon(
        [
            (video_x + 14 * SCALE, icon_y - 5 * SCALE),
            (video_x + 20 * SCALE, icon_y),
            (video_x + 14 * SCALE, icon_y + 5 * SCALE),
        ],
        fill=text_colour,
    )
    phone_x = device_w - 34 * SCALE
    draw.arc(
        (phone_x, icon_y - 9 * SCALE, phone_x + 18 * SCALE, icon_y + 9 * SCALE),
        start=210,
        end=330,
        fill=text_colour,
        width=2 * SCALE,
    )
    draw.line((phone_x + 3 * SCALE, icon_y - 7 * SCALE, phone_x + 3 * SCALE, icon_y - 3 * SCALE), fill=text_colour, width=3 * SCALE)
    draw.line((phone_x + 15 * SCALE, icon_y - 7 * SCALE, phone_x + 15 * SCALE, icon_y - 3 * SCALE), fill=text_colour, width=3 * SCALE)

    def side(bubble_width: int) -> int:
        if outgoing:
            return device_w - 12 * SCALE - bubble_width
        return 12 * SCALE

    if photo is not None:
        _bubble(draw, (side(photo.width), photo_top, side(photo.width) + photo.width, photo_top + photo_h), bubble_out if outgoing else bubble_in, radius)
        canvas.paste(photo, (side(photo.width), photo_top), photo)

    _bubble(
        draw,
        (side(text_w), text_top, side(text_w) + text_w, text_top + text_h),
        bubble_out if outgoing else bubble_in,
        radius,
    )
    body.draw(canvas, side(text_w) + pad_x, text_top + pad_y, (250, 250, 250))

    draw.text(
        (device_w - 12 * SCALE if outgoing else 12 * SCALE, stamp_y),
        stamp,
        font=time_font,
        fill=muted,
        anchor="rs" if outgoing else "ls",
    )
    return _png(canvas, device_w, height)


def instagram(
    *,
    name: str,
    message: str,
    time: str = "21:07",
    outgoing: bool = False,
    avatar: Optional[bytes] = None,
    status: str = "Active now",
    images: Optional[Dict[str, Image.Image]] = None,
    image: Optional[bytes] = None,
) -> bytes:
    """An Instagram direct message, dark mode."""
    return _messenger(
        name=name,
        message=message,
        time=time,
        outgoing=outgoing,
        avatar=avatar,
        status=status,
        bg=(0, 0, 0),
        header_bg=(0, 0, 0),
        bubble_in=(38, 38, 38),
        bubble_out=(55, 151, 240),
        text_colour=(245, 245, 245),
        muted=(142, 142, 142),
        radius=18,
        accent=(221, 42, 123),
        images=images,
        image=image,
    )


def snapchat(
    *,
    name: str,
    message: str,
    time: str = "21:07",
    outgoing: bool = False,
    avatar: Optional[bytes] = None,
    status: str = "Active now",
    images: Optional[Dict[str, Image.Image]] = None,
    image: Optional[bytes] = None,
) -> bytes:
    """A Snapchat chat, dark mode, with the 'Opened' marker on sent messages."""
    return _messenger(
        name=name,
        message=message,
        time=time,
        outgoing=outgoing,
        avatar=avatar,
        status=status,
        bg=(18, 18, 18),
        header_bg=(12, 12, 12),
        bubble_in=(44, 44, 44),
        bubble_out=(15, 173, 255),
        text_colour=(255, 255, 255),
        muted=(160, 160, 160),
        radius=20,
        accent=(255, 252, 0),
        mark="opened" if outgoing else None,
        images=images,
        image=image,
    )

def imessage(
    *,
    name: str,
    message: str,
    time: str = "21:07",
    outgoing: bool = False,
    avatar: Optional[bytes] = None,
    images: Optional[Dict[str, Image.Image]] = None,
    image: Optional[bytes] = None,
) -> bytes:
    """An iMessage thread, dark mode: blue outgoing bubble, Read under it."""
    return _messenger(
        name=name,
        message=message,
        time=time,
        outgoing=outgoing,
        avatar=avatar,
        status="iMessage",
        bg=(0, 0, 0),
        header_bg=(28, 28, 30),
        bubble_in=(38, 38, 42),
        bubble_out=(11, 147, 246),
        text_colour=(255, 255, 255),
        muted=(142, 142, 147),
        radius=18,
        accent=(11, 147, 246),
        mark="read" if outgoing else None,
        images=images,
        image=image,
    )


def _thumb_icon(draw: ImageDraw.ImageDraw, x: float, y: float, size: float, colour, up: bool = True):
    """A small outlined thumb, as YouTube draws it under a comment."""
    flip = 1 if up else -1
    mid = y + size / 2
    draw.rounded_rectangle(
        (x, mid - size * 0.12, x + size * 0.28, mid + size * 0.44),
        radius=2 * SCALE,
        outline=colour,
        width=int(1.6 * SCALE),
    )
    draw.polygon(
        [
            (x + size * 0.32, mid + size * 0.42 * flip),
            (x + size * 0.86, mid + size * 0.42 * flip),
            (x + size * 0.86, mid - size * 0.06 * flip),
            (x + size * 0.60, mid - size * 0.48 * flip),
            (x + size * 0.34, mid - size * 0.12 * flip),
        ],
        outline=colour,
        width=int(1.6 * SCALE),
    )


def youtube(
    *,
    name: str,
    message: str,
    time: str = "1 day ago",
    avatar: Optional[bytes] = None,
    likes: str = "1.2K",
    replies: str = "34",
    video: str = "Neues Video",
    channel: str = "",
    views: str = "412K views · 2 days ago",
    hearted: bool = False,
    pinned: bool = False,
    images: Optional[Dict[str, Image.Image]] = None,
    image: Optional[bytes] = None,
) -> bytes:
    """A YouTube comment under a video, dark mode."""
    channel = channel or name.lstrip("@")
    width = 520 * SCALE
    pad = 16 * SCALE

    body = Rich(message, 14, markers=MARKERS["chat"], images=images, line_height=20)
    body.wrap(width - pad * 2 - 56 * SCALE)

    top = pad
    thumb_w = 96 * SCALE
    thumb_h = 54 * SCALE
    title_y = top + 4 * SCALE

    comment_top = top + thumb_h + 18 * SCALE
    text_y = comment_top + 26 * SCALE
    action_y = text_y + body.height + 10 * SCALE
    height = action_y + 20 * SCALE + pad

    canvas = Image.new("RGB", (width, height), (15, 15, 15))
    draw = ImageDraw.Draw(canvas)

    # the video header above the comment, so the card reads as YouTube
    draw.rounded_rectangle(
        (pad, top, pad + thumb_w, top + thumb_h), radius=6 * SCALE, fill=(40, 40, 40)
    )
    draw.polygon(
        [
            (pad + thumb_w / 2 - 6 * SCALE, top + thumb_h / 2 - 9 * SCALE),
            (pad + thumb_w / 2 + 9 * SCALE, top + thumb_h / 2),
            (pad + thumb_w / 2 - 6 * SCALE, top + thumb_h / 2 + 9 * SCALE),
        ],
        fill=(230, 230, 230),
    )
    draw.text(
        (pad + thumb_w + 16 * SCALE, title_y + 10 * SCALE),
        video,
        font=_font(int(14 * SCALE), bold=True),
        fill=(241, 241, 241),
        anchor="ls",
    )
    draw.text(
        (pad + thumb_w + 16 * SCALE, title_y + 30 * SCALE),
        f"{channel} · {views}",
        font=_font(int(12 * SCALE)),
        fill=(170, 170, 170),
        anchor="ls",
    )
    draw.line((pad, top + thumb_h + 8 * SCALE, width - pad, top + thumb_h + 8 * SCALE), fill=(48, 48, 48), width=SCALE)

    if pinned:
        draw.text(
            (pad, comment_top - 8 * SCALE),
            f"Pinned by {channel}",
            font=_font(int(11 * SCALE)),
            fill=(170, 170, 170),
            anchor="ls",
        )
        comment_top += 14 * SCALE
        text_y += 14 * SCALE
        action_y += 14 * SCALE
        height += 14 * SCALE

    avatar_size = 32 * SCALE
    tile = _avatar(avatar, name, avatar_size, (200, 60, 60))
    canvas.paste(tile, (pad, comment_top - 2 * SCALE), tile)

    name_x = pad + avatar_size + 12 * SCALE
    draw.text(
        (name_x, comment_top + 12 * SCALE),
        "@" + name.lstrip("@"),
        font=_font(int(13 * SCALE), bold=True),
        fill=(241, 241, 241),
        anchor="ls",
    )
    time_w = _font(int(12 * SCALE)).getlength(time)
    draw.text(
        (name_x + _font(int(13 * SCALE), bold=True).getlength("@" + name.lstrip("@")) + 8 * SCALE, comment_top + 12 * SCALE),
        time,
        font=_font(int(12 * SCALE)),
        fill=(170, 170, 170),
        anchor="ls",
    )
    if hearted:
        heart_x = name_x + _font(int(13 * SCALE), bold=True).getlength("@" + name.lstrip("@")) + 8 * SCALE + time_w + 8 * SCALE
        draw.ellipse((heart_x, comment_top + 2 * SCALE, heart_x + 11 * SCALE, comment_top + 13 * SCALE), fill=(230, 33, 23))
        draw.polygon(
            [
                (heart_x + 5.5 * SCALE, comment_top + 11 * SCALE),
                (heart_x + 2 * SCALE, comment_top + 6 * SCALE),
                (heart_x + 5.5 * SCALE, comment_top + 4 * SCALE),
                (heart_x + 9 * SCALE, comment_top + 6 * SCALE),
            ],
            fill=(255, 255, 255),
        )

    body.draw(canvas, pad + avatar_size + 12 * SCALE, text_y, (241, 241, 241))

    small = _font(int(12 * SCALE))
    cursor = name_x
    _thumb_icon(draw, cursor, action_y - 6 * SCALE, 14 * SCALE, (170, 170, 170))
    cursor += 18 * SCALE
    draw.text((cursor, action_y), likes, font=small, fill=(170, 170, 170), anchor="ls")
    cursor += small.getlength(likes) + 22 * SCALE
    _thumb_icon(draw, cursor, action_y - 6 * SCALE, 14 * SCALE, (170, 170, 170), up=False)
    cursor += 18 * SCALE + 24 * SCALE
    draw.text((cursor, action_y), "Reply", font=_font(int(12 * SCALE), bold=True), fill=(241, 241, 241), anchor="ls")
    cursor += _font(int(12 * SCALE), bold=True).getlength("Reply") + 16 * SCALE
    draw.text((cursor, action_y), replies + " replies", font=small, fill=(170, 170, 170), anchor="ls")
    return _png(canvas, width, height)
