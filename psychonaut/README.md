# PsychonautWiki

A psychoactive substance reference built on
[PsychonautWiki](https://psychonautwiki.org). Look up dosages, durations,
subjective effects and combination warnings without leaving Discord.

Harm reduction reference only — this is not medical advice.

## Install

```
[p]repo add freak-cogs https://github.com/cens0r3d/freak-cogs
[p]cog install freak-cogs psychonaut
[p]load psychonaut
```

No API key and no configuration. The bot needs **Embed Links**.

## Commands

| Command | What it does |
| --- | --- |
| `[p]psychonaut <name>` | Overview: class, aliases, routes, tolerance, toxicity, addiction potential, cross-tolerance, article intro and structure image |
| `[p]psychonaut dose <name> [route]` | Dose table (threshold / light / common / strong / heavy) plus duration breakdown and bioavailability, one field per route |
| `[p]psychonaut effects <name>` | Subjective effects: physical, visual, cognitive, auditory, multisensory, transpersonal, after effects |
| `[p]psychonaut interactions <name>` | Dangerous, unsafe and uncertain combinations |
| `[p]psychonaut search <text>` | Canonical names and street names |
| `[p]psychonaut class [class]` | Every substance class with a count, or the substances in one class |
| `[p]psychonaut random [class]` | Random substance, optionally from one class |
| `[p]psychonaut stats` | Cache state: index size and age, cached pages, cache size |
| `[p]psychonaut refresh` | Drop the cache and reload the index (bot owner) |

Aliases: `[p]pw` and `[p]psy`, plus `dosage`/`doses`, `effect`, `interaction`/`inter`,
`find` and `classes`.

```
[p]psychonaut lsd
[p]psychonaut dose ketamine insufflated
[p]psychonaut interactions mdma
```

## Names

A lookup accepts the page title, any street name and sloppy input: `acid`, `lucy`
and `tabs` all reach LSD, `keta` reaches Ketamine, `2cb` reaches 2C-B. Unknown
names get the closest matches plus a link to the wiki's own search. When an input
fits more than one substance, the bot lists them instead of picking one.

## Notes

- The substance index is cached for 7 days, single pages for 30 days;
  `[p]psychonaut stats` shows both ages, `refresh` starts over.
- Everything lives in embeds; long effect lists and multi-route dose tables are
  paginated.
- Nothing is stored per user or per guild — there is no config at all.

## Troubleshooting

- **"PsychonautWiki is unreachable"** — neither the API nor a cached index
  answered. Check `[p]psychonaut stats` and whether the host can reach
  `api.psychonautwiki.org`.
- **"No dosage data"** — the wiki lists no dose for that route. The reply names
  the routes that do have data.
- **"No effect list"** — stub pages and non-recreational entries have no parsable
  effect section.
- **A dose looks wrong** — check the linked wiki page. Dosages are copied
  verbatim, including their units.
