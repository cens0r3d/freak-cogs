"""Parsing and formatting helpers for goabase.net party data.

Deliberately free of any ``discord``/``redbot`` import so the whole formatting
layer can be exercised against saved payloads with a plain interpreter.

Source: the site's own schema.org export, which is richer than its RSS feed::

    GET https://www.goabase.net/api/party/jsonld/?country=CH   -> ItemList (ids)
    GET https://www.goabase.net/api/party/jsonld/<id>          -> DanceEvent

Party data on goabase.net is published under CC BY-SA 2.0, hence the credit in
the embed footer.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Sequence, Tuple

API_ROOT = "https://www.goabase.net/api/party/jsonld"
SITE = "https://www.goabase.net"
PARTY_LIST_URL = SITE + "/party"
LOGO_URL = "https://www.goabase.net/images/goabase_logo_512x512.png"

COLOR = 0x8B5CF6
COLOR_RESCHEDULED = 0xE0A030
COLOR_CANCELLED = 0xE5484D
COLOR_ONLINE = 0x3BA3F0

DESCRIPTION_LIMIT = 320
LINEUP_LIMIT = 6

WEEKDAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
MONTHS = (
    "Jan",
    "Feb",
    "Mar",
    "Apr",
    "May",
    "Jun",
    "Jul",
    "Aug",
    "Sep",
    "Oct",
    "Nov",
    "Dec",
)

#: The countries goabase currently carries, straight from
#: ``/api/party/jsonld/country/list``. Cached here so validating a user's input
#: needs no network round-trip.
COUNTRIES: Dict[str, str] = {
    "AR": "Argentina",
    "AT": "Austria",
    "BE": "Belgium",
    "BR": "Brazil",
    "KH": "Cambodia",
    "CL": "Chile",
    "CZ": "Czechia",
    "DK": "Denmark",
    "SV": "El Salvador",
    "FI": "Finland",
    "DE": "Germany",
    "IN": "India",
    "IE": "Ireland",
    "IT": "Italy",
    "JP": "Japan",
    "MX": "Mexico",
    "NP": "Nepal",
    "NL": "Netherlands",
    "NZ": "New Zealand",
    "PS": "Palestine",
    "PA": "Panama",
    "PE": "Peru",
    "PL": "Poland",
    "SK": "Slovakia",
    "ZA": "South Africa",
    "ES": "Spain",
    "LK": "Sri Lanka",
    "CH": "Switzerland",
    "TH": "Thailand",
    "TR": "Türkiye",
    "GB": "United Kingdom",
}

_DECORATION = " \t*•·-–—>|:►▶▪●🎧🎶🎵☼✦★☆❤️🔥🌀🎉🎃👻"
_TIME_PREFIX = re.compile(r"^\d{1,2}[:.]\d{2}\s*[-–]?\s*")
#: Leading labels such as ``lineup:`` / ``line up -`` that are not acts.
_LABEL_PREFIX = re.compile(
    r"^(?:line[\s-]?up|acts?|live\s*acts?|djs?|artists?)\s*[:\-–]\s*", re.I
)
#: Hosts inside a performer blob (people paste their promo links there).
_URL_LIKE = re.compile(
    r"(?:https?://|www\.|\b[\w-]+\.(?:com|ch|de|at|net|org|io|be|fr|it|co|uk|tv|fm|info|link|me|nu|se|dk|pl|cz|es|nl)\b)",
    re.I,
)
#: Lines that label a block instead of naming an act.
_STOPWORDS = {
    "lineup",
    "line up",
    "line-up",
    "acts",
    "live acts",
    "live acts djs",
    "live acts & djs",
    "live acts and djs",
    "main stage",
    "second stage",
    "outdoor",
    "outdoor warm up",
    "warm up",
    "doors",
    "info",
    "infos",
    "tickets",
    "presale",
    "special guest",
    "special guests",
    "support",
    "guests",
    "abendkasse",
    "eintritt",
    "programm",
    "program",
    "location",
}


#: Function words that mark a description fragment rather than an act name.
_FILLER_WORDS = {
    "mit",
    "und",
    "der",
    "die",
    "das",
    "für",
    "auf",
    "im",
    "am",
    "the",
    "and",
    "with",
    "for",
    "from",
    "our",
    "your",
    "ein",
    "eine",
    "get",
    "ready",
}


def _is_flag_codepoint(codepoint: int) -> bool:
    """Characters that build an emoji flag rather than name an act.

    A character class written as ``[\U0001f1e6-\U0001f1ff...]`` reads as an
    overlapping range to static analysers (CodeQL: ``py/overly-large-range``),
    and the ranges are the one thing in this module that cannot be read at a
    glance. Comparing code points says the same thing without the ambiguity.
    """
    return (
        0x1F1E6 <= codepoint <= 0x1F1FF  # regional indicator letters (country flags)
        or codepoint == 0x1F3F4  # black flag, basis of subdivision flags
        or 0xE0061 <= codepoint <= 0xE007F  # tag characters of subdivision flags
        or codepoint == 0xFE0F  # variation selector-16
    )


def _strip_flags(text: str) -> str:
    return "".join(char for char in text if not _is_flag_codepoint(ord(char)))


def _alnum_ratio(text: str) -> float:
    if not text:
        return 0.0
    alnum = sum(1 for char in text if char.isalnum())
    return alnum / len(text)


def _candidate_name(chunk: str) -> Optional[str]:
    """Return a cleaned act name, or ``None`` when the chunk is not one."""
    name = chunk.strip()
    if not name:
        return None
    name = _LABEL_PREFIX.sub("", name)
    name = _TIME_PREFIX.sub("", name)
    name = _strip_flags(name)
    name = name.strip(_DECORATION)
    name = re.sub(r"\s{2,}", " ", name)
    if not 2 <= len(name) <= 48:
        return None
    if _URL_LIKE.search(name):
        return None
    if _alnum_ratio(name) < 0.55:
        return None
    words = name.split()
    if len(words) > 5:
        return None
    # A sentence fragment ("Groovig verspielt mit femininem Touch") is not an act.
    if len(words) >= 4 and any(
        word.lower().strip(",.;:") in _FILLER_WORDS for word in words
    ):
        return None
    name = name.rstrip(" ,;")
    if name.rstrip(" .").lower().strip(_DECORATION) in _STOPWORDS:
        return None
    if name.endswith((":)", ":(", ":")):
        return None
    # A name cut in half by the site ("(Schlaflos") reads as a bug in the embed.
    if name.count("(") != name.count(")"):
        return None
    if not any(char.isalpha() for char in name):
        return None
    return name


def lineup(value: Any, limit: int = LINEUP_LIMIT) -> str:
    """First few acts from the performers blob, without links, labels or emoji."""
    if not value:
        return ""
    raw = str(value).replace("\r\n", "\n").replace("\r", "\n")
    if "\n" not in raw.strip():
        # Some organisers paste a whole one-line schedule ("21:00 X 22:30 Y").
        chunks = re.split(r"(?=\b\d{1,2}[:.]\d{2}\s)", raw)
    else:
        chunks = raw.split("\n")

    names: List[str] = []
    for chunk in chunks:
        for part in re.split(r"\s+/\s+", chunk):
            name = _candidate_name(part)
            if name is None:
                continue
            if name.lower() in {existing.lower() for existing in names}:
                continue
            names.append(name)
            if len(names) >= limit:
                break
        if len(names) >= limit:
            break
    return " · ".join(names)


def parse_datetime(value: Any) -> Optional[datetime]:
    """Parse one of goabase's ISO-8601 timestamps, tolerating a ``Z`` suffix."""
    if not value:
        return None
    text = str(value).strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def party_id_from_url(url: Any) -> Optional[int]:
    """``.../api/party/jsonld/118581`` -> ``118581``."""
    if not url:
        return None
    tail = str(url).rstrip("/").rsplit("/", 1)[-1]
    try:
        return int(tail)
    except (TypeError, ValueError):
        return None


def _trim(text: str, limit: int) -> str:
    text = text.strip()
    if len(text) <= limit:
        return text
    cut = text[:limit].rsplit(" ", 1)[0].rstrip(" ,;:-–—")
    return cut + "…"


def clean_text(value: Any, limit: int = DESCRIPTION_LIMIT) -> str:
    """Collapse a user-written blurb (CRLF, runs of blank lines, markdown noise)."""
    if not value:
        return ""
    text = str(value).replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    text = re.sub(r"\*\*|\*|__", "", text)
    text = "\n".join(line.strip() for line in text.split("\n")).strip()
    return _trim(text, limit)


def format_when(start: Optional[datetime], end: Optional[datetime], tz) -> str:
    """``Fri 30 Oct 2026 · 21:00 – 06:00`` in the guild's own timezone."""
    if start is None:
        return "date unknown"
    local = start.astimezone(tz)
    stamp = f"{WEEKDAYS[local.weekday()]} {local.day} {MONTHS[local.month - 1]} {local.year}"
    text = f"{stamp} · {local:%H:%M}"
    if end is None:
        return text
    end_local = end.astimezone(tz)
    if end_local.date() == local.date():
        return f"{text} – {end_local:%H:%M}"
    if (end_local.date() - local.date()).days == 1:
        return f"{text} – {end_local:%H:%M} (+1)"
    return f"{text} – {WEEKDAYS[end_local.weekday()]} {end_local.day} {MONTHS[end_local.month - 1]} {end_local:%H:%M}"


def entry_info(offers: Any) -> Tuple[str, Optional[str]]:
    """Return (label, external ticket url) for a schema.org ``offers`` value.

    goabase encodes "no presale" as an offer pointing back at its own
    ``/party/entryfree/<id>`` page, which is a box-office note rather than a
    ticket shop - that must not become a button.
    """
    offer: Optional[Dict[str, Any]] = None
    if isinstance(offers, dict) and offers:
        offer = offers
    elif isinstance(offers, Sequence) and not isinstance(offers, (str, bytes)):
        for candidate in offers:
            if isinstance(candidate, dict) and candidate:
                offer = candidate
                break
    if not offer:
        return "", None

    price = offer.get("price")
    currency = str(offer.get("priceCurrency") or "").strip()
    url = offer.get("url") or None
    external = None
    if url and "/party/entryfree/" not in str(url):
        external = str(url)

    amount: Optional[str] = None
    if isinstance(price, (int, float)) or (isinstance(price, str) and price.strip()):
        try:
            value = float(price)
        except (TypeError, ValueError):
            value = None
        if value is not None:
            amount = (
                f"{value:.0f}" if abs(value - round(value)) < 1e-9 else f"{value:.2f}"
            )

    if amount and float(amount) == 0:
        return "Free entry", external
    if amount:
        money = f"{amount} {currency}".strip()
        return (
            ("Tickets from " + money) if external else ("Box office " + money)
        ), external
    if external:
        return "Tickets available", external
    return "", None


def place_info(location: Any) -> Tuple[str, Optional[str]]:
    """Return (place label, map url)."""
    if not isinstance(location, dict) or not location:
        return "", None
    address = location.get("address") or {}
    venue = clean_text(location.get("name"), 90)
    town = clean_text(address.get("addressLocality"), 60)
    country = clean_text(address.get("addressCountry"), 60)

    head = venue or town
    tail_parts = []
    if town and town != head:
        tail_parts.append(town)
    if country:
        tail_parts.append(country)
    label = head
    if tail_parts:
        label = f"{head} · {', '.join(tail_parts)}" if head else ", ".join(tail_parts)
    map_url = str(location.get("hasMap") or "").strip() or None
    return label, map_url


def flyer_url(image: Any) -> Optional[str]:
    """Full flyer, preferring the JPEG variant Discord renders everywhere."""
    if not isinstance(image, dict):
        return None
    candidates: List[str] = []
    for key in ("url", "thumbnailUrl"):
        value = image.get(key)
        if value:
            candidates.append(str(value))
    for url in candidates:
        if url.lower().endswith((".webp", ".avif", ".png")):
            jpg = url.rsplit(".", 1)[0] + ".jpg"
            return jpg
        if url.lower().endswith((".jpg", ".jpeg")):
            return url
    return candidates[0] if candidates else None


def status_info(event_status: Any, attendance_mode: Any) -> Tuple[str, int]:
    """Return (badge line, embed colour) for the event's state."""
    status = str(event_status or "").rsplit("/", 1)[-1]
    mode = str(attendance_mode or "").rsplit("/", 1)[-1]
    if status == "EventCancelled":
        return "❌ **Cancelled**", COLOR_CANCELLED
    if status == "EventRescheduled":
        return "🔄 **Rescheduled**", COLOR_RESCHEDULED
    if mode == "OnlineEventAttendanceMode":
        return "💻 **Online event**", COLOR_ONLINE
    return "", COLOR


def event_from_payload(payload: Any) -> Optional[Dict[str, Any]]:
    """Normalise one ``DanceEvent`` payload, or ``None`` when it is unusable."""
    if not isinstance(payload, dict) or not payload.get("name"):
        return None
    start = parse_datetime(payload.get("startDate"))
    if start is None:
        return None
    party_id = party_id_from_url(payload.get("@id")) or party_id_from_url(
        payload.get("url")
    )
    return {
        "id": party_id,
        "name": clean_text(payload.get("name"), 200),
        "url": str(
            payload.get("url") or (f"{SITE}/party/{party_id}" if party_id else SITE)
        ),
        "start": start,
        "end": parse_datetime(payload.get("endDate")),
        "description": clean_text(payload.get("description")),
        "performers": lineup(payload.get("performers")),
        "image": flyer_url(payload.get("image")),
        "location": place_info(payload.get("location")),
        "entry": entry_info(payload.get("offers")),
        "organizer": clean_text((payload.get("organizer") or {}).get("name"), 60),
        "badge": status_info(
            payload.get("eventStatus"), payload.get("eventAttendanceMode")
        ),
    }


def build_embed_spec(event: Dict[str, Any], tz) -> Dict[str, Any]:
    """Turn a normalised event into a Discord-agnostic embed description."""
    badge, colour = event["badge"]
    place, map_url = event["location"]
    entry, ticket_url = event["entry"]

    description_parts = []
    if badge:
        description_parts.append(badge)
    if event["description"]:
        description_parts.append(event["description"])
    if not description_parts:
        description_parts.append("_No description on goabase._")

    fields: List[Dict[str, Any]] = []
    when = format_when(event["start"], event["end"], tz)
    relative = f"<t:{int(event['start'].timestamp())}:R>"
    fields.append({"name": "📅 When", "value": f"{when}\n{relative}", "inline": False})
    if place:
        fields.append({"name": "📍 Where", "value": place, "inline": True})
    if entry:
        fields.append({"name": "🎟️ Entry", "value": entry, "inline": True})
    if map_url and not entry:
        fields.append(
            {"name": "🗺️ Map", "value": f"[OpenStreetMap]({map_url})", "inline": True}
        )
    if event["performers"]:
        fields.append(
            {"name": "🎧 Line-up", "value": event["performers"], "inline": False}
        )

    buttons: List[Dict[str, str]] = []
    if ticket_url:
        buttons.append({"label": "Tickets", "emoji": "🎟️", "url": ticket_url})
    if map_url:
        buttons.append({"label": "Map", "emoji": "📍", "url": map_url})
    buttons.append({"label": "Party page", "emoji": "🔗", "url": event["url"]})
    buttons.append({"label": "All parties", "emoji": "🌐", "url": PARTY_LIST_URL})

    footer = "goabase.net · CC BY-SA 2.0"
    if event["organizer"]:
        footer = f"goabase.net · {event['organizer']} · CC BY-SA 2.0"

    return {
        "title": event["name"],
        "url": event["url"],
        "description": "\n\n".join(description_parts),
        "fields": fields,
        "image": event["image"],
        "colour": colour,
        "footer": footer,
        "author": {"name": "goabase ॐ parties", "icon_url": LOGO_URL},
        "buttons": buttons,
    }
