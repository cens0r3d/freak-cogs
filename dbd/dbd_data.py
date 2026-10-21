"""Pure data helpers for the DBD cog: text cleanup, placeholder resolution, lookups.

Nothing in here imports ``discord`` or ``redbot`` so the logic can be exercised with a
plain interpreter against the real payloads.
"""

from __future__ import annotations

import html
import re
import unicodedata
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

# ---------------------------------------------------------------------------
# text cleanup
# ---------------------------------------------------------------------------

_BR = re.compile(r"<\s*br\s*/?\s*>", re.IGNORECASE)
_BLOCK_END = re.compile(r"</\s*(p|div|h[1-6]|tr)\s*>", re.IGNORECASE)
_LI = re.compile(r"<\s*li\s*>", re.IGNORECASE)
_LIST_END = re.compile(r"</\s*(ul|ol)\s*>", re.IGNORECASE)
_TAG = re.compile(r"<[^>]+>")
_MULTI_NL = re.compile(r"\n{3,}")
_SPACES = re.compile(r"[ \t\u00a0]{2,}")


def clean(text: Optional[str]) -> str:
    """Turn the wiki-ish HTML in the API payloads into readable plain text."""
    if not text:
        return ""
    out = _BR.sub("\n", text)
    out = _BLOCK_END.sub("\n", out)
    out = _LI.sub("\n• ", out)
    out = _LIST_END.sub("\n", out)
    out = _TAG.sub("", out)
    out = html.unescape(out).replace("\u00a0", " ")
    out = _SPACES.sub(" ", out)
    out = "\n".join(line.strip() for line in out.splitlines())
    out = _MULTI_NL.sub("\n\n", out)
    return out.strip()


def trim(text: str, limit: int) -> str:
    """Cut text on a word boundary and mark the cut."""
    if len(text) <= limit:
        return text
    cut = text[:limit].rsplit(" ", 1)[0].rstrip(" ,.;:")
    return cut + " …"


# ---------------------------------------------------------------------------
# perk placeholders
# ---------------------------------------------------------------------------

_TUNABLE = re.compile(r"\{Tunable\.([A-Za-z0-9_]+)\.([A-Za-z0-9_%]+)\}")
_KEYWORD = re.compile(r"\{Keyword\.([A-Za-z0-9_]+)\}")
_INPUT = re.compile(r"\{Input\.([A-Za-z0-9_]+)\}")

_INPUT_NAMES = {
    "ActivatableButton1": "ability button 1",
    "ActivatableButton2": "ability button 2",
    "ActivatableButton3": "ability button 3",
    "UseItem": "use item button",
    "Attack": "attack button",
    "Interaction": "interaction button",
    "SecondaryAction": "secondary action button",
}


def format_number(value: Any) -> str:
    """Render a tunable value: drop float noise, keep integers bare."""
    if isinstance(value, bool):
        return str(value)
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        if value.is_integer():
            return str(int(value))
        return f"{value:.2f}".rstrip("0").rstrip(".")
    return str(value)


def tier_values(values: Sequence[Any]) -> str:
    """``[50, 75, 100]`` -> ``50/75/100``; a single value stays bare."""
    return "/".join(format_number(v) for v in values)


def resolve_placeholders(text: str, tunables: Optional[Dict[str, Any]]) -> str:
    """Replace ``{Tunable.S02P03.Haste%}`` with the three tier values.

    The percent sign in the descriptions always sits *outside* the placeholder, so the
    substituted text is the bare number(s).
    """
    tunables = tunables or {}

    def _tunable(match: "re.Match[str]") -> str:
        raw = match.group(2)
        value = tunables.get(raw.lower())
        if value is None:
            value = tunables.get(raw.rstrip("%").lower())
        if isinstance(value, (list, tuple)) and value:
            return tier_values(list(value))
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return format_number(value)
        return "?" if raw else ""

    def _keyword(match: "re.Match[str]") -> str:
        return match.group(1).replace("_", " ").title()

    def _input(match: "re.Match[str]") -> str:
        name = match.group(1)
        return _INPUT_NAMES.get(name, name.replace("_", " ").title())

    out = _TUNABLE.sub(_tunable, text)
    out = _KEYWORD.sub(_keyword, out)
    out = _INPUT.sub(_input, out)
    return out


def perk_text(perk: Dict[str, Any]) -> str:
    """Fully rendered, cleaned perk description."""
    return clean(
        resolve_placeholders(perk.get("description") or "", perk.get("tunables"))
    )


# ---------------------------------------------------------------------------
# numbers from killer tunables
# ---------------------------------------------------------------------------

BASE_WALK_SPEED = 400.0  # cm/s == 4.0 m/s == 100 %


def killer_speed(tunables: Optional[Dict[str, Any]]) -> Optional[str]:
    """``460`` cm/s -> ``4.6 m/s (115 %)``."""
    if not tunables:
        return None
    raw = tunables.get("MaxWalkSpeed") or tunables.get("MaxWalkSpeedCrouched")
    try:
        speed = float(raw)
    except (TypeError, ValueError):
        return None
    if speed <= 0:
        return None
    return (
        f"{speed / 100:.2f}".rstrip("0").rstrip(".")
        + f" m/s ({speed / BASE_WALK_SPEED * 100:.0f} %)"
    )


def terror_radius(tunables: Optional[Dict[str, Any]]) -> Optional[str]:
    """``3200`` cm -> ``32 m``."""
    if not tunables:
        return None
    try:
        radius = float(tunables.get("TerrorRadius"))
    except (TypeError, ValueError):
        return None
    if radius < 0:
        return None
    return f"{radius / 100:.0f} m"


# ---------------------------------------------------------------------------
# search / lookup
# ---------------------------------------------------------------------------


def norm(text: Optional[str]) -> str:
    """Normalisation key: accent-free, punctuation-free, lowercase.

    ``&`` is spelled out as ``and`` and possessive ``'s`` is dropped, so "Quick & Quiet"
    matches "quick and quiet" and "Alex's Toolbox" matches "alex toolbox".
    """
    if not text:
        return ""
    lowered = text.lower().replace("\u2019", "'").replace("&", " and ")
    lowered = re.sub(r"'s\b", "", lowered)
    decomposed = unicodedata.normalize("NFKD", lowered)
    ascii_only = decomposed.encode("ascii", "ignore").decode("ascii")
    return re.sub(r"[^a-z0-9]", "", ascii_only.lower())


def build_index(
    entries: Dict[str, Dict[str, Any]], extra_keys: Iterable[str] = ()
) -> Dict[str, str]:
    """Map a normalised name to the payload key.

    ``extra_keys`` names further fields whose values should also resolve (e.g. a perk's
    internal id), so a lookup cannot miss because the user typed the other name.
    """
    index: Dict[str, str] = {}
    for key, entry in entries.items():
        for name in [entry.get("name"), key, *(entry.get(k) for k in extra_keys)]:
            normed = norm(name)
            if normed:
                index.setdefault(normed, key)
    return index


def lookup(index: Dict[str, str], query: str) -> Tuple[Optional[str], List[str]]:
    """Resolve a user query against an index.

    Returns ``(key, candidates)``. Exact normalised match wins, then prefix matches, then
    substring matches; several equally good candidates come back as a suggestion list.
    """
    needle = norm(query)
    if not needle:
        return None, []

    exact = index.get(needle)
    if exact is not None:
        return exact, []

    prefixed = sorted(
        {key for normed, key in index.items() if normed.startswith(needle)}
    )
    if len(prefixed) == 1:
        return prefixed[0], []
    if prefixed:
        return None, prefixed

    contained = sorted({key for normed, key in index.items() if needle in normed})
    if len(contained) == 1:
        return contained[0], []
    return None, contained


def suggest(
    entries: Dict[str, Dict[str, Any]], query: str, limit: int = 5
) -> List[str]:
    """Cheap typo tolerance: closest display names by prefix overlap."""
    needle = norm(query)
    scored = []
    for entry in entries.values():
        name = entry.get("name") or ""
        key = norm(name)
        if not key:
            continue
        common = len(set(key) & set(needle))
        if common >= max(2, len(needle) // 2):
            scored.append((common / max(len(key), 1), name))
    scored.sort(reverse=True)
    return [name for _, name in scored[:limit]]


def chunk(items: Sequence[Any], size: int) -> List[List[Any]]:
    return [list(items[i : i + size]) for i in range(0, len(items), size)]


# ---------------------------------------------------------------------------
# cosmetic mappings
# ---------------------------------------------------------------------------

DIFFICULTY = {
    "none": ("⚪", "None"),
    "easy": ("🟢", "Easy"),
    "intermediate": ("🟡", "Intermediate"),
    "hard": ("🟠", "Hard"),
    "veryhard": ("🔴", "Very Hard"),
}

RARITY = {
    "none": ("⚪", "None", 0x95A5A6),
    "common": ("🟤", "Common", 0xA1887F),
    "uncommon": ("🟡", "Uncommon", 0xD4AC0D),
    "rare": ("🟢", "Rare", 0x27AE60),
    "veryrare": ("🟣", "Very Rare", 0x8E44AD),
    "visceral": ("🔴", "Visceral", 0xC0392B),
    "ultrarare": ("🌸", "Ultra Rare", 0xE84393),
}

ROLE = {
    "killer": ("🔪", "Killer", 0x9B1B30),
    "survivor": ("🏃", "Survivor", 0x2E86AB),
}

ITEM_TYPES = {
    "toolbox": "Toolbox",
    "medkit": "Med-Kit",
    "flashlight": "Flashlight",
    "key": "Key",
    "map": "Map",
    "firecracker": "Firecracker",
    "fogvial": "Fog Vial",
    "": "—",
}


def role_parts(role: Optional[str]) -> Tuple[str, str, int]:
    emoji, label, colour = ROLE.get((role or "").lower(), ("🎭", "Unknown", 0x607D8B))
    return emoji, label, colour


def difficulty_parts(value: Optional[str]) -> Tuple[str, str]:
    return DIFFICULTY.get((value or "none").lower(), ("⚪", value or "Unknown"))


def rarity_parts(value: Optional[str]) -> Tuple[str, str, int]:
    return RARITY.get(
        (value or "none").lower(), ("⚪", (value or "Unknown").title(), 0x95A5A6)
    )


def title_case_tag(tag: str) -> str:
    return tag.replace("_", " ").replace("-", " ").title()
