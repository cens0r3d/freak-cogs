# PsychonautWiki

A psychoactive substance reference for Red-DiscordBot, built on
[PsychonautWiki](https://psychonautwiki.org). Look up dosages, durations, subjective
effects and combination warnings without leaving Discord.

Harm reduction reference only — this is not medical advice, and the bot does not
tell anyone how much to take beyond what the wiki already publishes.

## Install

```
[p]repo add freak-cogs https://github.com/cens0r3d/freak-cogs
[p]cog install freak-cogs psychonaut
[p]load psychonaut
```

No API key and no configuration. The bot needs the **Embed Links** permission (and
**Attach Files** if it should be able to answer with a file, which it currently
never does on its own).

## Commands

| Command | What it does |
| --- | --- |
| `[p]psychonaut <name>` | Overview: class, aliases, routes, tolerance, toxicity, addiction potential, cross-tolerance and the article intro with the structure image |
| `[p]psychonaut dose <name> [route]` | Dose table (threshold / light / common / strong / heavy) plus the full duration breakdown and bioavailability, one embed field per route |
| `[p]psychonaut effects <name>` | Subjective effects grouped into physical, visual, cognitive, auditory, multisensory, transpersonal and after effects |
| `[p]psychonaut interactions <name>` | Dangerous, unsafe and uncertain combinations |
| `[p]psychonaut search <text>` | Search canonical names and street names |
| `[p]psychonaut class [class]` | Without an argument: every substance class with a count. With one: the substances in it |
| `[p]psychonaut random [class]` | Random substance, optionally from one class |
| `[p]psychonaut stats` | Cache state: index size and age, cached pages, cache size |
| `[p]psychonaut refresh` | Drop the cache and reload the index (bot owner only) |

Aliases: `[p]pw` and `[p]psy` for the group, `dosage`/`doses` for `dose`,
`effect` for `effects`, `interaction`/`inter` for `interactions`, `find` for
`search`, `classes` for `class`.

Examples:

```
[p]psychonaut lsd
[p]psychonaut dose ketamine insufflated
[p]psychonaut interactions mdma
[p]psychonaut class dissociative
[p]psychonaut search benzo
```

## Names

A lookup accepts the page title, any street name and sloppy input: `acid`, `lucy`,
`cid` and `tabs` all reach LSD, `keta` reaches Ketamine, `2cb` reaches 2C-B,
`shrooms` reaches Psilocybin mushrooms. Matching runs in tiers — exact, then
compacted (ignoring spaces and punctuation), then prefix from three characters,
then whole word, then substring from four characters.

When a tier hits more than one substance the bot answers with the list instead of
picking one for you, and when nothing matches it offers the closest names plus the
wiki's own search as a fallback.

## Data sources

Two public interfaces, both keyless:

* **GraphQL** — `https://api.psychonautwiki.org` for everything structured: classes,
  tolerance (full / half / back to baseline), addiction potential, toxicity,
  cross-tolerances, and per route of administration the dose ranges,
  bioavailability and the duration breakdown (total, onset, come up, peak, offset,
  after effects), plus the three interaction lists.
* **MediaWiki** — `https://psychonautwiki.org/w/api.php` for the parts GraphQL does
  not expose usefully: the article intro paragraph (`action=parse&prop=wikitext`),
  the structure image (the `SkeletalImageFile` of `Template:SubstanceBox/<name>`,
  rendered to PNG through `thumb.php`) and the subjective effect lists
  (`{{effects/base}}` blocks, read from their `[[Effect::…]]` annotations).

GraphQL's own `summary`, `effects`, `effectsBySubstance` and `erowid` fields are not
used: they return template noise (`Summary sheet: LSD LSD`), the substance's own
name, or nothing at all.

## Caching

| Layer | Lifetime | Where |
| --- | --- | --- |
| Substance index (373 entries, ~960 names and aliases) | 7 days | memory + `psychonaut_cache.json` in the cog data dir |
| Detail page (GraphQL record + intro + effects + image) | 30 days | memory + the same file |

A failed fetch falls back to the cached copy, and a cache file that cannot be read
is ignored rather than fatal. `[p]psychonaut stats` shows both ages, `[p]psychonaut
refresh` (bot owner) starts from scratch.

## Permissions and limits

* Everything lives in embeds; long effect lists and multi-route dose tables are
  paginated with Red's standard menu controls.
* Nothing is stored per user or per guild — the cog has no config at all, so there
  is no end user data to export or delete.
* The lookups are read-only GET/POST requests against public endpoints; the cog
  never posts anything anywhere.

## Troubleshooting

**"PsychonautWiki is unreachable"** — neither the GraphQL API nor a cached index
answered. The bot keeps serving the cached index when it has one; if the message
appears repeatedly, check `[p]psychonaut stats` for the index age and whether the
host can reach `api.psychonautwiki.org` (`curl -sX POST
https://api.psychonautwiki.org/ -H 'Content-Type: application/json' -d
'{"query":"{ substances(limit: 1) { name } }"}'`).

**"No dosage data"** — the wiki itself lists no dose for that route. The reply names
the routes that do have data.

**"No effect list"** — the article has no parsable `{{effects/base}}` section, which
happens for stub pages and non-recreational entries.

**A substance shows the wrong class or weird aliases** — that is upstream data. The
cog never corrects it; `[p]psychonaut refresh` only re-reads what the API serves.

**A dose looks wrong** — check the wiki page linked in the embed before trusting it.
Dosages are copied verbatim, including their units (`µg`, `mg`, `ml`).
