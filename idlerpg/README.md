# IdleRPG

The IRC classic [idlerpg.net](https://www.idlerpg.net/) as a Red-DiscordBot cog.
Register a character and it levels purely by idling: every level-up finds an
item, and the level-up may start a battle against anyone else present. No IRC
server, no password — the Discord account *is* the login, and **every guild is
its own realm** with its own settings.

Everything the bot says is English.

## Install

```
[p]repo add freak-cogs https://github.com/cens0r3d/freak-cogs
[p]cog install freak-cogs idlerpg
[p]load idlerpg
```

No requirements — the cog only uses what Red already ships.

## Quick start

```
[p]irpg set toggle              # turn this server's realm on
[p]irpg panel #games            # post the panel with buttons
[p]irpg register Grimmwald Bard # name (3–24 chars) + free-form class (≤ 30)
[p]irpg char                    # your sheet: level, progress, all ten slots
```

The panel carries four buttons — **Join** (a modal asking for name and class),
**Character**, **Leaderboard**, **Realm** — and answers ephemerally. It keeps
working after a restart, so one panel per server is enough.

## Commands

Player commands:

| Command | What it does |
| --- | --- |
| `[p]irpg register <name> [class]` | Create a character (aliases: `join`, `start`) |
| `[p]irpg rename <name>` | Rename your character |
| `[p]irpg unregister` | Delete your character, asks first (aliases: `leave`, `quit`) |
| `[p]irpg align <good\|neutral\|evil>` | Pick an alignment |
| `[p]irpg char` | Character sheet (aliases: `me`, `whoami`, `sheet`) |
| `[p]irpg top [page]` | Leaderboard, paginated (aliases: `leaderboard`, `rank`) |
| `[p]irpg realm` | Realm overview: characters, present players, top three, quest (alias: `status`) |
| `[p]irpg quest` | Active quest, its members and remaining time |

Needs **Manage Server**:

| Command | What it does |
| --- | --- |
| `[p]irpg panel [#channel]` | Post the button panel |
| `[p]irpg set <setting> <value>` | Change one rule (alias: `settings`, no argument shows the current config) |
| `[p]irpg admin hog [@member]` | Summon the Hand of God (alias: `handofgod`) |
| `[p]irpg admin level <@member> <n>` | Set a character's level |
| `[p]irpg admin ttl <@member> <seconds>` | Shift the time to the next level (negative = faster) |
| `[p]irpg admin remove <@member>` | Delete a character (alias: `delete`) |

## Settings

| Setting | Default | Meaning |
| --- | --- | --- |
| `toggle` | off | Enable the realm (aliases `on`, `off`) |
| `channel [#channel]` | none | Where announcements go; falls back to the system channel |
| `role [@role]` | none | Role handed to registered characters |
| `speed <factor>` | `1` | Global pace: `2` = twice as fast, `0.5` = half |
| `base <seconds>` | `300` | Base time for level 1 |
| `growth <factor>` | `1.14` | Growth per level |
| `maxlevel <n>` | `120` | Highest reachable level |
| `battles` | on | Duels on level-up |
| `npc` | on | Fight the Shadow of the Realm when nobody else is around |
| `events` | on | Hand of God, godsends, calamities, team battles |
| `quests` | on | Automatic quests for idle players |
| `uniques` | on | The eight unique items |
| `penalty <seconds>` | `1` | Time cost per message character, `0` = talking is free |
| `penaltycap <chars>` | `60` | Characters of a message counted at most |
| `announce` | on | Announce level-ups, fights, quests and fate in the channel |
| `reset` | — | Wipe all characters and reset every setting, asks first |

## How the game works

**Levelling.** A character levels by being in the guild. The time to the next
level is `base × growth ^ level`; past level 50 it grows linearly by one day
per level instead of exponentially. Nothing else is required — no voice
channel, no command, no activity.

**Talking costs time.** Every message from a registered member adds
`penalty × characters × 1.14 ^ level` seconds to their next level, capped at
`penaltycap` characters. Command invocations are free, so using the cog never
hurts the player. Silence is the best strategy, exactly as in the original.

**Items.** Ten slots (weapon, shield, helm, armor, gloves, leggings, boots,
ring, amulet, charm). Each level-up rolls one item: first the eight unique
items (1/40 per eligible level-up), then a regular find up to `1.5 × level`,
weighted `1 / 1.4 ^ n` — low levels are common, high ones are rare. An item is
equipped only if it beats the current one.

**Battles.** A level-up may challenge another present character: below level 25
with a 25 % chance, from level 25 on always. Each side rolls a random number up
to the sum of its item levels; the higher roll wins. Winning takes at least 7 %
off the winner's remaining time (more against higher-level opponents), losing
adds at least 7 %. A win crits with 1/50 (good), 1/35 (neutral) or 1/20 (evil)
and then also penalises the loser; a win can steal one better item (≈ 2 %).

**Alignment.** Good: +10 % item sum in battles, but rare crits. Evil: −10 % item
sum, but crits at 1/20. Neutral: neither.

**Fate.** Per online character and day: Hand of God (1/20, 80 % helpful, shifts
5–75 % of the remaining time), godsend and calamity (1/8 each, 5–12 % of the
time or ±10 % on one item), team battle (5 %, needs six present characters).

**Quests.** Once enough characters have the required level and idle time, a
quest starts on its own (default: three characters, level 20+, six hours idled,
6–12 hours duration). It fails as soon as one participant takes a penalty — a
single message is enough — and then every present player pays the realm
penalty. Succeed and each quester drops 25 % of their remaining time.

## Data and privacy

One character per member is stored per guild: name, class, alignment, level,
remaining time, equipped items, counters (wins, losses, crits, steals,
penalties, idle time) and timestamps. Nothing leaves the host and nothing is
sent to a third party. `[p]mydata` returns the stored data as a JSON file,
`[p]mydata forgetme` deletes it, and `[p]irpg unregister` removes the character
as well.

Characters are written back every 60 seconds and when the cog unloads, not on
every tick, so a busy server does not hammer the config. A character whose
member leaves the server keeps its progress but stops accumulating time.

## Permissions

- **Send Messages** and **Embed Links** in the announcement channel.
- **Manage Roles** only if `[p]irpg set role` is used, with the bot's role above
  the one it hands out.
- The panel needs no permissions beyond sending messages and embeds.

## Files

| File | Contents |
| --- | --- |
| `idlerpg.py` | The cog: config, listener, tick, commands, embeds, panel |
| `game.py` | Pure rules without `discord`/`redbot` imports: formulas, item rolls, combat, fate probabilities, serialization |

Keeping the rules separate is deliberate — `game.py` can be driven by a test
harness with the real interpreter, without a bot token or a Discord connection.

## Credits

Inspired by [The Idle RPG](https://www.idlerpg.net/) by the #G7 crowd on
SlashNET — the formulas, the penalties and the item system are its ideas, ported
to Discord. This cog is an independent implementation, not affiliated with the
original.

MIT — see the repository's [LICENSE](../LICENSE).
