"""Reine Spiellogik für das IdleRPG-Cog — ohne ``discord``- oder ``redbot``-Importe.

Damit lassen sich alle Regeln ohne laufenden Bot prüfen: Zeitformeln, Strafen,
Gegenstandswürfe, Kämpfe, Schicksalswahrscheinlichkeiten und die
Serialisierung der Spielerfigur.

Die Formeln folgen dem IRC-Klassiker idlerpg.net, die Standardwerte sind aber
auf einen Discord-Server abgestimmt (siehe ``DEFAULTS`` in ``idlerpg.py``):

* Zeit bis zur nächsten Stufe: ``base * growth ** level``, ab ``cap_level``
  linear mit ``cap_extra`` Sekunden pro weiterer Stufe.
* Strafe für eine Nachricht: ``base * 1.14 ** level`` pro gezähltem Zeichen.
* Gegenstand: Stufe 1 bis ``level * item_mult``, Gewicht ``1 / 1.4 ** n``.
* Kampf: Summe der Gegenstandsstufen, je eine Zufallszahl, die größere gewinnt.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

SLOTS: Tuple[str, ...] = (
    "weapon",
    "shield",
    "helm",
    "tunic",
    "gloves",
    "leggings",
    "boots",
    "ring",
    "amulet",
    "charm",
)

SLOT_LABELS: Dict[str, str] = {
    "weapon": "Waffe",
    "shield": "Schild",
    "helm": "Helm",
    "tunic": "Rüstung",
    "gloves": "Handschuhe",
    "leggings": "Beinschutz",
    "boots": "Stiefel",
    "ring": "Ring",
    "amulet": "Amulett",
    "charm": "Talisman",
}

ALIGNMENTS: Tuple[str, ...] = ("good", "neutral", "evil")
ALIGN_LABELS: Dict[str, str] = {"good": "gut", "neutral": "neutral", "evil": "böse"}
ALIGN_ICONS: Dict[str, str] = {"good": "🕊️", "neutral": "⚖️", "evil": "😈"}
ALIGN_ALIASES: Dict[str, str] = {
    "good": "good",
    "gut": "good",
    "neutral": "neutral",
    "evil": "evil",
    "böse": "evil",
    "boese": "evil",
}

# Trefferchance für einen kritischen Treffer je Gesinnung (IRPG: gut 1/50,
# böse 1/20, neutral unverändert).
CRIT_CHANCE: Dict[str, float] = {"good": 1 / 50, "neutral": 1 / 35, "evil": 1 / 20}
STEAL_CHANCE = 0.019

# Gegenstandsnamen: Grundwort je Slot plus Genitivphrase je Stufe.
# Die Genitivphrase richtet sich nach dem Suffix, nicht nach dem Grundwort —
# damit stimmt das Geschlecht in jeder Kombination ("Klinge des Ritters",
# "Rüstung des Ritters").
ITEM_NOUNS: Dict[str, Tuple[str, ...]] = {
    "weapon": (
        "Klinge",
        "Axt",
        "Streitkolben",
        "Speer",
        "Langschwert",
        "Sense",
        "Dolch",
    ),
    "shield": ("Schild", "Bollwerk", "Buckler", "Aegis", "Turmschild"),
    "helm": ("Helm", "Krone", "Kapuze", "Visier", "Sturmhaube"),
    "tunic": ("Rüstung", "Kettenhemd", "Wams", "Harnisch", "Mantel"),
    "gloves": ("Handschuhe", "Stulpen", "Fäustlinge", "Pranken"),
    "leggings": ("Beinschienen", "Gamaschen", "Schienen", "Beinkleid"),
    "boots": ("Stiefel", "Treter", "Sandalen", "Schuhe"),
    "ring": ("Ring", "Reif", "Siegelring"),
    "amulet": ("Amulett", "Anhänger", "Medaillon"),
    "charm": ("Talisman", "Glücksbringer", "Zauberzeichen"),
}

NAME_SUFFIXES: Tuple[Tuple[int, Tuple[str, ...]], ...] = (
    (10, ("des Nachwuchses", "der Übung", "des Wanderers", "der ersten Schritte")),
    (25, ("des Söldners", "der Wache", "des Rekruten", "der Grenzlande")),
    (45, ("des Ritters", "der Dämmerung", "des Nordwinds", "der alten Straße")),
    (70, ("des Kriegsfürsten", "der Glut", "des Gewitters", "der Belagerung")),
    (100, ("des Erzmagiers", "der Finsternis", "des Himmels", "der Sternenschmiede")),
    (10**9, ("der Ewigkeit", "des Weltbrands", "der Schöpfung", "des Abgrunds")),
)

# Einzigartige Gegenstände: feste Stufe im Bereich, Mindeststufe des Helden
# und je 1/40 Chance pro Stufenaufstieg.
UNIQUES: Tuple[Dict[str, Any], ...] = (
    {"name": "Krone der Allwissenheit", "slot": "helm", "lo": 50, "hi": 74, "req": 25},
    {
        "name": "Ring der funkelnden Glorie",
        "slot": "ring",
        "lo": 50,
        "hi": 74,
        "req": 25,
    },
    {
        "name": "Plattenpanzer des Protektors",
        "slot": "tunic",
        "lo": 75,
        "hi": 99,
        "req": 30,
    },
    {
        "name": "Amulett der Sturmessenz",
        "slot": "amulet",
        "lo": 100,
        "hi": 124,
        "req": 35,
    },
    {
        "name": "Kolossklinge des Jotuns",
        "slot": "weapon",
        "lo": 150,
        "hi": 174,
        "req": 40,
    },
    {"name": "Stab der blinden Wut", "slot": "weapon", "lo": 175, "hi": 200, "req": 45},
    {"name": "Stiefel der Flinkheit", "slot": "boots", "lo": 250, "hi": 300, "req": 48},
    {
        "name": "Streitkolben des Verderbens",
        "slot": "weapon",
        "lo": 300,
        "hi": 350,
        "req": 52,
    },
)

QUEST_NAMES: Tuple[str, ...] = (
    "Die verlorene Krone von Nebelthal",
    "Der Pakt mit dem Frostriesen",
    "Das Flüstern im alten Aquädukt",
    "Die Rettung der Karawane",
    "Der Schlüssel zur Sternenschmiede",
    "Das Erwachen der glutroten Schlucht",
    "Die letzte Wache am Nordwall",
    "Der Gesang der versunkenen Glocke",
)


def _setting(settings: Dict[str, Any], key: str, default: Any) -> Any:
    value = settings.get(key, default)
    return default if value is None else value


def next_level_time(level: int, settings: Dict[str, Any]) -> float:
    """Sekunden, die Stufe ``level`` bis zur nächsten Stufe braucht."""
    base = float(_setting(settings, "base_time", 300.0))
    growth = float(_setting(settings, "growth", 1.14))
    cap = int(_setting(settings, "cap_level", 50))
    extra = float(_setting(settings, "cap_extra", 43200.0))
    speed = float(_setting(settings, "speed", 1.0)) or 1.0

    level = max(1, min(int(level), cap + 10_000))
    if level > cap:
        raw = base * (growth**cap) + extra * (level - cap)
    else:
        raw = base * (growth**level)
    return max(1.0, raw / speed)


def penalty_time(level: int, base: float) -> float:
    """Strafsekunden für ein Ereignis der Gewichtung ``base``."""
    if base <= 0:
        return 0.0
    return float(base) * (1.14 ** max(1, int(level)))


def item_sum(items: Dict[str, Dict[str, Any]]) -> int:
    return sum(int(item.get("level", 0)) for item in (items or {}).values())


def effective_sum(player: "Player") -> float:
    """Gegenstandssumme mit Gesinnungsbonus (gut +10 %, böse -10 %)."""
    total = float(player.item_sum())
    if player.align == "good":
        total *= 1.10
    elif player.align == "evil":
        total *= 0.90
    return max(1.0, total)


def weighted_item_level(max_level: int, rng: random.Random) -> int:
    """Stufe eines gefundenen Gegenstands, Gewicht ``1 / 1.4 ** n``."""
    max_level = max(1, int(max_level))
    q = 1.0 / 1.4
    total = q * (1.0 - q**max_level) / (1.0 - q)
    roll = rng.random() * total
    acc = 0.0
    for level in range(1, max_level + 1):
        acc += q**level
        if roll <= acc:
            return level
    return max_level


def _suffix_for(level: int, rng: random.Random) -> str:
    for ceiling, suffixes in NAME_SUFFIXES:
        if level < ceiling:
            return rng.choice(suffixes)
    return rng.choice(NAME_SUFFIXES[-1][1])


def item_name(slot: str, level: int, rng: random.Random) -> str:
    noun = rng.choice(ITEM_NOUNS.get(slot, ("Gegenstand",)))
    return f"{noun} {_suffix_for(level, rng)}"


def roll_item(
    level: int, settings: Dict[str, Any], rng: random.Random
) -> Dict[str, Any]:
    """Einen Gegenstand für einen Aufstieg auf ``level`` würfeln.

    Zuerst wird auf einen einzigartigen Gegenstand geprüft (falls aktiviert),
    danach auf einen normalen Fund bis ``level * item_mult``.
    """
    if _setting(settings, "uniques", True):
        for unique in UNIQUES:
            if level >= unique["req"] and rng.random() < 1 / 40:
                return {
                    "name": unique["name"],
                    "slot": unique["slot"],
                    "level": rng.randint(unique["lo"], unique["hi"]),
                    "unique": True,
                }

    mult = float(_setting(settings, "item_mult", 1.5))
    max_level = max(1, int(level * mult))
    found = weighted_item_level(max_level, rng)
    slot = rng.choice(SLOTS)
    return {
        "name": item_name(slot, found, rng),
        "slot": slot,
        "level": found,
        "unique": False,
    }


def equip(player: "Player", item: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Gegenstand anlegen, wenn er besser ist. Gibt den ersetzten zurück."""
    slot = item["slot"]
    current = player.items.get(slot)
    if current is not None and int(current.get("level", 0)) >= int(item["level"]):
        return None
    player.items[slot] = item
    return current


@dataclass
class BattleResult:
    """Ergebnis eines Kampfes, ohne die Spielerfigur zu verändern."""

    win: bool
    atk_roll: float
    def_roll: float
    percent: float
    crit: bool
    atk_delta: float
    def_delta: float
    steal_slot: Optional[str] = None
    steal_item: Optional[Dict[str, Any]] = None
    give_back: Optional[Dict[str, Any]] = None

    @property
    def atk_sum(self) -> float:
        return self.atk_roll

    @property
    def def_sum(self) -> float:
        return self.def_roll


def battle(
    atk: "Player", dfn: "Player", settings: Dict[str, Any], rng: random.Random
) -> BattleResult:
    """Kampf nach IRPG-Regeln auswerten.

    ``atk_delta`` und ``def_delta`` sind Sekunden, die auf die Restzeit addiert
    werden (positiv = Strafe, negativ = Belohnung).
    """
    atk_power = effective_sum(atk)
    def_power = effective_sum(dfn)
    atk_roll = rng.uniform(0.0, atk_power)
    def_roll = rng.uniform(0.0, def_power)
    win = atk_roll > def_roll

    atk_need = next_level_time(atk.level, settings)
    def_need = next_level_time(dfn.level, settings)

    crit = False
    steal_slot: Optional[str] = None
    steal_item: Optional[Dict[str, Any]] = None
    give_back: Optional[Dict[str, Any]] = None

    if win:
        percent = max(dfn.level / 4.0, 7.0) / 100.0
        atk_delta = -percent * atk_need
        def_delta = 0.0
        if rng.random() < CRIT_CHANCE.get(atk.align, 1 / 35):
            crit = True
            def_delta = rng.uniform(5.0, 25.0) / 100.0 * def_need
        if rng.random() < STEAL_CHANCE:
            better = [
                slot
                for slot, item in (dfn.items or {}).items()
                if slot in SLOTS
                and int(item.get("level", 0))
                > int((atk.items.get(slot) or {}).get("level", 0))
            ]
            if better:
                steal_slot = rng.choice(better)
                steal_item = dict(dfn.items[steal_slot])
                give_back = atk.items.get(steal_slot)
    else:
        percent = max(dfn.level / 7.0, 7.0) / 100.0
        atk_delta = percent * atk_need
        def_delta = 0.0

    return BattleResult(
        win=win,
        atk_roll=atk_roll,
        def_roll=def_roll,
        percent=percent,
        crit=crit,
        atk_delta=atk_delta,
        def_delta=def_delta,
        steal_slot=steal_slot,
        steal_item=steal_item,
        give_back=give_back,
    )


def chance_per_tick(per_day: float, dt: float) -> float:
    """Tageswahrscheinlichkeit auf ein Tickintervall umrechnen."""
    if per_day <= 0:
        return 0.0
    per_day = min(1.0, float(per_day))
    return 1.0 - (1.0 - per_day) ** (max(0.0, dt) / 86400.0)


def fmt_duration(seconds: float) -> str:
    total = int(max(0.0, round(float(seconds))))
    if total < 60:
        return f"{total} s"
    days, rest = divmod(total, 86400)
    hours, rest = divmod(rest, 3600)
    minutes, secs = divmod(rest, 60)
    parts: List[str] = []
    if days:
        parts.append(f"{days} T")
    if hours:
        parts.append(f"{hours} Std")
    if minutes and len(parts) < 2:
        parts.append(f"{minutes} Min")
    if not parts:
        parts.append(f"{secs} s")
    return " ".join(parts[:2])


def bar(fraction: float, width: int = 14) -> str:
    fraction = max(0.0, min(1.0, float(fraction)))
    filled = int(round(fraction * width))
    return "▰" * filled + "▱" * (width - filled)


def percent(fraction: float) -> str:
    return f"{max(0.0, min(1.0, float(fraction))) * 100:.1f} %".replace(".", ",")


@dataclass
class Player:
    """Eine Spielfigur. ``ttl`` sind die Restsekunden bis zur nächsten Stufe."""

    name: str
    cls: str = "Abenteurer"
    align: str = "neutral"
    level: int = 1
    ttl: float = 0.0
    items: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    created: float = 0.0
    idle: float = 0.0
    wins: int = 0
    losses: int = 0
    crits: int = 0
    steals: int = 0
    levels_gained: int = 0
    penalties: int = 0
    quest_sin: bool = False
    last_seen: float = 0.0

    # -- abgeleitete Werte -------------------------------------------------
    def next_time(self, settings: Dict[str, Any]) -> float:
        return next_level_time(self.level, settings)

    def progress(self, settings: Dict[str, Any]) -> float:
        need = self.next_time(settings)
        remaining = max(0.0, float(self.ttl))
        return max(0.0, min(1.0, 1.0 - remaining / need))

    def item_sum(self) -> int:
        return item_sum(self.items)

    def slot_item(self, slot: str) -> Optional[Dict[str, Any]]:
        return (self.items or {}).get(slot)

    # -- Serialisierung ----------------------------------------------------
    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "cls": self.cls,
            "align": self.align,
            "level": int(self.level),
            "ttl": float(self.ttl),
            "items": {slot: dict(item) for slot, item in (self.items or {}).items()},
            "created": float(self.created),
            "idle": float(self.idle),
            "wins": int(self.wins),
            "losses": int(self.losses),
            "crits": int(self.crits),
            "steals": int(self.steals),
            "levels_gained": int(self.levels_gained),
            "penalties": int(self.penalties),
            "quest_sin": bool(self.quest_sin),
            "last_seen": float(self.last_seen),
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Player":
        if not isinstance(data, dict):
            raise ValueError("player payload is not a dict")
        align = str(data.get("align") or "neutral").lower()
        if align not in ALIGNMENTS:
            align = "neutral"
        items: Dict[str, Dict[str, Any]] = {}
        raw_items = data.get("items") or {}
        if isinstance(raw_items, dict):
            for slot, item in raw_items.items():
                if slot not in SLOTS or not isinstance(item, dict):
                    continue
                try:
                    items[slot] = {
                        "name": str(
                            item.get("name") or SLOT_LABELS.get(slot, "Gegenstand")
                        ),
                        "slot": slot,
                        "level": int(item.get("level") or 1),
                        "unique": bool(item.get("unique")),
                    }
                except (TypeError, ValueError):
                    continue
        return cls(
            name=str(data.get("name") or "Namenlos"),
            cls=str(data.get("cls") or "Abenteurer"),
            align=align,
            level=max(1, int(data.get("level") or 1)),
            ttl=float(data.get("ttl") or 0.0),
            items=items,
            created=float(data.get("created") or 0.0),
            idle=float(data.get("idle") or 0.0),
            wins=int(data.get("wins") or 0),
            losses=int(data.get("losses") or 0),
            crits=int(data.get("crits") or 0),
            steals=int(data.get("steals") or 0),
            levels_gained=int(data.get("levels_gained") or 0),
            penalties=int(data.get("penalties") or 0),
            quest_sin=bool(data.get("quest_sin")),
            last_seen=float(data.get("last_seen") or 0.0),
        )
