"""Pure helpers for the psychonaut cog.

No ``discord`` or ``redbot`` imports live here, so the markup cleaning, the
SubstanceBox parser and the name matching can be exercised with a plain
interpreter against a saved fixture.

The source is PsychonautWiki's MediaWiki API:

* ``action=parse&prop=wikitext`` for the article and its ``Template:SubstanceBox/<name>``
* ``action=ask`` for the Semantic MediaWiki index of every substance
* ``list=search`` for free-text fallback when the index has no match

Dosage and duration values come out of the SubstanceBox parameters, where every
number carries a semantic annotation (``[[Oral min common dose::75]]``).  Cleaning
the markup therefore leaves a plain human range such as ``75 - 150 µg``.
"""

from __future__ import annotations

import re
import unicodedata
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

API = "https://psychonautwiki.org/w/api.php"
SITE = "https://psychonautwiki.org/wiki/"
THUMB = "https://psychonautwiki.org/w/thumb.php?f={file}&width={width}"

ROA_ORDER = [
    "Oral",
    "Sublingual",
    "Buccal",
    "Insufflated",
    "Smoked",
    "Vaporized",
    "Inhaled",
    "Rectal",
    "Intravenous",
    "Intramuscular",
    "Subcutaneous",
    "Transdermal",
]

ROA_LABELS = {
    "Oral": "Oral",
    "Sublingual": "Sublingual",
    "Buccal": "Buccal",
    "Insufflated": "Insufflated",
    "Smoked": "Smoked",
    "Vaporized": "Vaporized",
    "Inhaled": "Inhaled",
    "Rectal": "Rectal",
    "Intravenous": "Intravenous",
    "Intramuscular": "Intramuscular",
    "Subcutaneous": "Subcutaneous",
    "Transdermal": "Transdermal",
}

DOSE_KEYS = ["Threshold", "Light", "Common", "Strong", "Heavy"]
TIME_KEYS = ["Duration", "Onset", "Comeup", "Peak", "Offset", "Aftereffects"]

DOSE_LABELS = {
    "Threshold": "Threshold",
    "Light": "Light",
    "Common": "Common",
    "Strong": "Strong",
    "Heavy": "Heavy",
}

TIME_LABELS = {
    "Duration": "Total",
    "Onset": "Onset",
    "Comeup": "Come up",
    "Peak": "Peak",
    "Offset": "Offset",
    "Aftereffects": "After effects",
}

_REF_RE = re.compile(r"<ref[^>]*/\s*>|<ref[^>]*>.*?</ref\s*>", re.S | re.I)
_COMMENT_RE = re.compile(r"<!--.*?-->", re.S)
_FILE_LINK_RE = re.compile(r"\[\[(?:File|Image):[^\[\]]*(?:\]\]|\|)", re.I)
_ANNOT_RE = re.compile(r"\[\[\s*[^\[\]|:]+::([^\[\]|]*)(?:\|([^\[\]]*))?\]\]")
_LINK_RE = re.compile(r"\[\[([^\[\]|]*)(?:\|([^\[\]]*))?\]\]")
_EXT_RE = re.compile(r"\[(?:https?:)?//[^\s\]]+\s*([^\]]*)\]")
_TEMPLATE_RE = re.compile(r"\{\{[^{}]*\}\}")
_KEY_RE = re.compile(r"\s*([A-Za-z0-9_]+)\s*=")
_WS_RE = re.compile(r"[ \t\u00a0]+")
_BLANKS_RE = re.compile(r"\n{3,}")
_HTML_TAG_RE = re.compile(r"</?[a-zA-Z][^<>]{0,120}>")
_PERCENT_RE = re.compile(r"%%+")
_COMMON_ANNOT_RE = re.compile(r"\[\[\s*common name::\s*([^\[\]|]*)")
_ENTITY_RE = re.compile(r"&(nbsp|amp|lt|gt|quot|#39|#160);", re.I)
_ENTITIES = {
    "nbsp": " ",
    "amp": "&",
    "lt": "<",
    "gt": ">",
    "quot": '"',
    "#39": "'",
    "#160": " ",
}

# --------------------------------------------------------------------- markup


def clean(text: Optional[str]) -> str:
    """Turn wiki markup into readable plain text."""
    if not text:
        return ""
    value = _COMMENT_RE.sub("", str(text))
    value = _REF_RE.sub("", value)
    value = _HTML_TAG_RE.sub("", value)
    value = _FILE_LINK_RE.sub("", value)
    for _ in range(4):
        new = _TEMPLATE_RE.sub("", value)
        if new == value:
            break
        value = new
    value = _ANNOT_RE.sub(lambda m: (m.group(2) or m.group(1) or "").strip(), value)
    value = _LINK_RE.sub(lambda m: (m.group(2) or m.group(1) or "").strip(), value)
    value = _EXT_RE.sub(lambda m: (m.group(1) or "").strip(), value)
    value = value.replace("'''", "").replace("''", "")
    value = _ENTITY_RE.sub(
        lambda m: _ENTITIES.get(m.group(1).lower(), m.group(0)), value
    )
    value = _PERCENT_RE.sub("%", value)
    value = _WS_RE.sub(" ", value)
    value = "\n".join(line.strip() for line in value.splitlines())
    value = _BLANKS_RE.sub("\n\n", value)
    return value.strip(" \n-–|")


def norm(text: Optional[str]) -> str:
    """Fold a name for comparisons: lower case, no accents, no punctuation."""
    if not text:
        return ""
    value = unicodedata.normalize("NFKD", str(text))
    value = "".join(ch for ch in value if not unicodedata.combining(ch))
    value = value.casefold().replace("&", " and ")
    value = re.sub(r"'s\b", "", value)
    value = re.sub(r"[^a-z0-9]+", " ", value)
    return " ".join(value.split())


def squash(text: Optional[str]) -> str:
    """Normalised name without spaces, for a compact exact-match map."""
    return norm(text).replace(" ", "")


# ------------------------------------------------------------------- templates


def template_params(wikitext: Optional[str]) -> Dict[str, str]:
    """Return the parameters of the outermost ``{{...}}`` in ``wikitext``.

    Pipes inside nested templates and wiki links do not split a value, which is
    what makes this usable on SubstanceBox bodies with ``<ref>{{cite journal|…}}</ref>``
    inside a parameter.
    """
    if not wikitext:
        return {}
    text = str(wikitext)
    start = text.find("{{")
    if start < 0:
        return {}
    depth = 0
    end = -1
    index = start
    while index < len(text) - 1:
        pair = text[index : index + 2]
        if pair == "{{":
            depth += 1
            index += 2
            continue
        if pair == "}}":
            depth -= 1
            index += 2
            if depth == 0:
                end = index - 2
                break
            continue
        index += 1
    if end < 0:
        end = len(text)
    body = text[start + 2 : end]

    params: Dict[str, str] = {}
    key: Optional[str] = None
    buffer: List[str] = []
    curly = 0
    square = 0
    index = 0
    while index < len(body):
        pair = body[index : index + 2]
        if pair == "{{":
            curly += 1
            buffer.append(pair)
            index += 2
            continue
        if pair == "}}":
            if curly:
                curly -= 1
            buffer.append(pair)
            index += 2
            continue
        if pair == "[[":
            square += 1
            buffer.append(pair)
            index += 2
            continue
        if pair == "]]":
            if square:
                square -= 1
            buffer.append(pair)
            index += 2
            continue
        if body[index] == "|" and not curly and not square:
            if key is not None:
                params[key] = "".join(buffer).strip()
            buffer = []
            key = None
            match = _KEY_RE.match(body, index + 1)
            if match:
                key = match.group(1)
                index = match.end()
                continue
            index += 1
            continue
        buffer.append(body[index])
        index += 1
    if key is not None:
        params[key] = "".join(buffer).strip()
    return params


def file_name(value: Optional[str]) -> str:
    """``File:LSD.svg`` -> ``LSD.svg``."""
    text = clean(value)
    if not text:
        return ""
    text = text.split("|")[0].strip()
    if text.lower().startswith(("file:", "image:")):
        text = text.split(":", 1)[1].strip()
    return text


def thumb_url(name: str, width: int = 260) -> str:
    """A PNG thumbnail of a wiki file.

    MediaWiki renders SVG originals through ``thumb.php`` and serves the result as
    ``image/png``, which is why Discord can show it at all.
    """
    if not name:
        return ""
    quoted = name.replace(" ", "_")
    return THUMB.format(file=quoted, width=width)


# ---------------------------------------------------------------- SubstanceBox


def parse_box(wikitext: Optional[str]) -> Dict[str, Any]:
    """Parse ``Template:SubstanceBox/<name>`` wikitext into a lookup-friendly dict."""
    params = template_params(wikitext)
    if not params:
        return {}

    common: List[str] = []
    raw_common = params.get("NameCommon") or ""
    annotated = [
        clean(match.group(1)) for match in _COMMON_ANNOT_RE.finditer(raw_common)
    ]
    annotated = [name for name in annotated if name]
    if annotated:
        for value in annotated:
            if value not in common:
                common.append(value)
    else:
        for part in re.split(r",(?![^\[]*\])", raw_common):
            value = clean(part.split(".")[0])
            if value and len(value) <= 40 and value not in common:
                common.append(value)

    roas: Dict[str, Dict[str, str]] = {}
    for key, raw in params.items():
        if not key.endswith("ROA") or key == "ModuleROA":
            continue
        roa = key[: -len("ROA")]
        if not roa or not (raw or "").strip().lower() in ("true", "yes", "1"):
            continue
        fields: Dict[str, str] = {}
        for name in DOSE_KEYS + TIME_KEYS:
            value = clean(params.get(f"{roa}ROA_{name}"))
            if value:
                fields[name] = value
        note = clean(params.get(f"{roa}ROA_Caption"))
        if note:
            fields["_note"] = note
        bioavailability = clean(params.get(f"{roa}ROA_Bioavailability"))
        if bioavailability:
            fields["_bioavailability"] = bioavailability
        if any(name in fields for name in DOSE_KEYS + TIME_KEYS):
            roas[roa] = fields

    ordered: Dict[str, Dict[str, str]] = {}
    for roa in ROA_ORDER:
        if roa in roas:
            ordered[roa] = roas.pop(roa)
    for roa in sorted(roas):
        ordered[roa] = roas[roa]

    return {
        "title": clean(params.get("MaterialTable_Title")),
        "common": common,
        "substitution": clean(params.get("NameSubstitution")),
        "systematic": clean(params.get("NameSystematic")),
        "effect_class": clean(params.get("EffectClass")),
        "chemical_class": clean(params.get("ChemicalClass")),
        "structure": file_name(params.get("SkeletalImageFile")),
        "photo": file_name(params.get("PhotoImageFile")),
        "caption": clean(params.get("MolecularStructureCaption")),
        "roas": ordered,
    }


def box_template_name(article_wikitext: Optional[str]) -> str:
    """Find the ``SubstanceBox/<x>`` template an article transcludes."""
    if not article_wikitext:
        return ""
    match = re.search(r"\{\{\s*(SubstanceBox[^}|\n}]*)\s*[|}]", str(article_wikitext))
    return match.group(1).strip() if match else ""


def section(article_wikitext: Optional[str], heading: str) -> str:
    """Return the text under ``== heading ==`` (any ``=` depth), markup cleaned."""
    if not article_wikitext:
        return ""
    pattern = re.compile(
        r"^==+\s*" + re.escape(heading) + r"\s*==+\s*$(.*?)(?=^==+[^=]|\Z)",
        re.S | re.M | re.I,
    )
    match = pattern.search(str(article_wikitext))
    if not match:
        return ""
    return clean(match.group(1))


def intro(article_wikitext: Optional[str], limit: int = 900) -> str:
    """First prose paragraph of an article, without the summary/box templates."""
    text = str(article_wikitext or "")
    text = _COMMENT_RE.sub("", text)
    text = re.sub(r"\{\{[^{}]*\}\}", "", text)
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith(
            ("[[File:", "[[Image:", "{|", "|", "==", "{{", "<")
        ):
            continue
        if line.startswith(("'''", "''", "[[")):
            return trim(clean(line), limit)
    return ""


def trim(text: str, limit: int) -> str:
    """Cut to ``limit`` characters on a word boundary."""
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    cut = text[:limit]
    space = cut.rfind(" ")
    if space > limit * 0.6:
        cut = cut[:space]
    return cut.rstrip(" ,;:-") + "…"


# --------------------------------------------------------------- name matching


def build_index(rows: Iterable[Dict[str, Any]]) -> Dict[str, str]:
    """Map every normalised title and alias to its canonical page title.

    Rows may name the canonical entry as ``title`` (the wiki shape) or ``name``
    (the GraphQL shape).
    """
    index: Dict[str, str] = {}
    for row in rows:
        title = str(row.get("title") or row.get("name") or "").strip()
        if not title:
            continue
        for name in [title] + list(row.get("common") or []):
            key = norm(name)
            if key:
                index.setdefault(key, title)
    return index


def build_token_index(index: Dict[str, str]) -> Dict[str, List[str]]:
    """Word -> the page titles that contain it, for whole-word matches."""
    tokens: Dict[str, List[str]] = {}
    for key, title in index.items():
        for word in key.split():
            bucket = tokens.setdefault(word, [])
            if title not in bucket:
                bucket.append(title)
    return tokens


def match(
    query: str,
    index: Dict[str, str],
    tokens: Optional[Dict[str, List[str]]] = None,
) -> Tuple[Optional[str], List[str]]:
    """Resolve ``query`` against the index in ordered tiers.

    Returns ``(canonical_title, candidates)``.  ``candidates`` is only filled when a
    tier matched several entities, and the caller should show that list instead of
    guessing.  Tiers: exact, prefix (>= 3 chars), whole word, substring (>= 4 chars).
    """
    key = norm(query)
    if not key:
        return None, []
    if key in index:
        return index[key], []

    compact = key.replace(" ", "")
    if compact != key:
        for name, title in index.items():
            if name.replace(" ", "") == compact:
                return title, []

    if len(key) >= 3:
        prefixed = sorted(
            {title for name, title in index.items() if name.startswith(key)}
        )
        if len(prefixed) == 1:
            return prefixed[0], []
        if prefixed:
            return None, prefixed

    if tokens:
        whole = sorted(
            {title for word in key.split() for title in tokens.get(word, [])}
        )
        if len(whole) == 1:
            return whole[0], []
        if whole:
            return None, whole

    if len(key) >= 4:
        inside = sorted({title for name, title in index.items() if key in name})
        if len(inside) == 1:
            return inside[0], []
        if inside:
            return None, inside

    return None, []


def suggest(query: str, index: Dict[str, str], limit: int = 5) -> List[str]:
    """Cheap 'did you mean' list: closest names by shared prefix length."""
    key = norm(query)
    if not key:
        return []
    scored: List[Tuple[int, str]] = []
    for name in index:
        if not name:
            continue
        shared = 0
        for position in range(min(len(key), len(name))):
            if key[position] != name[position]:
                break
            shared += 1
        if shared >= 2:
            scored.append((shared, name))
    scored.sort(key=lambda item: (-item[0], len(item[1]), item[1]))
    output: List[str] = []
    for _, name in scored:
        title = index[name]
        if title not in output:
            output.append(title)
        if len(output) >= limit:
            break
    return output


# ------------------------------------------------------------------ formatting


def dose_block(
    fields: Dict[str, str], keys: Sequence[str], labels: Dict[str, str]
) -> str:
    """Render one ROA's dose or timing rows as embed-field text."""
    lines: List[str] = []
    for key in keys:
        value = fields.get(key)
        if not value:
            continue
        lines.append(f"**{labels.get(key, key)}:** {value}")
    return "\n".join(lines)


# --------------------------------------------------------------- effects
#
# Effect lists live in ``{{effects/base|{{effects/physical|*[[Effect::…]]}}…}}``.
# Every row is a bullet with a semantic annotation, so the annotation name is the
# effect and the trailing prose is commentary that would blow the embed budget.

EFFECT_ORDER = [
    "physical",
    "visual",
    "cognitive",
    "auditory",
    "multisensory",
    "transpersonal",
    "aftereffects",
]

EFFECT_LABELS = {
    "physical": "Physical",
    "visual": "Visual",
    "cognitive": "Cognitive",
    "auditory": "Auditory",
    "multisensory": "Multisensory",
    "transpersonal": "Transpersonal",
    "aftereffects": "After effects",
}

_EFFECT_BLOCK_RE = re.compile(r"\{\{effects/([a-z_]+)\s*\|")
_EFFECT_NAME_RE = re.compile(r"\[\[Effect::([^\]|]+)")


def parse_effects(article_wikitext: Optional[str]) -> Dict[str, List[str]]:
    """Map effect category -> effect names, in the order the article lists them."""
    text = str(article_wikitext or "")
    start = text.find("{{effects/base")
    if start < 0:
        return {}
    body = text[start:]
    blocks = [
        (match.start(), match.group(1)) for match in _EFFECT_BLOCK_RE.finditer(body)
    ]
    found: Dict[str, List[str]] = {}
    for position, category in enumerate(blocks):
        name = category[1]
        if name == "base":
            continue
        end = blocks[position + 1][0] if position + 1 < len(blocks) else len(body)
        chunk = body[category[0] : end]
        names = found.setdefault(name, [])
        for match in _EFFECT_NAME_RE.finditer(chunk):
            effect = clean(match.group(1))
            if effect and effect not in names:
                names.append(effect)
    ordered: Dict[str, List[str]] = {}
    for name in EFFECT_ORDER:
        if found.get(name):
            ordered[name] = found.pop(name)
    for name in sorted(found):
        if found[name]:
            ordered[name] = found[name]
    return ordered


# ------------------------------------------------------------------ formatting


def num(value: Any) -> str:
    """``15.0`` -> ``15``, ``1.5`` -> ``1.5``, ``None`` -> ``""``."""
    if value is None or value == "":
        return ""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return str(value)
    if number.is_integer():
        return str(int(number))
    return f"{number:g}"


def fmt_range(bounds: Any, units: str = "") -> str:
    """``{"min": 75, "max": 150}`` -> ``75 - 150 µg``."""
    if not isinstance(bounds, dict):
        return ""
    low = num(bounds.get("min"))
    high = num(bounds.get("max"))
    if not low and not high:
        return ""
    if not high or high == low:
        text = low
    else:
        text = f"{low} - {high}" if low else high
    return f"{text} {units}".strip()


def dose_rows(dose: Any) -> List[Tuple[str, str]]:
    """Turn a GraphQL ``SubstanceRoaDose`` into ``(label, value)`` rows."""
    if not isinstance(dose, dict):
        return []
    units = str(dose.get("units") or "").strip()
    rows: List[Tuple[str, str]] = []
    threshold = num(dose.get("threshold"))
    if threshold:
        rows.append(("Threshold", f"{threshold} {units}".strip()))
    light = fmt_range(dose.get("light"), units)
    if light:
        rows.append(("Light", light))
    common = fmt_range(dose.get("common"), units)
    if common:
        rows.append(("Common", common))
    strong = fmt_range(dose.get("strong"), units)
    if strong:
        rows.append(("Strong", strong))
    heavy = num(dose.get("heavy"))
    if heavy:
        rows.append(("Heavy", f"{heavy} {units} +".replace("  ", " ").strip()))
    return rows


def duration_rows(duration: Any) -> List[Tuple[str, str]]:
    """Turn a GraphQL ``SubstanceRoaDuration`` into ``(label, value)`` rows."""
    if not isinstance(duration, dict):
        return []
    rows: List[Tuple[str, str]] = []
    for key, label in (
        ("total", "Total"),
        ("onset", "Onset"),
        ("comeup", "Come up"),
        ("peak", "Peak"),
        ("offset", "Offset"),
        ("afterglow", "After effects"),
    ):
        block = duration.get(key)
        if not isinstance(block, dict):
            continue
        value = fmt_range(block, str(block.get("units") or "").strip())
        if value:
            rows.append((label, value))
    return rows


def _drop_plural(value: str) -> str:
    """``dissociatives`` -> ``dissociative``, ``class`` -> ``class``."""
    if value.endswith("ss") or not value.endswith("s"):
        return value
    return value[:-1]


def class_matches(query: str, label: str) -> bool:
    """Compare class names tolerantly, so ``dissociative`` finds ``Dissociatives``."""
    left = norm(query)
    right = norm(label)
    if not left or not right:
        return False
    return left == right or _drop_plural(left) == _drop_plural(right)


def humanize_class(name: str) -> str:
    """``psychedelic`` -> ``Psychedelic``."""
    name = (name or "").strip()
    if not name:
        return ""
    if name.islower():
        return name[:1].upper() + name[1:]
    return name


def paginate(lines: Sequence[str], per_page: int) -> List[List[str]]:
    """Split rendered lines into embed-sized pages, never breaking a bullet."""
    pages: List[List[str]] = []
    page: List[str] = []
    for line in lines:
        if len(page) >= per_page:
            pages.append(page)
            page = []
        page.append(line)
    if page:
        pages.append(page)
    return pages
