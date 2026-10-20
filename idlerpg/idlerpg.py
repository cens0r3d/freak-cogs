"""IdleRPG für Discord — registrieren, idlen, leveln.

Das Cog überträgt die Idee des IRC-Klassikers idlerpg.net auf Discord: Wer
eine Figur registriert hat, levelt allein durchs Herumsitzen, findet bei jedem
Aufstieg einen Gegenstand und kämpft gegen andere Anwesende. Statt eines
Passworts zählt der Discord-Account, statt eines IRC-Kanals die Gilde — jedes
Server-Reich ist eine eigene Welt.

Alle Regeln sind pro Server einstellbar (`[p]irpg set …`), die Spiellogik
selbst liegt ohne Framework-Abhängigkeiten in ``game.py``.

Betriebsdetails, die man beim Ändern kennen sollte:

* Der Tick läuft alle 15 Sekunden und arbeitet nur auf dem In-Memory-Cache
  (``self.players``). Geschrieben wird gebündelt alle 60 Sekunden und beim
  Entladen — ein Tick pro Figur und Sekunde wäre sonst ein Config-Schreibzugriff
  pro Sekunde.
* "Online" heißt: die Figur gehört zur Gilde und der Member steckt im Cache.
  Wer den Server verlässt, sammelt keine Zeit mehr, behält aber seine Figur.
* Strafen kosten Zeit: eine Nachricht kostet ``penalty`` Sekunden pro Zeichen,
  multipliziert mit ``1.14 ** Stufe``. Genau wie im Original ist Schweigen die
  beste Strategie.
* Ein Gilden-Check gehört an jeden Subcommand einzeln — ``set`` und ``admin``
  sind Gruppen, deren Checks nicht an die Subcommands vererbt werden.
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
    # Zeit bis zur nächsten Stufe: base * growth ** level, ab cap_level linear.
    "base_time": 300.0,
    "growth": 1.14,
    "cap_level": 50,
    "cap_extra": 43200.0,
    "speed": 1.0,
    "max_level": 120,
    # Gegenstände
    "item_mult": 1.5,
    "uniques": True,
    # Kämpfe
    "battles": True,
    "battle_low": 25,
    "npc": True,
    # Schicksal (Wahrscheinlichkeit pro Tag)
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
    # Strafen
    "penalty": 1.0,
    "penalty_cap": 60,
    "penalty_leave": 0.0,
    # Ausgabe
    "announce": True,
}


def admin_command(func):
    """Gilden- und Verwaltungs-Check für einen einzelnen (Sub-)Command."""

    func = commands.admin_or_permissions(manage_guild=True)(func)
    func = commands.guild_only()(func)
    return func


class IdleRPG(commands.Cog):
    """Idle-Rollenspiel: Figur registrieren, idlen, leveln, kämpfen."""

    __version__ = "1.0.1"

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
    # Lebenszyklus
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
                log.exception("IdleRPG: Tick für Gilde %s fehlgeschlagen", guild.id)

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
    # Datenzugriff
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
                log.warning("IdleRPG: unlesbare Figur %s in Gilde %s", key, guild.id)
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
                log.exception(
                    "IdleRPG: Speichern für Gilde %s fehlgeschlagen", guild_id
                )

    async def _save_quest(self, guild_id: int, quest: Dict[str, Any]) -> None:
        self.quests[guild_id] = quest
        self._quest_loaded.add(guild_id)
        with contextlib.suppress(Exception):
            await self.config.guild_from_id(guild_id).quest.set(quest)

    # ------------------------------------------------------------------
    # Der Tick
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
        """Stufenaufstiege abarbeiten (mehrere, wenn viel Zeit aufgelaufen ist)."""
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
            label = SLOT_LABELS.get(item["slot"], "Gegenstand")
            marker = " ✨ **einzigartig**" if item.get("unique") else ""
            if previous is None:
                lines.append(
                    f"🎉 **{player.name}** erreicht **Stufe {player.level}** und findet "
                    f"{label}: **{item['name']}** (Stufe {item['level']}){marker}"
                )
            else:
                lines.append(
                    f"🎉 **{player.name}** erreicht **Stufe {player.level}** und tauscht "
                    f"{label} **{previous['name']}** ({previous['level']}) gegen "
                    f"**{item['name']}** (Stufe {item['level']}){marker}"
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
                f"⚔️ **{atk.name}** (Summe {int(effective_sum(atk))}) besiegt "
                f"**{defender.name}** ({int(effective_sum(defender))}) — "
                f"{result.percent * 100:.0f} % schneller Richtung Stufe {atk.level + 1}."
            )
            if result.crit:
                atk.crits += 1
                defender.ttl += result.def_delta
                lines.append(
                    f"💥 Kritischer Treffer! **{defender.name}** verliert zusätzlich "
                    f"{result.def_delta / max(1.0, defender.next_time(conf)) * 100:.0f} %."
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
                    f"🖐️ **{atk.name}** stiehlt **{defender.name}** "
                    f"{SLOT_LABELS.get(slot, 'Gegenstand')} **{result.steal_item['name']}** "
                    f"(Stufe {result.steal_item['level']})."
                )
        else:
            atk.losses += 1
            defender.wins += 1
            lines.append(
                f"💀 **{atk.name}** verliert gegen **{defender.name}** — "
                f"{result.percent * 100:.0f} % Rückstand auf Stufe {atk.level + 1}."
            )

    def _shadow(self, atk: Player, players: Dict[int, Player]) -> Player:
        """Ersatzgegner, wenn sonst niemand da ist (wie der IRPG-Bot)."""
        best = max((player.item_sum() for player in players.values()), default=0)
        shadow = Player(
            name="Schatten des Reiches", cls="Erscheinung", level=max(1, atk.level)
        )
        shadow.items = {
            "weapon": {
                "name": "Schattenklinge",
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
        """Hand Gottes, Geschenke, Unheil und Teamkämpfe."""
        need = player.next_time(conf)

        if self._rng.random() < chance_per_tick(float(conf.get("hog", 0.05)), dt):
            shift = self._rng.uniform(0.05, 0.75)
            if self._rng.random() < 0.8:
                player.ttl -= need * shift
                lines.append(
                    f"✨ Die Hand Gottes führt **{player.name}** — "
                    f"{shift * 100:.0f} % schneller."
                )
            else:
                player.ttl += need * shift
                lines.append(
                    f"⚡ Die Hand Gottes zürnt **{player.name}** — "
                    f"{shift * 100:.0f} % langsamer."
                )

        if self._rng.random() < chance_per_tick(float(conf.get("godsend", 0.125)), dt):
            shift = self._rng.uniform(0.05, 0.12)
            if not player.items or self._rng.random() < 0.5:
                player.ttl -= need * shift
                lines.append(
                    f"🌟 Ein Geschenk der Götter: **{player.name}** ist "
                    f"{shift * 100:.0f} % schneller."
                )
            else:
                slot = self._rng.choice(list(player.items))
                item = player.items[slot]
                item["level"] = max(1, int(round(int(item["level"]) * 1.10)))
                lines.append(
                    f"🌟 Ein Geschenk der Götter: **{item['name']}** von **{player.name}** "
                    f"wird auf Stufe {item['level']} aufgewertet."
                )

        if self._rng.random() < chance_per_tick(float(conf.get("calamity", 0.125)), dt):
            shift = self._rng.uniform(0.05, 0.12)
            if not player.items or self._rng.random() < 0.5:
                player.ttl += need * shift
                lines.append(
                    f"🌑 Ein Unglück trifft **{player.name}** — "
                    f"{shift * 100:.0f} % langsamer."
                )
            else:
                slot = self._rng.choice(list(player.items))
                item = player.items[slot]
                item["level"] = max(1, int(round(int(item["level"]) * 0.90)))
                lines.append(
                    f"🌑 Ein Unglück: **{item['name']}** von **{player.name}** "
                    f"verliert Stufe — jetzt {item['level']}."
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
                f"🛡️ Teamkampf: {names_win} besiegen {names_lose} — "
                f"{fmt_duration(gain)} gutgeschrieben bzw. abgezogen."
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
                    f"💀 Die Quest **{quest.get('name', '?')}** ist gescheitert — "
                    "das Reich bestraft alle Anwesenden."
                )
            else:
                for member in members:
                    member.ttl -= 0.25 * member.next_time(conf)
                    member.quest_sin = False
                names = humanize_list([f"**{member.name}**" for member in members])
                lines.append(
                    f"🏆 Die Quest **{quest.get('name', '?')}** ist erfüllt: {names} "
                    "sind 25 % schneller."
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
            f"🗺️ Neue Quest: **{quest['name']}** — {names} ziehen los. "
            f"{hours:.0f} Stunden, keine Strafe erlaubt."
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
            text += f"\n… und {len(lines) - 10} weitere Ereignisse."
        with contextlib.suppress(discord.HTTPException):
            await channel.send(
                text[:1900], allowed_mentions=discord.AllowedMentions.none()
            )

    # ------------------------------------------------------------------
    # Listener
    # ------------------------------------------------------------------

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        """Reden kostet Zeit — genau wie im Original."""
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
        """Beim Verlassen des Servers optional eine Strafe verbuchen."""
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
        """Löscht der Ansage-Kanal, wird die Referenz darauf entfernt."""
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
    # Darstellung
    # ------------------------------------------------------------------

    def _colour(self, ctx: Optional[commands.Context] = None) -> discord.Colour:
        if ctx is not None and getattr(ctx, "embed_color", None):
            return ctx.embed_color
        return discord.Colour(PANEL_COLOR)

    def _char_embed(
        self,
        player: Player,
        conf: Dict[str, Any],
        colour: Optional[discord.Colour] = None,
    ) -> discord.Embed:
        progress = player.progress(conf)
        max_level = int(conf.get("max_level", 120))
        if player.level >= max_level:
            remaining = "Maximalstufe erreicht"
        else:
            remaining = f"noch {fmt_duration(player.ttl)} bis Stufe {player.level + 1}"
        embed = discord.Embed(
            title=f"{ALIGN_ICONS.get(player.align, '⚖️')} {player.name}",
            description=(
                f"*{player.cls}* · {ALIGN_LABELS.get(player.align, 'neutral')} · "
                f"Stufe **{player.level}**"
            ),
            colour=colour or discord.Colour(PANEL_COLOR),
        )
        embed.add_field(
            name="Fortschritt",
            value=(f"`{bar(progress)}` {percent(progress)}\n{remaining}"),
            inline=False,
        )
        embed.add_field(
            name="Gegenstandssumme", value=f"**{player.item_sum()}**", inline=True
        )
        embed.add_field(
            name="Kämpfe", value=f"{player.wins} S / {player.losses} N", inline=True
        )
        embed.add_field(
            name="Bilanz",
            value=(
                f"{player.levels_gained} Aufstiege · {player.crits} Krits · "
                f"{player.steals} Diebstähle · {player.penalties} Strafen\n"
                f"geidlet: {fmt_duration(player.idle)}"
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
            name="Ausrüstung", value="\n".join(equipment)[:1024], inline=False
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
                f"`{index}.` **{player.name}** — Stufe {player.level} · Items {player.item_sum()}"
                for index, player in enumerate(top, start=1)
            )
            or "*Noch niemand im Reich.*"
        )

        if quest:
            left = max(0.0, float(quest.get("end", 0)) - time.time())
            quest_text = f"**{quest.get('name', '?')}** · {fmt_duration(left)} übrig"
        else:
            quest_text = "*keine*"

        embed = discord.Embed(
            title="⚔️ Das Reich",
            description=(
                f"{len(players)} Figuren · {online} davon anwesend\n"
                f"Tempo: Faktor {conf.get('speed', 1.0):g} · "
                f"Stufe 1 in {fmt_duration(next_level_time(1, conf))}"
            ),
            colour=colour or discord.Colour(PANEL_COLOR),
        )
        embed.add_field(name="Spitze", value=best, inline=False)
        embed.add_field(name="Quest", value=quest_text, inline=False)
        embed.add_field(
            name="Regeln",
            value=(
                "Wer registriert ist, levelt durchs Herumsitzen. "
                "Jede Nachricht kostet Zeit, Kämpfe entscheiden die Gegenstände."
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
                    f"{badge} **{player.name}** — Stufe {player.level} · "
                    f"Items {player.item_sum()} · {percent(player.progress(conf))}"
                )
            embed = discord.Embed(
                title="🏆 Rangliste",
                description="\n".join(lines) or "*Noch niemand im Reich.*",
                colour=discord.Colour(PANEL_COLOR),
            )
            embed.set_footer(
                text=f"Seite {page_index + 1}/{len(chunks)} · {len(ordered)} Figuren"
            )
            pages.append(embed)
        return pages

    # ------------------------------------------------------------------
    # Registrierung
    # ------------------------------------------------------------------

    async def _register(
        self, guild: discord.Guild, user_id: int, name: str, klasse: str
    ) -> Tuple[Optional[Player], Optional[str]]:
        name = (name or "").strip()
        klasse = (klasse or "").strip() or "Abenteurer"
        if not NAME_RE.match(name):
            return (
                None,
                "Der Name muss 3–24 Zeichen haben (Buchstaben, Zahlen, Leerzeichen, `-`).",
            )
        if name.casefold() == "schatten des reiches":
            return None, "Dieser Name ist besetzt."
        if len(klasse) > 30:
            return None, "Die Klasse darf höchstens 30 Zeichen haben."

        players = await self._players(guild)
        if user_id in players:
            return None, f"Du hast schon eine Figur: **{players[user_id].name}**."
        if any(
            player.name.casefold() == name.casefold() for player in players.values()
        ):
            return None, f"Der Name **{name}** ist schon vergeben."

        conf = await self._settings(guild)
        player = Player(
            name=name,
            cls=klasse,
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
                    await member.add_roles(role, reason="IdleRPG: Figur registriert")

        return player, None

    async def _join_from_interaction(
        self, interaction: discord.Interaction, name: str, klasse: Optional[str]
    ) -> None:
        if interaction.guild is None:
            await interaction.response.send_message(
                "Das Reich gibt es nur auf einem Server.", ephemeral=True
            )
            return
        player, error = await self._register(
            interaction.guild, interaction.user.id, name, klasse or ""
        )
        if player is None:
            await interaction.response.send_message(f"❌ {error}", ephemeral=True)
            return
        conf = await self._settings(interaction.guild)
        await interaction.response.send_message(
            embed=self._char_embed(player, conf),
            content=(
                f"Willkommen im Reich, **{player.name}**! Deine Zeit läuft ab jetzt — "
                "Schweigen bringt dich schneller voran."
            ),
            ephemeral=True,
        )

    # ------------------------------------------------------------------
    # Commands
    # ------------------------------------------------------------------

    async def _gate(self, ctx: commands.Context) -> bool:
        if ctx.guild is None:
            await ctx.send("IdleRPG läuft nur auf einem Server.")
            return False
        if not await self.config.guild(ctx.guild).enabled():
            await ctx.send(
                f"IdleRPG ist hier noch aus. Ein Admin kann es mit "
                f"`{ctx.clean_prefix}irpg set toggle` einschalten."
            )
            return False
        return True

    @commands.group(name="idlerpg", aliases=["irpg"], invoke_without_command=True)
    @commands.guild_only()
    async def idlerpg(self, ctx: commands.Context) -> None:
        """Idle-Rollenspiel: registrieren, idlen, leveln."""
        await ctx.send_help(ctx.command)

    @idlerpg.command(name="register", aliases=["join", "beitreten", "start"])
    @commands.guild_only()
    async def register(
        self, ctx: commands.Context, name: str, *, klasse: str = ""
    ) -> None:
        """Figur anlegen: `[p]irpg register <Name> [Klasse]`."""
        if not await self._gate(ctx):
            return
        player, error = await self._register(ctx.guild, ctx.author.id, name, klasse)
        if player is None:
            await ctx.send(f"❌ {error}")
            return
        conf = await self._settings(ctx.guild)
        await ctx.send(
            content=f"⚔️ **{player.name}** betritt das Reich.",
            embed=self._char_embed(player, conf, self._colour(ctx)),
        )

    @idlerpg.command(name="rename", aliases=["umbenennen"])
    @commands.guild_only()
    async def rename(self, ctx: commands.Context, name: str) -> None:
        """Figur umbenennen."""
        if not await self._gate(ctx):
            return
        players = await self._players(ctx.guild)
        player = players.get(ctx.author.id)
        if player is None:
            await ctx.send("Du hast noch keine Figur — `[p]irpg register <Name>`.")
            return
        name = name.strip()
        if not NAME_RE.match(name):
            await ctx.send("Der Name muss 3–24 Zeichen haben.")
            return
        if any(
            other.name.casefold() == name.casefold() and other is not player
            for other in players.values()
        ):
            await ctx.send(f"Der Name **{name}** ist schon vergeben.")
            return
        old = player.name
        player.name = name
        self._dirty.add(ctx.guild.id)
        await self._save(ctx.guild.id)
        await ctx.send(f"**{old}** heißt jetzt **{name}**.")

    @idlerpg.command(name="unregister", aliases=["leave", "verlassen", "austreten"])
    @commands.guild_only()
    async def unregister(self, ctx: commands.Context) -> None:
        """Figur löschen (mit Rückfrage)."""
        if not await self._gate(ctx):
            return
        players = await self._players(ctx.guild)
        player = players.get(ctx.author.id)
        if player is None:
            await ctx.send("Du hast keine Figur.")
            return

        view = ConfirmView(ctx.author, disable_buttons=True)
        view.message = await ctx.send(
            f"Figur **{player.name}** (Stufe {player.level}) wirklich löschen? "
            "Der Fortschritt ist danach weg.",
            view=view,
        )
        await view.wait()
        if not view.result:
            await ctx.send("Alles bleibt, wie es ist.")
            return

        players.pop(ctx.author.id, None)
        self._dirty.add(ctx.guild.id)
        await self._save(ctx.guild.id)
        role_id = await self.config.guild(ctx.guild).role()
        role = ctx.guild.get_role(role_id) if role_id else None
        if role is not None and isinstance(ctx.author, discord.Member):
            with contextlib.suppress(discord.HTTPException, discord.Forbidden):
                await ctx.author.remove_roles(role, reason="IdleRPG: Figur gelöscht")
        await ctx.send(f"**{player.name}** verlässt das Reich.")

    @idlerpg.command(name="align", aliases=["gesinnung"])
    @commands.guild_only()
    async def align(self, ctx: commands.Context, gesinnung: str) -> None:
        """Gesinnung wählen: `gut`, `neutral` oder `böse`."""
        if not await self._gate(ctx):
            return
        players = await self._players(ctx.guild)
        player = players.get(ctx.author.id)
        if player is None:
            await ctx.send("Du hast noch keine Figur.")
            return
        choice = ALIGN_ALIASES.get(gesinnung.casefold())
        if choice is None:
            await ctx.send("Bitte `gut`, `neutral` oder `böse` angeben.")
            return
        if choice == player.align:
            await ctx.send(f"Du bist schon {ALIGN_LABELS[choice]}.")
            return
        player.align = choice
        self._dirty.add(ctx.guild.id)
        await self._save(ctx.guild.id)
        hints = {
            "good": "Kämpfe +10 % Gegenstandssumme, dafür seltener Krits.",
            "neutral": "Keine Boni, keine Mali.",
            "evil": "Kämpfe −10 % Gegenstandssumme, dafür Krits 1/20.",
        }
        await ctx.send(
            f"{ALIGN_ICONS[choice]} **{player.name}** ist jetzt {ALIGN_LABELS[choice]}. "
            f"{hints[choice]}"
        )

    @idlerpg.command(name="char", aliases=["me", "whoami", "figur"])
    @commands.guild_only()
    async def char(self, ctx: commands.Context) -> None:
        """Eigene Figur anzeigen."""
        if not await self._gate(ctx):
            return
        players = await self._players(ctx.guild)
        player = players.get(ctx.author.id)
        if player is None:
            await ctx.send(
                f"Du hast noch keine Figur — `{ctx.clean_prefix}irpg register <Name>`."
            )
            return
        conf = await self._settings(ctx.guild)
        await ctx.send(embed=self._char_embed(player, conf, self._colour(ctx)))

    @idlerpg.command(name="top", aliases=["rangliste", "leaderboard", "rang"])
    @commands.guild_only()
    async def top(self, ctx: commands.Context, seite: int = 1) -> None:
        """Rangliste anzeigen."""
        if not await self._gate(ctx):
            return
        conf = await self._settings(ctx.guild)
        pages = await self._rank_pages(ctx.guild, conf)
        index = max(0, min(len(pages) - 1, seite - 1))
        view = RankView(ctx.author.id, pages, index)
        view.message = await ctx.send(embed=pages[index], view=view)

    @idlerpg.command(name="realm", aliases=["reich", "status"])
    @commands.guild_only()
    async def realm(self, ctx: commands.Context) -> None:
        """Überblick über das Reich."""
        if not await self._gate(ctx):
            return
        conf = await self._settings(ctx.guild)
        players = await self._players(ctx.guild)
        if ctx.guild.id not in self._quest_loaded:
            self.quests[ctx.guild.id] = await self.config.guild(ctx.guild).quest() or {}
            self._quest_loaded.add(ctx.guild.id)
        quest = self.quests.get(ctx.guild.id) or {}
        await ctx.send(
            embed=self._realm_embed(ctx.guild, players, conf, quest, self._colour(ctx))
        )

    @idlerpg.command(name="quest", aliases=["abenteuer"])
    @commands.guild_only()
    async def quest(self, ctx: commands.Context) -> None:
        """Aktuelle Quest anzeigen."""
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
                "Gerade zieht niemand los. Eine Quest startet automatisch, sobald "
                f"**{conf.get('quest_size', 3)}** Figuren ab Stufe "
                f"**{conf.get('quest_min_level', 20)}** je "
                f"**{conf.get('quest_min_idle_hours', 6)} Std** geidlet haben "
                f"(aktuell {eligible} bereit)."
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
            f"Beteiligt: {humanize_list([f'**{name}**' for name in members]) or '—'}\n"
            f"Noch {fmt_duration(left)}. Wer in dieser Zeit eine Strafe kassiert, "
            "lässt die ganze Quest scheitern."
        )

    @idlerpg.command(name="panel", aliases=["tafel"])
    @admin_command
    async def panel(
        self, ctx: commands.Context, channel: Optional[discord.TextChannel] = None
    ) -> None:
        """Panel mit Buttons posten (Admin)."""
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
            self._colour(ctx),
        )
        embed.title = "⚔️ IdleRPG"
        embed.description = (
            "Tritt bei, lehn dich zurück und lass deine Figur leveln.\n"
            "Jede Nachricht kostet Zeit — Kämpfe entscheiden die Gegenstände."
        )
        message = await channel.send(embed=embed, view=RealmPanel(self))
        await self.config.guild(ctx.guild).panel.set(
            {"channel": channel.id, "message": message.id}
        )
        if channel != ctx.channel:
            await ctx.send(f"Panel steht in {channel.mention}.")

    # -- Einstellungen --------------------------------------------------

    @idlerpg.group(
        name="set", aliases=["settings", "einstellungen"], invoke_without_command=True
    )
    @admin_command
    async def set_(self, ctx: commands.Context) -> None:
        """Einstellungen dieses Servers (Admin)."""
        conf = await self._settings(ctx.guild)
        await ctx.send(
            f"**Tempo** `{conf['speed']:g}` · **Basiszeit** `{fmt_duration(conf['base_time'])}` "
            f"· **Wachstum** `{conf['growth']:g}` · **Max-Stufe** `{conf['max_level']}`\n"
            f"**Kämpfe** `{conf['battles']}` · **Schatten** `{conf['npc']}` · "
            f"**Ereignisse** `{conf['events']}` · **Quests** `{conf['quests']}`\n"
            f"**Strafe/Zeichen** `{conf['penalty']:g} s` (max {conf['penalty_cap']} Zeichen) · "
            f"**Ansagen** `{conf['announce']}`\n"
            f"Mehr mit `{ctx.clean_prefix}irpg set <name> <wert>`."
        )

    async def _store(self, ctx: commands.Context, key: str, value: Any) -> None:
        async with self.config.guild(ctx.guild).settings() as settings:
            settings[key] = value
        self._conf.pop(ctx.guild.id, None)
        await ctx.send(f"✅ **{key}** = `{value}`")

    @set_.command(name="toggle", aliases=["on", "off"])
    @admin_command
    async def set_toggle(self, ctx: commands.Context) -> None:
        """IdleRPG auf diesem Server ein- oder ausschalten."""
        current = await self.config.guild(ctx.guild).enabled()
        await self.config.guild(ctx.guild).enabled.set(not current)
        self._conf.pop(ctx.guild.id, None)
        state = "an" if not current else "aus"
        await ctx.send(f"IdleRPG ist jetzt **{state}**.")
        if not current:
            await ctx.send(
                f"Figuren anlegen mit `{ctx.clean_prefix}irpg register <Name>`."
            )

    @set_.command(name="channel", aliases=["kanal"])
    @admin_command
    async def set_channel(
        self, ctx: commands.Context, channel: Optional[discord.TextChannel] = None
    ) -> None:
        """Ansage-Kanal setzen (ohne Angabe: aus)."""
        await self.config.guild(ctx.guild).channel.set(channel.id if channel else None)
        await ctx.send(
            f"Ansagen gehen jetzt nach {channel.mention}."
            if channel
            else "Ansagen aus."
        )

    @set_.command(name="role", aliases=["rolle"])
    @admin_command
    async def set_role(self, ctx: commands.Context, role: discord.Role = None) -> None:
        """Rolle für registrierte Figuren (ohne Angabe: keine)."""
        await self.config.guild(ctx.guild).role.set(role.id if role else None)
        await ctx.send(
            f"Registrierte bekommen jetzt {role.mention}."
            if role
            else "Rollenvergabe aus."
        )

    @set_.command(name="speed", aliases=["tempo"])
    @admin_command
    async def set_speed(self, ctx: commands.Context, faktor: float) -> None:
        """Geschwindigkeit: 2 = doppelt so schnell, 0.5 = halb so schnell."""
        if not 0.05 <= faktor <= 100:
            await ctx.send("Bitte einen Faktor zwischen 0,05 und 100 angeben.")
            return
        await self._store(ctx, "speed", round(faktor, 3))

    @set_.command(name="base", aliases=["basiszeit"])
    @admin_command
    async def set_base(self, ctx: commands.Context, sekunden: int) -> None:
        """Basiszeit für Stufe 1 in Sekunden (Standard 300)."""
        if not 5 <= sekunden <= 86400:
            await ctx.send("Bitte 5 bis 86400 Sekunden angeben.")
            return
        await self._store(ctx, "base_time", float(sekunden))

    @set_.command(name="growth", aliases=["wachstum"])
    @admin_command
    async def set_growth(self, ctx: commands.Context, faktor: float) -> None:
        """Wachstum pro Stufe (Standard 1,14)."""
        if not 1.01 <= faktor <= 3.0:
            await ctx.send("Bitte einen Faktor zwischen 1,01 und 3,0 angeben.")
            return
        await self._store(ctx, "growth", round(faktor, 4))

    @set_.command(name="maxlevel", aliases=["maxstufe"])
    @admin_command
    async def set_maxlevel(self, ctx: commands.Context, stufe: int) -> None:
        """Höchste erreichbare Stufe."""
        if not 2 <= stufe <= 1000:
            await ctx.send("Bitte 2 bis 1000 angeben.")
            return
        await self._store(ctx, "max_level", stufe)

    @set_.command(name="battles", aliases=["kaempfe"])
    @admin_command
    async def set_battles(self, ctx: commands.Context) -> None:
        """Kämpfe ein- oder ausschalten."""
        conf = await self._settings(ctx.guild)
        await self._store(ctx, "battles", not conf.get("battles", True))

    @set_.command(name="npc", aliases=["schatten"])
    @admin_command
    async def set_npc(self, ctx: commands.Context) -> None:
        """Kämpfe gegen den Schatten des Reiches (wenn niemand sonst da ist)."""
        conf = await self._settings(ctx.guild)
        await self._store(ctx, "npc", not conf.get("npc", True))

    @set_.command(name="events", aliases=["ereignisse"])
    @admin_command
    async def set_events(self, ctx: commands.Context) -> None:
        """Schicksalsereignisse (Hand Gottes, Geschenke, Unheil) ein-/ausschalten."""
        conf = await self._settings(ctx.guild)
        await self._store(ctx, "events", not conf.get("events", True))

    @set_.command(name="quests", aliases=["abenteuer"])
    @admin_command
    async def set_quests(self, ctx: commands.Context) -> None:
        """Quests ein- oder ausschalten."""
        conf = await self._settings(ctx.guild)
        await self._store(ctx, "quests", not conf.get("quests", True))

    @set_.command(name="uniques", aliases=["einzigartig"])
    @admin_command
    async def set_uniques(self, ctx: commands.Context) -> None:
        """Einzigartige Gegenstände ein- oder ausschalten."""
        conf = await self._settings(ctx.guild)
        await self._store(ctx, "uniques", not conf.get("uniques", True))

    @set_.command(name="penalty", aliases=["strafe"])
    @admin_command
    async def set_penalty(self, ctx: commands.Context, sekunden: float) -> None:
        """Strafsekunden pro Nachrichten-Zeichen (0 = Reden kostet nichts)."""
        if not 0 <= sekunden <= 60:
            await ctx.send("Bitte 0 bis 60 Sekunden angeben.")
            return
        await self._store(ctx, "penalty", round(sekunden, 3))

    @set_.command(name="penaltycap", aliases=["strafenlimit"])
    @admin_command
    async def set_penaltycap(self, ctx: commands.Context, zeichen: int) -> None:
        """So viele Zeichen einer Nachricht werden höchstens gezählt."""
        if not 0 <= zeichen <= 2000:
            await ctx.send("Bitte 0 bis 2000 Zeichen angeben.")
            return
        await self._store(ctx, "penalty_cap", zeichen)

    @set_.command(name="announce", aliases=["ansagen"])
    @admin_command
    async def set_announce(self, ctx: commands.Context) -> None:
        """Ansagen im Kanal ein- oder ausschalten."""
        conf = await self._settings(ctx.guild)
        await self._store(ctx, "announce", not conf.get("announce", True))

    @set_.command(name="reset")
    @admin_command
    async def set_reset(self, ctx: commands.Context) -> None:
        """Alles auf Standard zurücksetzen und alle Figuren löschen."""
        view = ConfirmView(ctx.author, disable_buttons=True)
        view.message = await ctx.send(
            "Das löscht **alle Figuren** und setzt jede Einstellung zurück. Sicher?",
            view=view,
        )
        await view.wait()
        if not view.result:
            await ctx.send("Nichts passiert.")
            return
        await self.config.guild(ctx.guild).clear()
        self.players.pop(ctx.guild.id, None)
        self.quests.pop(ctx.guild.id, None)
        self._quest_loaded.discard(ctx.guild.id)
        self._conf.pop(ctx.guild.id, None)
        self._dirty.discard(ctx.guild.id)
        await ctx.send("Das Reich ist zurückgesetzt.")

    # -- Verwaltung -----------------------------------------------------

    @idlerpg.group(name="admin", aliases=["verwalten"], invoke_without_command=True)
    @admin_command
    async def admin(self, ctx: commands.Context) -> None:
        """Figuren verwalten (Admin)."""
        await ctx.send_help(ctx.command)

    async def _target(
        self, ctx: commands.Context, member: discord.Member
    ) -> Optional[Player]:
        players = await self._players(ctx.guild)
        player = players.get(member.id)
        if player is None:
            await ctx.send(f"{member.display_name} hat keine Figur.")
            return None
        return player

    @admin.command(name="hog", aliases=["handgottes"])
    @admin_command
    async def admin_hog(
        self, ctx: commands.Context, member: Optional[discord.Member] = None
    ) -> None:
        """Hand Gottes beschwören — für eine Figur oder das ganze Reich."""
        conf = await self._settings(ctx.guild)
        players = await self._players(ctx.guild)
        targets = (
            [await self._target(ctx, member)] if member else list(players.values())
        )
        targets = [player for player in targets if player is not None]
        if not targets:
            await ctx.send("Keine Figuren im Reich.")
            return

        lines = []
        for player in targets:
            shift = self._rng.uniform(0.05, 0.75)
            if self._rng.random() < 0.8:
                player.ttl -= player.next_time(conf) * shift
                lines.append(f"✨ **{player.name}** {shift * 100:.0f} % schneller")
            else:
                player.ttl += player.next_time(conf) * shift
                lines.append(f"⚡ **{player.name}** {shift * 100:.0f} % langsamer")
        self._dirty.add(ctx.guild.id)
        await ctx.send("Die Hand Gottes fährt herab:\n" + "\n".join(lines[:15]))

    @admin.command(name="level")
    @admin_command
    async def admin_level(
        self, ctx: commands.Context, member: discord.Member, stufe: int
    ) -> None:
        """Figur auf eine Stufe setzen."""
        if not 1 <= stufe <= 1000:
            await ctx.send("Bitte 1 bis 1000 angeben.")
            return
        player = await self._target(ctx, member)
        if player is None:
            return
        conf = await self._settings(ctx.guild)
        player.level = stufe
        player.ttl = next_level_time(stufe, conf)
        self._dirty.add(ctx.guild.id)
        await self._save(ctx.guild.id)
        await ctx.send(f"**{player.name}** ist jetzt Stufe **{stufe}**.")

    @admin.command(name="ttl")
    @admin_command
    async def admin_ttl(
        self, ctx: commands.Context, member: discord.Member, sekunden: int
    ) -> None:
        """Restzeit bis zur nächsten Stufe verändern (negativ = schneller)."""
        player = await self._target(ctx, member)
        if player is None:
            return
        player.ttl = max(0.0, player.ttl + sekunden)
        self._dirty.add(ctx.guild.id)
        await self._save(ctx.guild.id)
        await ctx.send(
            f"**{player.name}** braucht noch {fmt_duration(player.ttl)} bis Stufe "
            f"{player.level + 1}."
        )

    @admin.command(name="remove", aliases=["entfernen"])
    @admin_command
    async def admin_remove(self, ctx: commands.Context, member: discord.Member) -> None:
        """Figur eines Mitglieds löschen."""
        player = await self._target(ctx, member)
        if player is None:
            return
        players = await self._players(ctx.guild)
        players.pop(member.id, None)
        self._dirty.add(ctx.guild.id)
        await self._save(ctx.guild.id)
        await ctx.send(f"**{player.name}** wurde aus dem Reich entfernt.")


# ----------------------------------------------------------------------
# Persistente Views
# ----------------------------------------------------------------------


class JoinModal(discord.ui.Modal, title="Dem Reich beitreten"):
    name_input: discord.ui.TextInput = discord.ui.TextInput(
        label="Name der Figur",
        placeholder="z. B. Grimmwald",
        min_length=3,
        max_length=24,
    )
    class_input: discord.ui.TextInput = discord.ui.TextInput(
        label="Klasse (frei wählbar)",
        placeholder="z. B. Barde",
        required=False,
        max_length=30,
        default="Abenteurer",
    )

    def __init__(self, cog: IdleRPG) -> None:
        super().__init__()
        self.cog = cog

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await self.cog._join_from_interaction(
            interaction, self.name_input.value, self.class_input.value
        )


class RealmPanel(discord.ui.View):
    """Persistentes Panel: Beitreten, Charakter, Rangliste, Reich."""

    def __init__(self, cog: IdleRPG) -> None:
        super().__init__(timeout=None)
        self.cog = cog

    async def _player(self, interaction: discord.Interaction):
        players = await self.cog._players(interaction.guild)
        return players.get(interaction.user.id)

    @discord.ui.button(
        label="Beitreten",
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
        label="Charakter",
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
                "Du hast noch keine Figur — nimm **Beitreten**.", ephemeral=True
            )
            return
        conf = await self.cog._settings(interaction.guild)
        await interaction.response.send_message(
            embed=self.cog._char_embed(player, conf), ephemeral=True
        )

    @discord.ui.button(
        label="Rangliste",
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
        label="Reich",
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
    """Blättern durch die Rangliste — nur für den Aufrufer."""

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
                "Das ist nicht deine Rangliste.", ephemeral=True
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
