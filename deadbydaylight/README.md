# DBD

Dead by Daylight information for Red-DiscordBot: killers with their real move speed
and terror radius, survivors, perks with all three tier values rendered, items,
offerings and maps.

**No API key and no configuration.** The data comes from the community JSON API
behind [dbd.tricky.lol](https://dbd.tricky.lol), which mirrors the current game
build; the payloads are cached in memory and on disk, so a restart or a short
outage still answers.

Everything the bot says is English.

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

The **cog folder is called `deadbydaylight`**, the command is `[p]dbd` (alias
`[p]deadbydaylight`). Red names the extension after the folder, so `[p]cog install` and
`[p]load` want `deadbydaylight`.

No requirements beyond Red itself — the cog uses `aiohttp`, which ships with Red.

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
| `[p]dbd list <killers\|survivors\|perks\|items\|offerings\|maps> [role]` | Browse a table, 15 per page, paginated |
| `[p]dbd stats` | Row counts, cache age, how many icon files are mapped |
| `[p]dbd refresh` | Force a full reload from the data source (bot owner) |

`list` takes an optional `killer`/`survivor` filter, mostly useful for
`[p]dbd list perks killer`.

## How names are matched

Lookups run through a normalised key (`&` is read as `and`, possessive `'s` is
dropped, accents folded, punctuation and case ignored) and then through four
rules, in this order:

1. **exact** match on the whole name
2. **prefix** match (only for queries of four characters or more)
3. **whole word** match — the query has to be one word of the name
4. **substring** match (only for queries of five characters or more)

So these all resolve:

| You type | It finds | Why |
| --- | --- | --- |
| `hex ruin`, `HEX RUIN` | Hex: Ruin | exact |
| `quick and quiet` | Quick & Quiet | exact (`&` = `and`) |
| `nurse` | The Nurse | whole word |
| `the nurse` | The Nurse | exact |
| `onryo` | The Onryō | exact (accents folded) |
| `alexs toolbox` | Alex's Toolbox | exact (possessive dropped) |
| `ruin` (perks) | Hex: Ruin | whole word |

Rules 3 and 4 exist because squashing a name and searching inside it produces
nonsense: `hex` would match *The Xenomorph*, `the` would match *Mettle of Man*.
Word boundaries and the minimum lengths keep those out.

If several entries match equally well (`[p]dbd perk hex` → 21 Hex perks,
`[p]dbd offering gateau` → two anniversary cakes) the cog answers with a
candidate list instead of picking one.

A name that matches nothing at all gets the closest names by character overlap as
a hint (`serving` → Surge, Scavenger). When even that finds nothing, the embed
points at `[p]dbd list <table>` instead of inventing a suggestion.

`[p]dbd survivor nurse` is explicitly refused with "The Nurse is a Killer — try
`[p]dbd killer The Nurse`" instead of silently showing a killer.

## Perk descriptions are rendered

The raw payload stores placeholders next to a `tunables` table:

```
{ "description": "You gain {Tunable.S02P03.Haste%}% {Keyword.Haste} for
                  {Tunable.S02P03.HasteDuration}s.", "tunables": { … } }
```

The cog substitutes them per tier, so what you read is the in-game text:

- Sprint Burst → "You gain 50 % Haste for 2 s." plus the exhaustion cooldown
- Hex: Ruin → "regresses at 100/125/150 % speed" — all three tiers, slash-separated
- Status effects (`{Keyword.Exposed}`) and input prompts (`{Input.UseItem}` →
  "use item button") are spelled out too

If a tunable is missing from the payload the value shows as `?` — Sloppy Butcher's
haemorrhage bonus is such a case, since the game build leaves it out.

## Killer numbers

`[p]dbd killer` derives the two numbers people actually ask for from the game
values instead of copying a string:

- **Move speed** — `MaxWalkSpeed` is in cm/s. 460 → `4.6 m/s (115 %)`, computed
  against the 4.0 m/s survivor base.
- **Terror radius** — `TerrorRadius` is in cm. 3200 → `32 m`.

Both stay hidden when the payload has no tunables for that character (survivors
never do).

## Data source and caching

| What | Where |
| --- | --- |
| Killers, survivors | `GET https://dbd.tricky.lol/api/characters` |
| Perks | `GET https://dbd.tricky.lol/api/perks` |
| Items | `GET https://dbd.tricky.lol/api/items` |
| Offerings | `GET https://dbd.tricky.lol/api/offerings` |
| Maps | `GET https://dbd.tricky.lol/api/maps` |

- The five endpoints are fetched once, in parallel, on first use.
- **12 hours** in memory, then a refresh on the next command.
- The full payload is also written to `dbd_data.json` in the cog's data directory
  (`…/data/<instance>/cogs/DBD/`). A restart reads that file instead of the API,
  and an unreachable API falls back to it — even after weeks.
- `[p]dbd refresh` (bot owner) forces a reload and reports how long it took.
- `[p]dbd stats` shows the row counts and how old the cache is.

At the time of writing the dataset holds 44 killers, 54 survivors, 321 perks, 98
items, 125 offerings and 59 maps. Counts grow with the game; nothing in the cog is
hardcoded to a chapter.

**Not included:** the Shrine of Secrets. `/api/shrine` returns only internal perk
IDs (`K24P01`) without names, and there is no published ID→name table this cog
could resolve them against. Showing raw IDs would be noise, so the command is
left out rather than half-built.

## Images

Icons are optional eye candy. The primary source is
[`Zawgalicious/DBDImages`](https://github.com/Zawgalicious/DBDImages), which keeps every
icon in one tree, so a single recursive listing maps all of them:

| Folder | Used for | Coverage against the dataset |
| --- | --- | --- |
| `images/characters/survivor/` | survivor portraits | 51/54 |
| `images/characters/killer/` | killer portraits | 42/44 |
| `images/perks/survivor/` | survivor perk icons | 173/176 |
| `images/perks/killer/` | killer perk icons | 145/145 |
| `images/maps/` | map thumbnails | 57/59 |
| `images/offerings/` | offering thumbnails | 98/125 |
| `images/items/` | item thumbnails | 42/98 |

(`profsoru/dbd-perk-icons` and `profsoru/dbd-killer-perk-icons` are read as a second pass
and only fill perk gaps, so the main tree stays authoritative. `images/items/` is thin
because that mirror only carries the higher-rarity variants — a missing icon is fine, a
wrong one is not.)

- The listing is read at most once every **3 days** and cached on disk, then only files
  that really exist are linked. Nothing builds a URL out of a name and hopes.
- **File names are inconsistent**, so lookups try several keys: the name, the name without
  `The`, `&` written as `and` and as nothing, the possessive both ways (`dragonsgrip` and
  `dragongrip`), initials stripped (`leonscottkennedy` for *Leon S. Kennedy*), the quoted
  nickname (`billoverbeck` for *William "Bill" Overbeck*), then single words (`davidtapp`
  for *Detective Tapp*), then an unambiguous partial match, and finally a ≥0.9 spelling
  match (that one catches the mirror's own `barbecueandchilli` typo).
- When none of that is clear the entry simply gets **no** thumbnail: an empty corner beats
  the wrong character's face. *The Slasher* and *The Judgment* have no file upstream, and
  several of the newest items are missing too.
- If GitHub is unreachable or rate-limited, every command still renders — just without
  images. The icon cache carries a format version, so a stale cache from an older release
  is discarded instead of producing dead links.

## Errors are embeds

A miss, an ambiguous query, a wrong-role request, an unknown list, an empty filter
and an unreachable data source all answer with an embed naming the next step.
None of them raises, so the channel never sees `An error occurred: …`.

## Troubleshooting

- **"Dead by Daylight data unavailable"** — the API is down and no local copy
  exists yet (fresh install, or the data directory was wiped). Try again later;
  once one fetch succeeds, outages no longer matter.
- **An old killer or perk is missing** — the cog is serving a cache up to 12 hours
  old. `[p]dbd refresh` (bot owner) pulls immediately.
- **No perk icon, but the perk exists** — either that perk is not in the icon
  repository yet or GitHub could not be read. `[p]dbd stats` shows how many files
  were mapped per set.
- **The wrong perk comes back** — the query was ambiguous. Use the full name from
  `[p]dbd list perks`.
- **`[p]dbd refresh` says you are not the bot owner** — that is the only
  restricted subcommand; everything else is open to every member.

## Data and privacy

Nothing is stored per user or per guild — no settings, no history, no identifiers.
The only files the cog writes are the two JSON caches in its data directory, and
`[p]mydata` returns nothing for this cog. Requests go to `dbd.tricky.lol` and (for
images) `api.github.com` / `raw.githubusercontent.com` from the bot host, so that
host's IP is visible to both.

## Credits

The dataset and the icons are community work — this cog only renders them. Dead by
Daylight is a trademark of Behaviour Interactive Inc.; the cog is unofficial and
not affiliated with them.
