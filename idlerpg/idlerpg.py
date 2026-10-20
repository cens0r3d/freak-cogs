"""IdleRPG for Discord — register, idle, level up.

A port of the IRC classic idlerpg.net to Discord: a registered character
levels purely by idling, finds an item on every level-up and fights whoever
else is around. The Discord account replaces the password, the guild replaces
the IRC channel — every server is its own realm.

Every rule is configurable per server (`[p]irpg set …`); the game logic itself
lives in ``game.py`` without framework imports.

Operational details worth knowing before changing anything:

* The tick runs every 15 seconds and only touches the in-memory cache
  (``self.players``). It is written back every 60 seconds and on unload — a
  per-player write on every tick would be one config write per second.
* "Online" means the character belongs to the guild and the member is in the
  cache. Leaving the server pauses the clock but keeps the character.
* Penalties cost time: a message costs ``penalty`` seconds per character,
  multiplied by ``1.14 ** level``. As in the original, silence is the best
  strategy.
* A guild gate belongs on every subcommand individually — ``set`` and
  ``admin`` are groups, and group checks are not inherited by subcommands.
"""

from __future__ import annotations

import asyncio
import contextlib
import io
import json
import logging
import random
import re
import time
from typing import Any, Dict, List, Optional, Sequence, Tuple

import discord
from discord.ext import tasks
from redbot.core import Config, commands
from redbot.core.bot import Red
from redbot.core.utils.chat_formatting import humanize_list
from redbot.core.utils.views import ConfirmView

from .game import (
    ALIGN_ALIASES,
    ALIGN_ICONS,
    ALIGN_LABELS,
    QUEST_NAMES,
    SLOTS,
    SLOT_LABELS,
    Player,
    bar,
    battle,
    chance_per_tick,
    effective_sum,
    equip,
    fmt_duration,
    next_level_time,
    penalty_time,
    percent,
    roll_item,
)

log = logging.getLogger("red.freak_cogs.idlerpg")

TICK_SECONDS = 15
FLUSH_SECONDS = 60
PANEL_COLOR = 0xE0B34C
NAME_RE = re.compile(r"^[\w \-'.]{3,24}$", re.UNICODE)

DEFAULTS: Dict[str, Any] = {
    # Time to the next level: base * growth ** level, linear past cap_level.
    "base_time": 300.0,
    "growth": 1.14,
    "cap_level": 50,
    "cap_extra": 43200.0,
    "speed": 1.0,
    "max_level": 120,
    # Items
    "item_mult": 1.5,
    "uniques": True,
    # Battles
    "battles": True,
    "battle_low": 25,
    "npc": True,
    # Fate (probability per day)
    "events": True,
    "godsend": 0.125,
    "calamity": 0.125,
    "hog": 0.05,
    "teambattle": 0.05,
    # Quests
    "quests": True,
    "quest_size": 3,
    "quest_min_level": 20,
    "quest_min_idle_hours": 6,
    "quest_min_hours": 6,
    "quest_max_hours": 12,
    "quest_cooldown": 7200,
    "quest_fail": 15.0,
    # Penalties
    "penalty": 1.0,
    "penalty_cap": 60,
    "penalty_leave": 0.0,
    # Output
    "announce": True,
}


def admin_command(func):
    """Guild and manage-server gate for a single (sub)command."""

    func = commands.admin_or_permissions(manage_guild=True)(func)
    func = commands.guild_only()(func)
    return func


class IdleRPG(commands.Cog):
    """Idle RPG: register a character, idle, level up, fight."""

    __version__ = "1.1.0"

    def __init__(self, bot: Red) -> None:
        self.bot = bot
        self.config = Config.get_conf(
            self, identifier=0x1D1E2A2B3C4D, force_registration=True
        )
        self.config.register_guild(
            enabled=False,
            channel=None,
            role=None,
            panel=None,
            settings=dict(DEFAULTS),
            players={},
            quest={},
        )

        self.players: Dict[int, Dict[int, Player]] = {}
        self.quests: Dict[int, Dict[str, Any]] = {}
        self._quest_loaded: set = set()
        self._next_quest: Dict[int, float] = {}
        self._conf: Dict[int, Dict[str, Any]] = {}
        self._last: Dict[int, float] = {}
        self._dirty: set = set()
        self._rng = random.Random()

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def cog_load(self) -> None:
        self.bot.add_view(RealmPanel(self))
        self._ticker.start()
        self._flusher.start()

    def cog_unload(self) -> None:
        self._ticker.cancel()
        self._flusher.cancel()
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        loop.create_task(self._flush())

    @tasks.loop(seconds=TICK_SECONDS)
    async def _ticker(self) -> None:
        now = time.time()
        for guild in list(self.bot.guilds):
            try:
                await self._tick_guild(guild, now)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("IdleRPG: tick for guild %s failed", guild.id)

    @_ticker.before_loop
    async def _before_ticker(self) -> None:
        await self.bot.wait_until_ready()

    @tasks.loop(seconds=FLUSH_SECONDS)
    async def _flusher(self) -> None:
        await self._flush()

    @_flusher.before_loop
    async def _before_flusher(self) -> None:
        await self.bot.wait_until_ready()

    # ------------------------------------------------------------------
    # Data access
    # ------------------------------------------------------------------

    async def _settings(self, guild: discord.Guild) -> Dict[str, Any]:
        cached = self._conf.get(guild.id)
        if cached is not None:
            return cached
        stored = await self.config.guild(guild).settings()
        merged = dict(DEFAULTS)
        merged.update(
            {key: value for key, value in (stored or {}).items() if key in DEFAULTS}
        )
        self._conf[guild.id] = merged
        return merged

    async def _players(self, guild: discord.Guild) -> Dict[int, Player]:
        cached = self.players.get(guild.id)
        if cached is not None:
            return cached
        raw = await self.config.guild(guild).players()
        players: Dict[int, Player] = {}
        for key, payload in (raw or {}).items():
            try:
                players[int(key)] = Player.from_dict(payload)
            except (TypeError, ValueError):
                log.warning(
                    "IdleRPG: unreadable character %s in guild %s", key, guild.id
                )
        self.players[guild.id] = players
        return players

    async def _save(self, guild_id: int) -> None:
        players = self.players.get(guild_id)
        if players is None:
            return
        payload = {
            str(user_id): player.to_dict() for user_id, player in players.items()
        }
        with contextlib.suppress(Exception):
            await self.config.guild_from_id(guild_id).players.set(payload)
        self._dirty.discard(guild_id)

    async def _flush(self) -> None:
        for guild_id in list(self._dirty):
            try:
                await self._save(guild_id)
            except Exception:
                log.exception("IdleRPG: saving guild %s failed", guild_id)

    async def _save_quest(self, guild_id: int, quest: Dict[str, Any]) -> None:
        self.quests[guild_id] = quest
        self._quest_loaded.add(guild_id)
        with contextlib.suppress(Exception):
            await self.config.guild_from_id(guild_id).quest.set(quest)

    # ------------------------------------------------------------------
    # The tick
    # ------------------------------------------------------------------

    async def _tick_guild(self, guild: discord.Guild, now: float) -> None:
        if await self.bot.cog_disabled_in_guild(self, guild):
            return
        if not await self.config.guild(guild).enabled():
            return

        conf = await self._settings(guild)
        players = await self._players(guild)

        last = self._last.get(guild.id)
        self._last[guild.id] = now
        if last is None:
            return

        dt = min(max(0.0, now - last), 3600.0)
        if dt <= 0:
            return

        online = {
            user_id: player
            for user_id, player in players.items()
            if guild.get_member(user_id)
        }
        lines: List[str] = []

        for player in list(online.values()):
            player.idle += dt
            player.last_seen = now
            player.ttl -= dt
            self._advance(guild, player, players, online, conf, lines)
            if conf.get("events", True):
                self._fate(player, online, conf, dt, lines)

        if players:
            await self._quest_tick(guild, players, online, conf, now, lines)

        self._dirty.add(guild.id)
        await self._announce(guild, conf, lines)

    def _advance(
        self,
        guild: discord.Guild,
        player: Player,
        players: Dict[int, Player],
        online: Dict[int, Player],
        conf: Dict[str, Any],
        lines: List[str],
    ) -> None:
        """Handle level-ups (several when a lot of time piled up)."""
        max_level = int(conf.get("max_level", 120))
        guard = 0
        while player.ttl <= 0 and player.level < max_level and guard < 20:
            guard += 1
            overshoot = -player.ttl
            player.level += 1
            player.levels_gained += 1
            player.ttl = next_level_time(player.level, conf) - overshoot

            item = roll_item(player.level, conf, self._rng)
            previous = equip(player, item)
            label = SLOT_LABELS.get(item["slot"], "Item")
            marker = " ✨ **unique**" if item.get("unique") else ""
            if previous is None:
                lines.append(
                    f"🎉 **{player.name}** reaches **level {player.level}** and finds "
                    f"{label}: **{item['name']}** (level {item['level']}){marker}"
                )
            else:
                lines.append(
                    f"🎉 **{player.name}** reaches **level {player.level}** and trades "
                    f"{label} **{previous['name']}** ({previous['level']}) for "
                    f"**{item['name']}** (level {item['level']}){marker}"
                )

            if conf.get("battles", True):
                self._maybe_battle(guild, player, players, online, conf, lines)

        if player.level >= max_level:
            player.ttl = max(0.0, player.ttl)
        self._dirty.add(guild.id)

    def _maybe_battle(
        self,
        guild: discord.Guild,
        atk: Player,
        players: Dict[int, Player],
        online: Dict[int, Player],
        conf: Dict[str, Any],
        lines: List[str],
    ) -> None:
        if atk.level < 25 and self._rng.random() * 100 >= float(
            conf.get("battle_low", 25)
        ):
            return

        opponents = [other for other in online.values() if other is not atk]
        if opponents:
            defender = self._rng.choice(opponents)
        elif conf.get("npc", True) and self._rng.random() < 0.5:
            defender = self._shadow(atk, players)
        else:
            return

        result = battle(atk, defender, conf, self._rng)
        atk.ttl += result.atk_delta

        if result.win:
            atk.wins += 1
            defender.losses += 1
            lines.append(
                f"⚔️ **{atk.name}** (sum {int(effective_sum(atk))}) defeats "
                f"**{defender.name}** ({int(effective_sum(defender))}) — "
                f"{result.percent * 100:.0f}% faster toward level {atk.level + 1}."
            )
            if result.crit:
                atk.crits += 1
                defender.ttl += result.def_delta
                lines.append(
                    f"💥 Critical strike! **{defender.name}** loses another "
                    f"{result.def_delta / max(1.0, defender.next_time(conf)) * 100:.0f}%."
                )
            if result.steal_slot and result.steal_item:
                slot = result.steal_slot
                given = atk.items.get(slot)
                atk.items[slot] = result.steal_item
                if given is None:
                    defender.items.pop(slot, None)
                else:
                    defender.items[slot] = given
                atk.steals += 1
                lines.append(
                    f"🖐️ **{atk.name}** steals **{defender.name}**'s "
                    f"{SLOT_LABELS.get(slot, 'item')} **{result.steal_item['name']}** "
                    f"(level {result.steal_item['level']})."
                )
        else:
            atk.losses += 1
            defender.wins += 1
            lines.append(
                f"💀 **{atk.name}** loses to **{defender.name}** — "
                f"{result.percent * 100:.0f}% behind toward level {atk.level + 1}."
            )

    def _shadow(self, atk: Player, players: Dict[int, Player]) -> Player:
        """Stand-in opponent when nobody else is around (like the IRPG bot)."""
        best = max((player.item_sum() for player in players.values()), default=0)
        shadow = Player(
            name="Shadow of the Realm", cls="Apparition", level=max(1, atk.level)
        )
        shadow.items = {
            "weapon": {
                "name": "Shadowblade",
                "slot": "weapon",
                "level": max(1, best + 1),
                "unique": False,
            }
        }
        return shadow

    def _fate(
        self,
        player: Player,
        online: Dict[int, Player],
        conf: Dict[str, Any],
        dt: float,
        lines: List[str],
    ) -> None:
        """Hand of God, godsends, calamities and team battles."""
        need = player.next_time(conf)

        if self._rng.random() < chance_per_tick(float(conf.get("hog", 0.05)), dt):
            shift = self._rng.uniform(0.05, 0.75)
            if self._rng.random() < 0.8:
                player.ttl -= need * shift
                lines.append(
                    f"✨ The Hand of God guides **{player.name}** — "
                    f"{shift * 100:.0f}% faster."
                )
            else:
                player.ttl += need * shift
                lines.append(
                    f"⚡ The Hand of God smites **{player.name}** — "
                    f"{shift * 100:.0f}% slower."
                )

        if self._rng.random() < chance_per_tick(float(conf.get("godsend", 0.125)), dt):
            shift = self._rng.uniform(0.05, 0.12)
            if not player.items or self._rng.random() < 0.5:
                player.ttl -= need * shift
                lines.append(
                    f"🌟 A godsend: **{player.name}** is " f"{shift * 100:.0f}% faster."
                )
            else:
                slot = self._rng.choice(list(player.items))
                item = player.items[slot]
                item["level"] = max(1, int(round(int(item["level"]) * 1.10)))
                lines.append(
                    f"🌟 A godsend: **{player.name}**'s **{item['name']}** "
                    f"is upgraded to level {item['level']}."
                )

        if self._rng.random() < chance_per_tick(float(conf.get("calamity", 0.125)), dt):
            shift = self._rng.uniform(0.05, 0.12)
            if not player.items or self._rng.random() < 0.5:
                player.ttl += need * shift
                lines.append(
                    f"🌑 A calamity strikes **{player.name}** — "
                    f"{shift * 100:.0f}% slower."
                )
            else:
                slot = self._rng.choice(list(player.items))
                item = player.items[slot]
                item["level"] = max(1, int(round(int(item["level"]) * 0.90)))
                lines.append(
                    f"🌑 A calamity: **{player.name}**'s **{item['name']}** "
                    f"drops to level {item['level']}."
                )

        if (
            conf.get("battles", True)
            and len(online) >= 6
            and self._rng.random()
            < chance_per_tick(float(conf.get("teambattle", 0.05)), dt)
        ):
            members = list(online.values())
            self._rng.shuffle(members)
            side_a, side_b = members[:3], members[3:6]
            power_a = sum(effective_sum(member) for member in side_a)
            power_b = sum(effective_sum(member) for member in side_b)
            winner, loser = (
                (side_a, side_b)
                if self._rng.uniform(0, power_a) > self._rng.uniform(0, power_b)
                else (side_b, side_a)
            )
            lowest = min(winner, key=lambda member: member.next_time(conf))
            gain = 0.20 * lowest.next_time(conf)
            for member in winner:
                member.ttl -= gain
            for member in loser:
                member.ttl += gain
            names_win = humanize_list([f"**{member.name}**" for member in winner[:3]])
            names_lose = humanize_list([f"**{member.name}**" for member in loser[:3]])
            lines.append(
                f"🛡️ Team battle: {names_win} defeat {names_lose} — "
                f"{fmt_duration(gain)} awarded and lost."
            )

    async def _quest_tick(
        self,
        guild: discord.Guild,
        players: Dict[int, Player],
        online: Dict[int, Player],
        conf: Dict[str, Any],
        now: float,
        lines: List[str],
    ) -> None:
        if guild.id not in self._quest_loaded:
            self.quests[guild.id] = await self.config.guild(guild).quest() or {}
            self._quest_loaded.add(guild.id)

        quest = self.quests.get(guild.id) or {}

        if quest and now >= float(quest.get("end", 0)):
            member_ids = [int(member) for member in quest.get("members", [])]
            members = [
                players[member_id] for member_id in member_ids if member_id in players
            ]
            broke = (
                bool(quest.get("failed"))
                or len(members) < len(member_ids)
                or any(member.quest_sin for member in members)
            )
            if broke:
                penalty = float(conf.get("quest_fail", 15.0))
                for player in online.values():
                    player.ttl += penalty_time(player.level, penalty)
                lines.append(
                    f"💀 The quest **{quest.get('name', '?')}** has failed — "
                    "the realm punishes everyone present."
                )
            else:
                for member in members:
                    member.ttl -= 0.25 * member.next_time(conf)
                    member.quest_sin = False
                names = humanize_list([f"**{member.name}**" for member in members])
                lines.append(
                    f"🏆 The quest **{quest.get('name', '?')}** is complete: {names} "
                    "are 25% faster."
                )
            await self._save_quest(guild.id, {})
            self._next_quest[guild.id] = now + float(conf.get("quest_cooldown", 7200))
            self._dirty.add(guild.id)
            return

        if quest:
            return
        if not conf.get("quests", True) or now < self._next_quest.get(guild.id, 0.0):
            return

        size = max(2, int(conf.get("quest_size", 3)))
        min_level = int(conf.get("quest_min_level", 20))
        min_idle = float(conf.get("quest_min_idle_hours", 6)) * 3600
        eligible = [
            (user_id, player)
            for user_id, player in online.items()
            if player.level >= min_level and player.idle >= min_idle
        ]
        if len(eligible) < size:
            return

        chosen = self._rng.sample(eligible, size)
        hours = self._rng.uniform(
            float(conf.get("quest_min_hours", 6)),
            float(conf.get("quest_max_hours", 12)),
        )
        quest = {
            "name": self._rng.choice(QUEST_NAMES),
            "start": now,
            "end": now + hours * 3600.0,
            "members": [int(user_id) for user_id, _ in chosen],
            "failed": False,
        }
        for _, player in chosen:
            player.quest_sin = False
        await self._save_quest(guild.id, quest)
        names = humanize_list([f"**{player.name}**" for _, player in chosen])
        lines.append(
            f"🗺️ New quest: **{quest['name']}** — {names} set out. "
            f"{hours:.0f} hours, no penalties allowed."
        )

    async def _announce(
        self, guild: discord.Guild, conf: Dict[str, Any], lines: Sequence[str]
    ) -> None:
        if not lines or not conf.get("announce", True):
            return

        channel_id = await self.config.guild(guild).channel()
        channel = guild.get_channel(channel_id) if channel_id else None
        if not isinstance(channel, discord.TextChannel):
            channel = guild.system_channel
        if not isinstance(channel, discord.TextChannel):
            return

        text = "\n".join(list(lines)[:10])
        if len(lines) > 10:
            text += f"\n… and {len(lines) - 10} more events."
        with contextlib.suppress(discord.HTTPException):
            await channel.send(
                text[:1900], allowed_mentions=discord.AllowedMentions.none()
            )

    # ------------------------------------------------------------------
    # Listener
    # ------------------------------------------------------------------

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        """Talking costs time — exactly like the original."""
        if message.guild is None or message.author.bot:
            return
        players = self.players.get(message.guild.id)
        if not players:
            return
        player = players.get(message.author.id)
        if player is None:
            return

        content = message.content or ""
        try:
            prefixes = await self.bot.get_prefix(message)
        except Exception:
            prefixes = []
        if isinstance(prefixes, str):
            prefixes = [prefixes]
        if any(prefix and content.startswith(prefix) for prefix in prefixes):
            return

        conf = await self._settings(message.guild)
        base = float(conf.get("penalty", 1.0) or 0.0)
        if base <= 0:
            return
        counted = len(content[: int(conf.get("penalty_cap", 60))])
        if counted <= 0:
            return

        player.ttl += penalty_time(player.level, base * counted)
        player.penalties += 1
        player.quest_sin = True
        self._dirty.add(message.guild.id)

    @commands.Cog.listener()
    async def on_member_remove(self, member: discord.Member) -> None:
        """Optionally book a penalty when a member leaves the server."""
        if member.bot or member.guild is None:
            return
        players = self.players.get(member.guild.id)
        if not players:
            return
        player = players.get(member.id)
        if player is None:
            return
        conf = await self._settings(member.guild)
        base = float(conf.get("penalty_leave", 0.0) or 0.0)
        if base > 0:
            player.ttl += penalty_time(player.level, base)
        player.quest_sin = True
        self._dirty.add(member.guild.id)

    @commands.Cog.listener()
    async def on_guild_channel_delete(self, channel: discord.abc.GuildChannel) -> None:
        """When the announcement channel is deleted, drop the reference."""
        conf = await self.config.guild(channel.guild).channel()
        if conf == channel.id:
            await self.config.guild(channel.guild).channel.set(None)

    # ------------------------------------------------------------------
    # Endnutzerdaten
    # ------------------------------------------------------------------

    async def red_get_data_for_user(self, *, user_id: int) -> Dict[str, io.BytesIO]:
        characters: Dict[str, Any] = {}
        for guild_id, conf in (await self.config.all_guilds()).items():
            payload = (conf.get("players") or {}).get(str(user_id))
            if not payload:
                continue
            guild = self.bot.get_guild(int(guild_id))
            characters[str(guild_id)] = {
                "server": guild.name if guild is not None else str(guild_id),
                "character": payload,
            }
        if not characters:
            return {}
        document = json.dumps(
            {"user_id": user_id, "characters": characters}, ensure_ascii=False, indent=2
        )
        return {"idlerpg.json": io.BytesIO(document.encode("utf-8"))}

    async def red_delete_data_for_user(self, *, requester: str, user_id: int) -> None:
        for guild_id in list((await self.config.all_guilds()).keys()):
            async with self.config.guild_from_id(guild_id).players() as players:
                players.pop(str(user_id), None)
            cached = self.players.get(guild_id)
            if cached is not None:
                cached.pop(user_id, None)
            quest = self.quests.get(guild_id)
            if quest and user_id in [
                int(member) for member in quest.get("members", [])
            ]:
                quest["failed"] = True
                await self._save_quest(guild_id, quest)
            self._dirty.discard(guild_id)

    # ------------------------------------------------------------------
    # Rendering
    # ------------------------------------------------------------------

    async def _colour(self, ctx: Optional[commands.Context] = None) -> discord.Colour:
        """Red's ``ctx.embed_color`` is a property that returns a coroutine.

        ``await ctx.embed_color()`` is the only valid call: handing the property
        straight to ``discord.Embed`` passes the method as the colour and
        discord.py raises ``TypeError: Expected discord.Colour … but received
        method``.  Without a context (panel views) the house colour applies.
        """
        fallback = discord.Colour(PANEL_COLOR)
        if ctx is None:
            return fallback
        try:
            getter = getattr(ctx, "embed_color", None)
        except Exception:
            return fallback
        if getter is None:
            return fallback
        try:
            colour = getter() if callable(getter) else getter
            if asyncio.iscoroutine(colour):
                colour = await colour
        except Exception:
            return fallback
        return colour if isinstance(colour, discord.Colour) else fallback

    def _char_embed(
        self,
        player: Player,
        conf: Dict[str, Any],
        colour: Optional[discord.Colour] = None,
    ) -> discord.Embed:
        progress = player.progress(conf)
        max_level = int(conf.get("max_level", 120))
        if player.level >= max_level:
            remaining = "maximum level reached"
        else:
            remaining = f"{fmt_duration(player.ttl)} to level {player.level + 1}"
        embed = discord.Embed(
            title=f"{ALIGN_ICONS.get(player.align, '⚖️')} {player.name}",
            description=(
                f"*{player.cls}* · {ALIGN_LABELS.get(player.align, 'neutral')} · "
                f"level **{player.level}**"
            ),
            colour=colour or discord.Colour(PANEL_COLOR),
        )
        embed.add_field(
            name="Progress",
            value=(f"`{bar(progress)}` {percent(progress)}\n{remaining}"),
            inline=False,
        )
        embed.add_field(name="Item sum", value=f"**{player.item_sum()}**", inline=True)
        embed.add_field(
            name="Battles", value=f"{player.wins} W / {player.losses} L", inline=True
        )
        embed.add_field(
            name="Record",
            value=(
                f"{player.levels_gained} level-ups · {player.crits} crits · "
                f"{player.steals} steals · {player.penalties} penalties\n"
                f"idled: {fmt_duration(player.idle)}"
            ),
            inline=False,
        )

        equipment = []
        for slot in SLOTS:
            item = player.items.get(slot)
            if item is None:
                equipment.append(f"{SLOT_LABELS[slot]}: *—*")
            else:
                marker = "✨" if item.get("unique") else ""
                equipment.append(
                    f"{SLOT_LABELS[slot]}: {marker}**{item['name']}** ({item['level']})"
                )
        embed.add_field(
            name="Equipment", value="\n".join(equipment)[:1024], inline=False
        )
        return embed

    def _realm_embed(
        self,
        guild: discord.Guild,
        players: Dict[int, Player],
        conf: Dict[str, Any],
        quest: Dict[str, Any],
        colour: Optional[discord.Colour] = None,
    ) -> discord.Embed:
        online = sum(1 for user_id in players if guild.get_member(user_id))
        top = sorted(players.values(), key=lambda p: (-p.level, p.ttl))[:3]
        best = (
            "\n".join(
                f"`{index}.` **{player.name}** — level {player.level} · items {player.item_sum()}"
                for index, player in enumerate(top, start=1)
            )
            or "*Nobody in the realm yet.*"
        )

        if quest:
            left = max(0.0, float(quest.get("end", 0)) - time.time())
            quest_text = f"**{quest.get('name', '?')}** · {fmt_duration(left)} left"
        else:
            quest_text = "*none*"

        embed = discord.Embed(
            title="⚔️ The Realm",
            description=(
                f"{len(players)} characters · {online} present\n"
                f"Pace: factor {conf.get('speed', 1.0):g} · "
                f"level 1 in {fmt_duration(next_level_time(1, conf))}"
            ),
            colour=colour or discord.Colour(PANEL_COLOR),
        )
        embed.add_field(name="Top", value=best, inline=False)
        embed.add_field(name="Quest", value=quest_text, inline=False)
        embed.add_field(
            name="Rules",
            value=(
                "Everyone registered levels by idling. "
                "Every message costs time, battles decide the items."
            ),
            inline=False,
        )
        return embed

    async def _rank_pages(
        self, guild: discord.Guild, conf: Dict[str, Any]
    ) -> List[discord.Embed]:
        players = await self._players(guild)
        ordered = sorted(players.values(), key=lambda p: (-p.level, p.ttl))
        chunks = [
            ordered[index : index + 10] for index in range(0, len(ordered), 10)
        ] or [[]]
        pages: List[discord.Embed] = []
        medals = {1: "🥇", 2: "🥈", 3: "🥉"}
        for page_index, chunk in enumerate(chunks):
            lines = []
            for offset, player in enumerate(chunk):
                rank = page_index * 10 + offset + 1
                badge = medals.get(rank, f"`{rank:>2}.`")
                lines.append(
                    f"{badge} **{player.name}** — level {player.level} · "
                    f"items {player.item_sum()} · {percent(player.progress(conf))}"
                )
            embed = discord.Embed(
                title="🏆 Leaderboard",
                description="\n".join(lines) or "*Nobody in the realm yet.*",
                colour=discord.Colour(PANEL_COLOR),
            )
            embed.set_footer(
                text=f"Page {page_index + 1}/{len(chunks)} · {len(ordered)} characters"
            )
            pages.append(embed)
        return pages

    # ------------------------------------------------------------------
    # Registration
    # ------------------------------------------------------------------

    async def _register(
        self, guild: discord.Guild, user_id: int, name: str, char_class: str
    ) -> Tuple[Optional[Player], Optional[str]]:
        name = (name or "").strip()
        char_class = (char_class or "").strip() or "Adventurer"
        if not NAME_RE.match(name):
            return (
                None,
                "Name must be 3–24 characters (letters, digits, spaces, `-`).",
            )
        if name.casefold() == "shadow of the realm":
            return None, "That name is reserved."
        if len(char_class) > 30:
            return None, "Class must be at most 30 characters."

        players = await self._players(guild)
        if user_id in players:
            return None, f"You already have a character: **{players[user_id].name}**."
        if any(
            player.name.casefold() == name.casefold() for player in players.values()
        ):
            return None, f"The name **{name}** is taken."

        conf = await self._settings(guild)
        player = Player(
            name=name,
            cls=char_class,
            created=time.time(),
            last_seen=time.time(),
        )
        player.ttl = next_level_time(1, conf)
        players[user_id] = player
        self._dirty.add(guild.id)
        await self._save(guild.id)

        role_id = await self.config.guild(guild).role()
        if role_id:
            role = guild.get_role(role_id)
            member = guild.get_member(user_id)
            if role is not None and member is not None:
                with contextlib.suppress(discord.HTTPException, discord.Forbidden):
                    await member.add_roles(role, reason="IdleRPG: character registered")

        return player, None

    async def _join_from_interaction(
        self, interaction: discord.Interaction, name: str, char_class: Optional[str]
    ) -> None:
        if interaction.guild is None:
            await interaction.response.send_message(
                "The realm only exists on a server.", ephemeral=True
            )
            return
        player, error = await self._register(
            interaction.guild, interaction.user.id, name, char_class or ""
        )
        if player is None:
            await interaction.response.send_message(f"❌ {error}", ephemeral=True)
            return
        conf = await self._settings(interaction.guild)
        await interaction.response.send_message(
            embed=self._char_embed(player, conf),
            content=(
                f"Welcome to the realm, **{player.name}**! Your clock is running — "
                "staying quiet gets you further."
            ),
            ephemeral=True,
        )

    # ------------------------------------------------------------------
    # Commands
    # ------------------------------------------------------------------

    async def _gate(self, ctx: commands.Context) -> bool:
        if ctx.guild is None:
            await ctx.send("IdleRPG only runs on a server.")
            return False
        if not await self.config.guild(ctx.guild).enabled():
            await ctx.send(
                "IdleRPG is not enabled here. An admin can turn it on with "
                f"`{ctx.clean_prefix}irpg set toggle`."
            )
            return False
        return True

    @commands.group(name="idlerpg", aliases=["irpg"], invoke_without_command=True)
    @commands.guild_only()
    async def idlerpg(self, ctx: commands.Context) -> None:
        """Idle RPG: register a character, idle, level up."""
        await ctx.send_help(ctx.command)

    @idlerpg.command(name="register", aliases=["join", "start"])
    @commands.guild_only()
    async def register(
        self, ctx: commands.Context, name: str, *, char_class: str = ""
    ) -> None:
        """Create a character: `[p]irpg register <name> [class]`."""
        if not await self._gate(ctx):
            return
        player, error = await self._register(ctx.guild, ctx.author.id, name, char_class)
        if player is None:
            await ctx.send(f"❌ {error}")
            return
        conf = await self._settings(ctx.guild)
        await ctx.send(
            content=f"⚔️ **{player.name}** enters the realm.",
            embed=self._char_embed(player, conf, await self._colour(ctx)),
        )

    @idlerpg.command(name="rename")
    @commands.guild_only()
    async def rename(self, ctx: commands.Context, name: str) -> None:
        """Rename your character."""
        if not await self._gate(ctx):
            return
        players = await self._players(ctx.guild)
        player = players.get(ctx.author.id)
        if player is None:
            await ctx.send(
                "You don't have a character yet — `[p]irpg register <name>`."
            )
            return
        name = name.strip()
        if not NAME_RE.match(name):
            await ctx.send("Name must be 3–24 characters.")
            return
        if any(
            other.name.casefold() == name.casefold() and other is not player
            for other in players.values()
        ):
            await ctx.send(f"The name **{name}** is taken.")
            return
        old = player.name
        player.name = name
        self._dirty.add(ctx.guild.id)
        await self._save(ctx.guild.id)
        await ctx.send(f"**{old}** is now **{name}**.")

    @idlerpg.command(name="unregister", aliases=["leave", "quit"])
    @commands.guild_only()
    async def unregister(self, ctx: commands.Context) -> None:
        """Delete your character (asks first)."""
        if not await self._gate(ctx):
            return
        players = await self._players(ctx.guild)
        player = players.get(ctx.author.id)
        if player is None:
            await ctx.send("You don't have a character.")
            return

        view = ConfirmView(ctx.author, disable_buttons=True)
        view.message = await ctx.send(
            f"Delete **{player.name}** (level {player.level})? All progress is lost.",
            view=view,
        )
        await view.wait()
        if not view.result:
            await ctx.send("Nothing changed.")
            return

        players.pop(ctx.author.id, None)
        self._dirty.add(ctx.guild.id)
        await self._save(ctx.guild.id)
        role_id = await self.config.guild(ctx.guild).role()
        role = ctx.guild.get_role(role_id) if role_id else None
        if role is not None and isinstance(ctx.author, discord.Member):
            with contextlib.suppress(discord.HTTPException, discord.Forbidden):
                await ctx.author.remove_roles(role, reason="IdleRPG: character deleted")
        await ctx.send(f"**{player.name}** leaves the realm.")

    @idlerpg.command(name="align")
    @commands.guild_only()
    async def align(self, ctx: commands.Context, alignment: str) -> None:
        """Choose an alignment: `good`, `neutral` or `evil`."""
        if not await self._gate(ctx):
            return
        players = await self._players(ctx.guild)
        player = players.get(ctx.author.id)
        if player is None:
            await ctx.send("You don't have a character yet.")
            return
        choice = ALIGN_ALIASES.get(alignment.casefold())
        if choice is None:
            await ctx.send("Pick `good`, `neutral` or `evil`.")
            return
        if choice == player.align:
            await ctx.send(f"You are already {ALIGN_LABELS[choice]}.")
            return
        player.align = choice
        self._dirty.add(ctx.guild.id)
        await self._save(ctx.guild.id)
        hints = {
            "good": "Battles +10% item sum, but crits are rare.",
            "neutral": "No bonuses, no penalties.",
            "evil": "Battles −10% item sum, but crits at 1/20.",
        }
        await ctx.send(
            f"{ALIGN_ICONS[choice]} **{player.name}** is now {ALIGN_LABELS[choice]}. "
            f"{hints[choice]}"
        )

    @idlerpg.command(name="char", aliases=["me", "whoami", "sheet"])
    @commands.guild_only()
    async def char(self, ctx: commands.Context) -> None:
        """Show your character."""
        if not await self._gate(ctx):
            return
        players = await self._players(ctx.guild)
        player = players.get(ctx.author.id)
        if player is None:
            await ctx.send(
                f"You don't have a character yet — "
                f"`{ctx.clean_prefix}irpg register <name>`."
            )
            return
        conf = await self._settings(ctx.guild)
        await ctx.send(embed=self._char_embed(player, conf, await self._colour(ctx)))

    @idlerpg.command(name="top", aliases=["leaderboard", "rank"])
    @commands.guild_only()
    async def top(self, ctx: commands.Context, page: int = 1) -> None:
        """Show the leaderboard."""
        if not await self._gate(ctx):
            return
        conf = await self._settings(ctx.guild)
        pages = await self._rank_pages(ctx.guild, conf)
        index = max(0, min(len(pages) - 1, page - 1))
        view = RankView(ctx.author.id, pages, index)
        view.message = await ctx.send(embed=pages[index], view=view)

    @idlerpg.command(name="realm", aliases=["status"])
    @commands.guild_only()
    async def realm(self, ctx: commands.Context) -> None:
        """Realm overview."""
        if not await self._gate(ctx):
            return
        conf = await self._settings(ctx.guild)
        players = await self._players(ctx.guild)
        if ctx.guild.id not in self._quest_loaded:
            self.quests[ctx.guild.id] = await self.config.guild(ctx.guild).quest() or {}
            self._quest_loaded.add(ctx.guild.id)
        quest = self.quests.get(ctx.guild.id) or {}
        await ctx.send(
            embed=self._realm_embed(
                ctx.guild, players, conf, quest, await self._colour(ctx)
            )
        )

    @idlerpg.command(name="quest")
    @commands.guild_only()
    async def quest(self, ctx: commands.Context) -> None:
        """Show the active quest."""
        if not await self._gate(ctx):
            return
        conf = await self._settings(ctx.guild)
        players = await self._players(ctx.guild)
        if ctx.guild.id not in self._quest_loaded:
            self.quests[ctx.guild.id] = await self.config.guild(ctx.guild).quest() or {}
            self._quest_loaded.add(ctx.guild.id)
        quest = self.quests.get(ctx.guild.id) or {}

        if not quest:
            eligible = sum(
                1
                for player in players.values()
                if player.level >= int(conf.get("quest_min_level", 20))
                and player.idle >= float(conf.get("quest_min_idle_hours", 6)) * 3600
            )
            await ctx.send(
                "Nobody is on a quest right now. One starts automatically once "
                f"**{conf.get('quest_size', 3)}** characters at level "
                f"**{conf.get('quest_min_level', 20)}** have idled for "
                f"**{conf.get('quest_min_idle_hours', 6)}h** "
                f"(currently {eligible} ready)."
            )
            return

        members = [
            players[int(member)].name
            for member in quest.get("members", [])
            if int(member) in players
        ]
        left = max(0.0, float(quest.get("end", 0)) - time.time())
        await ctx.send(
            f"🗺️ **{quest.get('name', '?')}**\n"
            f"Members: {humanize_list([f'**{name}**' for name in members]) or '—'}\n"
            f"{fmt_duration(left)} left. Anyone who takes a penalty in that time "
            "fails the whole quest."
        )

    @idlerpg.command(name="panel")
    @admin_command
    async def panel(
        self, ctx: commands.Context, channel: Optional[discord.TextChannel] = None
    ) -> None:
        """Post the panel with buttons (admin)."""
        channel = channel or ctx.channel
        conf = await self._settings(ctx.guild)
        players = await self._players(ctx.guild)
        if ctx.guild.id not in self._quest_loaded:
            self.quests[ctx.guild.id] = await self.config.guild(ctx.guild).quest() or {}
            self._quest_loaded.add(ctx.guild.id)
        embed = self._realm_embed(
            ctx.guild,
            players,
            conf,
            self.quests.get(ctx.guild.id) or {},
            await self._colour(ctx),
        )
        embed.title = "⚔️ IdleRPG"
        embed.description = (
            "Join, lean back and let your character level.\n"
            "Every message costs time — battles decide the items."
        )
        message = await channel.send(embed=embed, view=RealmPanel(self))
        await self.config.guild(ctx.guild).panel.set(
            {"channel": channel.id, "message": message.id}
        )
        if channel != ctx.channel:
            await ctx.send(f"Panel posted in {channel.mention}.")

    # -- Settings --------------------------------------------------

    @idlerpg.group(name="set", aliases=["settings"], invoke_without_command=True)
    @admin_command
    async def set_(self, ctx: commands.Context) -> None:
        """Settings for this server (admin)."""
        conf = await self._settings(ctx.guild)
        await ctx.send(
            f"**Pace** `{conf['speed']:g}` · "
            f"**Base time** `{fmt_duration(conf['base_time'])}` "
            f"· **Growth** `{conf['growth']:g}` · **Max level** `{conf['max_level']}`\n"
            f"**Battles** `{conf['battles']}` · **Shadow** `{conf['npc']}` · "
            f"**Events** `{conf['events']}` · **Quests** `{conf['quests']}`\n"
            f"**Penalty/char** `{conf['penalty']:g}s` "
            f"(max {conf['penalty_cap']} chars) · "
            f"**Announcements** `{conf['announce']}`\n"
            f"More with `{ctx.clean_prefix}irpg set <name> <value>`."
        )

    async def _store(self, ctx: commands.Context, key: str, value: Any) -> None:
        async with self.config.guild(ctx.guild).settings() as settings:
            settings[key] = value
        self._conf.pop(ctx.guild.id, None)
        await ctx.send(f"✅ **{key}** = `{value}`")

    @set_.command(name="toggle", aliases=["on", "off"])
    @admin_command
    async def set_toggle(self, ctx: commands.Context) -> None:
        """Enable or disable IdleRPG on this server."""
        current = await self.config.guild(ctx.guild).enabled()
        await self.config.guild(ctx.guild).enabled.set(not current)
        self._conf.pop(ctx.guild.id, None)
        state = "on" if not current else "off"
        await ctx.send(f"IdleRPG is now **{state}**.")
        if not current:
            await ctx.send(
                f"Create characters with `{ctx.clean_prefix}irpg register <name>`."
            )

    @set_.command(name="channel")
    @admin_command
    async def set_channel(
        self, ctx: commands.Context, channel: Optional[discord.TextChannel] = None
    ) -> None:
        """Set the announcement channel (no channel: off)."""
        await self.config.guild(ctx.guild).channel.set(channel.id if channel else None)
        await ctx.send(
            f"Announcements now go to {channel.mention}."
            if channel
            else "Announcements off."
        )

    @set_.command(name="role")
    @admin_command
    async def set_role(self, ctx: commands.Context, role: discord.Role = None) -> None:
        """Role for registered characters (no role: none)."""
        await self.config.guild(ctx.guild).role.set(role.id if role else None)
        await ctx.send(
            f"Registered characters now get {role.mention}."
            if role
            else "Role assignment off."
        )

    @set_.command(name="speed")
    @admin_command
    async def set_speed(self, ctx: commands.Context, factor: float) -> None:
        """Pace: 2 = twice as fast, 0.5 = half as fast."""
        if not 0.05 <= factor <= 100:
            await ctx.send("Pick a factor between 0.05 and 100.")
            return
        await self._store(ctx, "speed", round(factor, 3))

    @set_.command(name="base")
    @admin_command
    async def set_base(self, ctx: commands.Context, seconds: int) -> None:
        """Base time for level 1 in seconds (default 300)."""
        if not 5 <= seconds <= 86400:
            await ctx.send("Pick between 5 and 86400 seconds.")
            return
        await self._store(ctx, "base_time", float(seconds))

    @set_.command(name="growth")
    @admin_command
    async def set_growth(self, ctx: commands.Context, factor: float) -> None:
        """Growth per level (default 1.14)."""
        if not 1.01 <= factor <= 3.0:
            await ctx.send("Pick a factor between 1.01 and 3.0.")
            return
        await self._store(ctx, "growth", round(factor, 4))

    @set_.command(name="maxlevel")
    @admin_command
    async def set_maxlevel(self, ctx: commands.Context, level_value: int) -> None:
        """Highest reachable level."""
        if not 2 <= level_value <= 1000:
            await ctx.send("Pick between 2 and 1000.")
            return
        await self._store(ctx, "max_level", level_value)

    @set_.command(name="battles")
    @admin_command
    async def set_battles(self, ctx: commands.Context) -> None:
        """Enable or disable battles."""
        conf = await self._settings(ctx.guild)
        await self._store(ctx, "battles", not conf.get("battles", True))

    @set_.command(name="npc")
    @admin_command
    async def set_npc(self, ctx: commands.Context) -> None:
        """Fight the Shadow of the Realm when nobody else is around."""
        conf = await self._settings(ctx.guild)
        await self._store(ctx, "npc", not conf.get("npc", True))

    @set_.command(name="events")
    @admin_command
    async def set_events(self, ctx: commands.Context) -> None:
        """Fate events (Hand of God, godsends, calamities) on or off."""
        conf = await self._settings(ctx.guild)
        await self._store(ctx, "events", not conf.get("events", True))

    @set_.command(name="quests")
    @admin_command
    async def set_quests(self, ctx: commands.Context) -> None:
        """Quests on or off."""
        conf = await self._settings(ctx.guild)
        await self._store(ctx, "quests", not conf.get("quests", True))

    @set_.command(name="uniques")
    @admin_command
    async def set_uniques(self, ctx: commands.Context) -> None:
        """Unique items on or off."""
        conf = await self._settings(ctx.guild)
        await self._store(ctx, "uniques", not conf.get("uniques", True))

    @set_.command(name="penalty")
    @admin_command
    async def set_penalty(self, ctx: commands.Context, seconds: float) -> None:
        """Penalty seconds per message character (0 = talking is free)."""
        if not 0 <= seconds <= 60:
            await ctx.send("Pick between 0 and 60 seconds.")
            return
        await self._store(ctx, "penalty", round(seconds, 3))

    @set_.command(name="penaltycap")
    @admin_command
    async def set_penaltycap(self, ctx: commands.Context, chars: int) -> None:
        """How many characters of a message are counted at most."""
        if not 0 <= chars <= 2000:
            await ctx.send("Pick between 0 and 2000 characters.")
            return
        await self._store(ctx, "penalty_cap", chars)

    @set_.command(name="announce")
    @admin_command
    async def set_announce(self, ctx: commands.Context) -> None:
        """Announcements in the channel on or off."""
        conf = await self._settings(ctx.guild)
        await self._store(ctx, "announce", not conf.get("announce", True))

    @set_.command(name="reset")
    @admin_command
    async def set_reset(self, ctx: commands.Context) -> None:
        """Reset everything to defaults and delete all characters."""
        view = ConfirmView(ctx.author, disable_buttons=True)
        view.message = await ctx.send(
            "This deletes **all characters** and resets every setting. Are you sure?",
            view=view,
        )
        await view.wait()
        if not view.result:
            await ctx.send("Nothing changed.")
            return
        await self.config.guild(ctx.guild).clear()
        self.players.pop(ctx.guild.id, None)
        self.quests.pop(ctx.guild.id, None)
        self._quest_loaded.discard(ctx.guild.id)
        self._conf.pop(ctx.guild.id, None)
        self._dirty.discard(ctx.guild.id)
        await ctx.send("The realm has been reset.")

    # -- Administration -----------------------------------------------------

    @idlerpg.group(name="admin", aliases=["manage"], invoke_without_command=True)
    @admin_command
    async def admin(self, ctx: commands.Context) -> None:
        """Manage characters (admin)."""
        await ctx.send_help(ctx.command)

    async def _target(
        self, ctx: commands.Context, member: discord.Member
    ) -> Optional[Player]:
        players = await self._players(ctx.guild)
        player = players.get(member.id)
        if player is None:
            await ctx.send(f"{member.display_name} has no character.")
            return None
        return player

    @admin.command(name="hog", aliases=["handofgod"])
    @admin_command
    async def admin_hog(
        self, ctx: commands.Context, member: Optional[discord.Member] = None
    ) -> None:
        """Summon the Hand of God — for one character or the whole realm."""
        conf = await self._settings(ctx.guild)
        players = await self._players(ctx.guild)
        targets = (
            [await self._target(ctx, member)] if member else list(players.values())
        )
        targets = [player for player in targets if player is not None]
        if not targets:
            await ctx.send("No characters in the realm.")
            return

        lines = []
        for player in targets:
            shift = self._rng.uniform(0.05, 0.75)
            if self._rng.random() < 0.8:
                player.ttl -= player.next_time(conf) * shift
                lines.append(f"✨ **{player.name}** {shift * 100:.0f}% faster")
            else:
                player.ttl += player.next_time(conf) * shift
                lines.append(f"⚡ **{player.name}** {shift * 100:.0f}% slower")
        self._dirty.add(ctx.guild.id)
        await ctx.send("The Hand of God descends:\n" + "\n".join(lines[:15]))

    @admin.command(name="level")
    @admin_command
    async def admin_level(
        self, ctx: commands.Context, member: discord.Member, level_value: int
    ) -> None:
        """Set a character's level."""
        if not 1 <= level_value <= 1000:
            await ctx.send("Pick between 1 and 1000.")
            return
        player = await self._target(ctx, member)
        if player is None:
            return
        conf = await self._settings(ctx.guild)
        player.level = level_value
        player.ttl = next_level_time(level_value, conf)
        self._dirty.add(ctx.guild.id)
        await self._save(ctx.guild.id)
        await ctx.send(f"**{player.name}** is now level **{level_value}**.")

    @admin.command(name="ttl")
    @admin_command
    async def admin_ttl(
        self, ctx: commands.Context, member: discord.Member, seconds: int
    ) -> None:
        """Change the time to the next level (negative = faster)."""
        player = await self._target(ctx, member)
        if player is None:
            return
        player.ttl = max(0.0, player.ttl + seconds)
        self._dirty.add(ctx.guild.id)
        await self._save(ctx.guild.id)
        await ctx.send(
            f"**{player.name}** needs {fmt_duration(player.ttl)} more to reach "
            f"level {player.level + 1}."
        )

    @admin.command(name="remove", aliases=["delete"])
    @admin_command
    async def admin_remove(self, ctx: commands.Context, member: discord.Member) -> None:
        """Delete a member's character."""
        player = await self._target(ctx, member)
        if player is None:
            return
        players = await self._players(ctx.guild)
        players.pop(member.id, None)
        self._dirty.add(ctx.guild.id)
        await self._save(ctx.guild.id)
        await ctx.send(f"**{player.name}** was removed from the realm.")


# ----------------------------------------------------------------------
# Persistent views
# ----------------------------------------------------------------------


class JoinModal(discord.ui.Modal, title="Join the realm"):
    name_input: discord.ui.TextInput = discord.ui.TextInput(
        label="Character name",
        placeholder="e.g. Grimmwald",
        min_length=3,
        max_length=24,
    )
    class_input: discord.ui.TextInput = discord.ui.TextInput(
        label="Class (free choice)",
        placeholder="e.g. Bard",
        required=False,
        max_length=30,
        default="Adventurer",
    )

    def __init__(self, cog: IdleRPG) -> None:
        super().__init__()
        self.cog = cog

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await self.cog._join_from_interaction(
            interaction, self.name_input.value, self.class_input.value
        )


class RealmPanel(discord.ui.View):
    """Persistent panel: join, character, leaderboard, realm."""

    def __init__(self, cog: IdleRPG) -> None:
        super().__init__(timeout=None)
        self.cog = cog

    async def _player(self, interaction: discord.Interaction):
        players = await self.cog._players(interaction.guild)
        return players.get(interaction.user.id)

    @discord.ui.button(
        label="Join",
        emoji="⚔️",
        style=discord.ButtonStyle.success,
        custom_id="idlerpg:join",
        row=0,
    )
    async def join(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        await interaction.response.send_modal(JoinModal(self.cog))

    @discord.ui.button(
        label="Character",
        emoji="🧙",
        style=discord.ButtonStyle.primary,
        custom_id="idlerpg:char",
        row=0,
    )
    async def character(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        player = await self._player(interaction)
        if player is None:
            await interaction.response.send_message(
                "You don't have a character yet — press **Join**.", ephemeral=True
            )
            return
        conf = await self.cog._settings(interaction.guild)
        await interaction.response.send_message(
            embed=self.cog._char_embed(player, conf), ephemeral=True
        )

    @discord.ui.button(
        label="Leaderboard",
        emoji="🏆",
        style=discord.ButtonStyle.secondary,
        custom_id="idlerpg:top",
        row=0,
    )
    async def ranking(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        conf = await self.cog._settings(interaction.guild)
        pages = await self.cog._rank_pages(interaction.guild, conf)
        view = RankView(interaction.user.id, pages, 0)
        await interaction.response.send_message(
            embed=pages[0], view=view, ephemeral=True
        )

    @discord.ui.button(
        label="Realm",
        emoji="🗺️",
        style=discord.ButtonStyle.secondary,
        custom_id="idlerpg:realm",
        row=0,
    )
    async def realm(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        conf = await self.cog._settings(interaction.guild)
        players = await self.cog._players(interaction.guild)
        if interaction.guild.id not in self.cog._quest_loaded:
            self.cog.quests[interaction.guild.id] = (
                await self.cog.config.guild(interaction.guild).quest() or {}
            )
            self.cog._quest_loaded.add(interaction.guild.id)
        quest = self.cog.quests.get(interaction.guild.id) or {}
        await interaction.response.send_message(
            embed=self.cog._realm_embed(interaction.guild, players, conf, quest),
            ephemeral=True,
        )


class RankView(discord.ui.View):
    """Page through the leaderboard — only for the caller."""

    def __init__(
        self, author_id: int, pages: List[discord.Embed], index: int = 0
    ) -> None:
        super().__init__(timeout=180)
        self.author_id = author_id
        self.pages = pages
        self.index = max(0, min(len(pages) - 1, index))
        self.message: Optional[discord.Message] = None
        self._sync()

    def _sync(self) -> None:
        self.prev.disabled = self.index <= 0
        self.next.disabled = self.index >= len(self.pages) - 1

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.author_id:
            await interaction.response.send_message(
                "This is not your leaderboard.", ephemeral=True
            )
            return False
        return True

    async def _edit(self, interaction: discord.Interaction) -> None:
        self._sync()
        await interaction.response.edit_message(embed=self.pages[self.index], view=self)

    @discord.ui.button(label="◀", style=discord.ButtonStyle.secondary)
    async def prev(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        self.index = max(0, self.index - 1)
        await self._edit(interaction)

    @discord.ui.button(label="▶", style=discord.ButtonStyle.secondary)
    async def next(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        self.index = min(len(self.pages) - 1, self.index + 1)
        await self._edit(interaction)
