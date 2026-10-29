# DBD

Dead by Daylight information: killers with their real move speed and terror
radius, survivors, perks with all three tier values, items, offerings and maps.

**No API key and no configuration.** Everything is in embeds, so the bot needs
**Embed Links**. Everything the bot says is English.

```
[p]dbd killer nurse
[p]dbd perk hex ruin
[p]dbd survivor dwight
[p]dbd item toolbox
[p]dbd map greenville
[p]dbd list perks killer
[p]dbd search ruin
```

## Install

```
[p]repo add freak-cogs https://github.com/cens0r3d/freak-cogs
[p]cog install freak-cogs deadbydaylight
[p]load deadbydaylight
```

The **folder is called `deadbydaylight`**, the command is `[p]dbd` (alias
`[p]deadbydaylight`) — `cog install` and `load` want the folder name.

No requirements beyond Red itself.

## Commands

| Command | What it does |
| --- | --- |
| `[p]dbd` | Command overview |
| `[p]dbd killer <name>` | Difficulty, gender, height, move speed, terror radius, personal perks, bio |
| `[p]dbd survivor <name>` | Same layout for survivors |
| `[p]dbd char <name>` | Either role, without the filter |
| `[p]dbd perk <name>` | Description with tier values, owner, teachable level, categories |
| `[p]dbd item <name>` | Description, item type, rarity |
| `[p]dbd offering <name>` | Description, rarity, type, tags, retired flag |
| `[p]dbd map <name>` | Map with its realm |
| `[p]dbd lore <name>` | Full backstory of a character, paginated |
| `[p]dbd random [perks\|killers\|survivors]` | Roll a random entry (default: perks) |
| `[p]dbd search <text>` | Search every table at once, paginated |
| `[p]dbd list <killers\|survivors\|perks\|items\|offerings\|maps> [role]` | Browse a table, 15 per page |
| `[p]dbd stats` | Row counts, cache age, mapped icon files |
| `[p]dbd refresh` | Force a full reload (bot owner) |

## Names

Lookups are forgiving: case, accents, `&` versus `and`, possessives and
punctuation do not matter. `hex ruin`, `HEX RUIN` and `onryo` all resolve, and
partial names work (`ruin` → Hex: Ruin). When several entries match equally well,
the bot lists the candidates instead of picking one for you. A miss gets the
closest names as a hint, and `[p]dbd survivor nurse` is refused with a pointer to
`[p]dbd killer The Nurse`.

## Data and caching

The data comes from the community API behind
[dbd.tricky.lol](https://dbd.tricky.lol) and is cached in memory for 12 hours and
on disk, so a restart or a short outage still answers. `[p]dbd stats` shows the
age, `[p]dbd refresh` (owner) pulls immediately. Around 44 killers, 54 survivors,
321 perks, 98 items, 125 offerings and 59 maps at the time of writing.

**Not included:** the Shrine of Secrets — the API returns only internal perk IDs
without names.

Characters, perks, maps and items come with icons when one exists upstream; a
missing icon is left out rather than guessed, so an empty corner never turns into
the wrong face.

## Troubleshooting

- **"Dead by Daylight data unavailable"** — the API is down and no local copy
  exists yet. Try again later; once one fetch succeeds, outages no longer matter.
- **An old killer or perk is missing** — the cache is up to 12 hours old.
  `[p]dbd refresh` pulls immediately.
- **No perk icon, but the perk exists** — it is not in the icon repository yet.
- **The wrong perk comes back** — the query was ambiguous. Use the full name from
  `[p]dbd list perks`.
- **`[p]dbd refresh` says you are not the bot owner** — that is the only
  restricted subcommand.

## Credits

The dataset and the icons are community work — this cog only renders them. Dead by
Daylight is a trademark of Behaviour Interactive Inc.; the cog is unofficial and
not affiliated with them.
