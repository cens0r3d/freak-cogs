# IdleRPG

The IRC classic [idlerpg.net](https://www.idlerpg.net/) as a Discord cog.
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

The panel carries four buttons — **Join** (name and class via modal),
**Character**, **Leaderboard**, **Realm** — and keeps working after a restart.

## Commands

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
| `[p]irpg panel [#channel]` | Post the button panel (**Manage Server**) |
| `[p]irpg set <setting> <value>` | Change one rule (**Manage Server**; alias `settings`, no argument shows the config) |
| `[p]irpg admin hog [@member]` | Hand of God (alias: `handofgod`) |
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
| `battles` / `npc` / `events` / `quests` / `uniques` | on | Duels, the Shadow of the Realm, fate events, auto quests, the eight unique items |
| `penalty <seconds>` | `1` | Time cost per message character, `0` = talking is free |
| `penaltycap <chars>` | `60` | Characters of a message counted at most |
| `announce` | on | Announce level-ups, fights, quests and fate in the channel |
| `reset` | — | Wipe all characters and reset every setting, asks first |

## The rules

- **Levelling.** Time to the next level is `base × growth ^ level`; past level 50
  it grows by one day per level instead of exponentially. No voice, no command,
  no activity needed.
- **Talking costs time.** Every message adds `penalty × characters × 1.14 ^ level`
  seconds. Commands are free, so using the cog never hurts. Silence is the best
  strategy, as in the original.
- **Items.** Ten slots (weapon, shield, helm, armor, gloves, leggings, boots,
  ring, amulet, charm). Each level-up rolls one — the unique items first, then a
  regular find weighted towards low levels. An item is equipped only if it beats
  the current one.
- **Battles.** A level-up may challenge another present character: below level 25
  with 25 % chance, above always. Both sides roll up to their item sum, the
  higher roll wins, and the outcome shifts the winner's or loser's remaining
  time. Crits and item theft depend on alignment — good hits harder, evil crits
  more often.
- **Fate and quests.** Per character and day: Hand of God, godsend, calamity,
  team battles. Quests start on their own once enough characters have the
  required level and idle time; a single message from a quester fails it for
  everyone.

## Data and permissions

One character per member is stored per guild — nothing leaves the host.
`[p]mydata` exports it as JSON, `[p]mydata forgetme` or `[p]irpg unregister`
deletes it.

The bot needs **Send Messages** and **Embed Links** in the announcement channel,
and **Manage Roles** only if `[p]irpg set role` is used (the bot's role must sit
above the one it hands out).

## Credits

Inspired by [The Idle RPG](https://www.idlerpg.net/) by the #G7 crowd on
SlashNET — the formulas, the penalties and the item system are its ideas, ported
to Discord. This cog is an independent implementation, not affiliated with the
original.

MIT — see the repository's [LICENSE](../LICENSE).
