"""Pure game logic for the IdleRPG cog — no ``discord`` or ``redbot`` imports.

Keeping the rules here means they can be exercised without a running bot:
time formulas, penalties, item rolls, combat, fate probabilities and the
serialization of a character.

The formulas follow the IRC classic idlerpg.net, with defaults tuned for a
Discord server (see ``DEFAULTS`` in ``idlerpg.py``):

* Time to the next level: ``base * growth ** level``, linear with
  ``cap_extra`` seconds per level once ``cap_level`` is passed.
* Penalty for a message: ``base * 1.14 ** level`` per counted character.
* Item: level 1 to ``level * item_mult``, weight ``1 / 1.4 ** n``.
* Battle: sum of item levels, one random number each, the higher one wins.
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
    "weapon": "Weapon",
    "shield": "Shield",
    "helm": "Helm",
    "tunic": "Armor",
    "gloves": "Gloves",
    "leggings": "Leggings",
    "boots": "Boots",
    "ring": "Ring",
    "amulet": "Amulet",
    "charm": "Charm",
}

ALIGNMENTS: Tuple[str, ...] = ("good", "neutral", "evil")
ALIGN_LABELS: Dict[str, str] = {"good": "good", "neutral": "neutral", "evil": "evil"}
ALIGN_ICONS: Dict[str, str] = {"good": "🕊️", "neutral": "⚖️", "evil": "😈"}
ALIGN_ALIASES: Dict[str, str] = {
    "good": "good",
    "neutral": "neutral",
    "evil": "evil",
}

# Critical strike chance per alignment (IRPG: good 1/50, evil 1/20, neutral
# left unchanged).
CRIT_CHANCE: Dict[str, float] = {"good": 1 / 50, "neutral": 1 / 35, "evil": 1 / 20}
STEAL_CHANCE = 0.019

# Item names: a noun per slot plus a genitive phrase per tier.  The phrase
# carries its own article, so every combination reads correctly ("Blade of the
# Knight", "Armor of the Knight").
ITEM_NOUNS: Dict[str, Tuple[str, ...]] = {
    "weapon": ("Blade", "Axe", "Mace", "Spear", "Longsword", "Scythe", "Dagger"),
    "shield": ("Shield", "Bulwark", "Buckler", "Aegis", "Tower Shield"),
    "helm": ("Helm", "Crown", "Hood", "Visor", "Great Helm"),
    "tunic": ("Armor", "Chainmail", "Jerkin", "Cuirass", "Cloak"),
    "gloves": ("Gloves", "Gauntlets", "Mitts", "Paws"),
    "leggings": ("Greaves", "Leggings", "Shin Guards", "Trousers"),
    "boots": ("Boots", "Treads", "Sandals", "Shoes"),
    "ring": ("Ring", "Band", "Signet Ring"),
    "amulet": ("Amulet", "Pendant", "Medallion"),
    "charm": ("Talisman", "Lucky Charm", "Ward"),
}

NAME_SUFFIXES: Tuple[Tuple[int, Tuple[str, ...]], ...] = (
    (10, ("of the Novice", "of Practice", "of the Wanderer", "of First Steps")),
    (25, ("of the Mercenary", "of the Watch", "of the Recruit", "of the Borderlands")),
    (45, ("of the Knight", "of Dusk", "of the North Wind", "of the Old Road")),
    (70, ("of the Warlord", "of Embers", "of the Thunderstorm", "of the Siege")),
    (100, ("of the Archmage", "of Darkness", "of the Heavens", "of the Starsmith")),
    (10**9, ("of Eternity", "of the Worldfire", "of Creation", "of the Abyss")),
)

# Unique items: fixed level range, minimum hero level and a 1/40 chance per
# level-up.
UNIQUES: Tuple[Dict[str, Any], ...] = (
    {"name": "Crown of Omniscience", "slot": "helm", "lo": 50, "hi": 74, "req": 25},
    {"name": "Ring of Sparkling Glory", "slot": "ring", "lo": 50, "hi": 74, "req": 25},
    {"name": "Protector's Plate Mail", "slot": "tunic", "lo": 75, "hi": 99, "req": 30},
    {
        "name": "Amulet of Storm Essence",
        "slot": "amulet",
        "lo": 100,
        "hi": 124,
        "req": 35,
    },
    {
        "name": "Jotun's Colossal Blade",
        "slot": "weapon",
        "lo": 150,
        "hi": 174,
        "req": 40,
    },
    {"name": "Cane of Blind Rage", "slot": "weapon", "lo": 175, "hi": 200, "req": 45},
    {"name": "Boots of Swiftness", "slot": "boots", "lo": 250, "hi": 300, "req": 48},
    {"name": "Cluehammer of Doom", "slot": "weapon", "lo": 300, "hi": 350, "req": 52},
)

QUEST_NAMES: Tuple[str, ...] = (
    "The Lost Crown of Mistvale",
    "The Pact with the Frost Giant",
    "Whispers in the Old Aqueduct",
    "The Rescue of the Caravan",
    "The Key to the Starsmith's Forge",
    "The Wakening of the Ember Gorge",
    "The Last Watch on the North Wall",
    "The Song of the Sunken Bell",
)


def _setting(settings: Dict[str, Any], key: str, default: Any) -> Any:
    value = settings.get(key, default)
    return default if value is None else value


def next_level_time(level: int, settings: Dict[str, Any]) -> float:
    """Seconds level ``level`` needs to reach the next level."""
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
    """Penalty seconds for an event of weight ``base``."""
    if base <= 0:
        return 0.0
    return float(base) * (1.14 ** max(1, int(level)))


def item_sum(items: Dict[str, Dict[str, Any]]) -> int:
    return sum(int(item.get("level", 0)) for item in (items or {}).values())


def effective_sum(player: "Player") -> float:
    """Item sum with the alignment modifier (good +10 %, evil -10 %)."""
    total = float(player.item_sum())
    if player.align == "good":
        total *= 1.10
    elif player.align == "evil":
        total *= 0.90
    return max(1.0, total)


def weighted_item_level(max_level: int, rng: random.Random) -> int:
    """Level of a found item, weighted ``1 / 1.4 ** n``."""
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


def _suffixes_for(level: int) -> Tuple[str, ...]:
    for ceiling, suffixes in NAME_SUFFIXES:
        if level < ceiling:
            return suffixes
    return NAME_SUFFIXES[-1][1]


def name_variants(slot: str, level: int) -> int:
    """How many different names exist for this slot at this level."""
    return len(ITEM_NOUNS.get(slot, ("Item",))) * len(_suffixes_for(level))


def item_name(slot: str, level: int, variant: int = 0) -> str:
    """Deterministic name for an item.

    The name is derived from slot, level and a stored variant instead of being
    kept in the save data, so renaming or translating the tables updates every
    character instead of leaving old names behind.
    """
    nouns = ITEM_NOUNS.get(slot, ("Item",))
    suffixes = _suffixes_for(level)
    noun = nouns[int(variant) % len(nouns)]
    suffix = suffixes[(int(variant) // len(nouns)) % len(suffixes)]
    return f"{noun} {suffix}"


def roll_item(
    level: int, settings: Dict[str, Any], rng: random.Random
) -> Dict[str, Any]:
    """Roll an item for a level-up to ``level``.

    Unique items are checked first (when enabled), then a regular find up to
    ``level * item_mult``.
    """
    if _setting(settings, "uniques", True):
        for index, unique in enumerate(UNIQUES):
            if level >= unique["req"] and rng.random() < 1 / 40:
                return {
                    "name": unique["name"],
                    "slot": unique["slot"],
                    "level": rng.randint(unique["lo"], unique["hi"]),
                    "unique": True,
                    "variant": index,
                }

    mult = float(_setting(settings, "item_mult", 1.5))
    max_level = max(1, int(level * mult))
    found = weighted_item_level(max_level, rng)
    slot = rng.choice(SLOTS)
    variant = rng.randrange(name_variants(slot, found))
    return {
        "name": item_name(slot, found, variant),
        "slot": slot,
        "level": found,
        "unique": False,
        "variant": variant,
    }


def equip(player: "Player", item: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Equip an item if it is better. Returns the replaced one."""
    slot = item["slot"]
    current = player.items.get(slot)
    if current is not None and int(current.get("level", 0)) >= int(item["level"]):
        return None
    player.items[slot] = item
    return current


@dataclass
class BattleResult:
    """Result of a battle, without touching either character."""

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


def battle(
    atk: "Player", dfn: "Player", settings: Dict[str, Any], rng: random.Random
) -> BattleResult:
    """Resolve a battle with the IRPG rules.

    ``atk_delta`` and ``def_delta`` are seconds added to the remaining time
    (positive = penalty, negative = reward).
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
    """Convert a per-day probability to a tick interval."""
    if per_day <= 0:
        return 0.0
    per_day = min(1.0, float(per_day))
    return 1.0 - (1.0 - per_day) ** (max(0.0, dt) / 86400.0)


def fmt_duration(seconds: float) -> str:
    total = int(max(0.0, round(float(seconds))))
    if total < 60:
        return f"{total}s"
    days, rest = divmod(total, 86400)
    hours, rest = divmod(rest, 3600)
    minutes, _ = divmod(rest, 60)
    parts: List[str] = []
    if days:
        parts.append(f"{days}d")
    if hours:
        parts.append(f"{hours}h")
    if minutes and len(parts) < 2:
        parts.append(f"{minutes}m")
    if not parts:
        parts.append(f"{total}s")
    return " ".join(parts[:2])


def bar(fraction: float, width: int = 14) -> str:
    fraction = max(0.0, min(1.0, float(fraction)))
    filled = int(round(fraction * width))
    return "▰" * filled + "▱" * (width - filled)


def percent(fraction: float) -> str:
    return f"{max(0.0, min(1.0, float(fraction))) * 100:.1f}%"


def _unique_for(
    slot: str, level: int, stored_name: str, variant: int
) -> Optional[Dict[str, Any]]:
    """Pick the unique item a saved entry belongs to (old saves lack ``variant``)."""
    if 0 <= variant < len(UNIQUES) and UNIQUES[variant]["slot"] == slot:
        return UNIQUES[variant]
    for unique in UNIQUES:
        if unique["name"] == stored_name:
            return unique
    for unique in UNIQUES:
        if unique["slot"] == slot and unique["lo"] <= level <= unique["hi"]:
            return unique
    return None


def _item_display_name(
    slot: str, level: int, unique: bool, variant: int, stored_name: str
) -> str:
    if unique:
        match = _unique_for(slot, level, stored_name, variant)
        return match["name"] if match else (stored_name or "Relic")
    return item_name(slot, level, variant)


def _class_name(value: Any) -> str:
    """Older saves may carry the German default class."""
    text = str(value or "").strip() or "Adventurer"
    if text == "Abenteurer":
        return "Adventurer"
    return text


@dataclass
class Player:
    """A character. ``ttl`` is the seconds remaining to the next level."""

    name: str
    cls: str = "Adventurer"
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

    # -- derived values ---------------------------------------------------
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

    # -- serialization ----------------------------------------------------
    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "cls": self.cls,
            "align": self.align,
            "level": int(self.level),
            "ttl": float(self.ttl),
            "items": {
                slot: {
                    "name": item.get("name", ""),
                    "slot": slot,
                    "level": int(item.get("level", 1)),
                    "unique": bool(item.get("unique")),
                    "variant": int(item.get("variant") or 0),
                }
                for slot, item in (self.items or {}).items()
            },
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
                    level_value = int(item.get("level") or 1)
                    unique = bool(item.get("unique"))
                    variant = int(item.get("variant") or 0)
                except (TypeError, ValueError):
                    continue
                slot_name = item.get("slot")
                if slot_name not in SLOTS:
                    slot_name = slot
                items[slot_name] = {
                    "name": _item_display_name(
                        slot_name,
                        level_value,
                        unique,
                        variant,
                        str(item.get("name") or ""),
                    ),
                    "slot": slot_name,
                    "level": level_value,
                    "unique": unique,
                    "variant": variant,
                }
        return cls(
            name=str(data.get("name") or "Nameless"),
            cls=_class_name(data.get("cls")),
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
