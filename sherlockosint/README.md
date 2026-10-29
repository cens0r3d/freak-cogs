# sherlockosint

> **Fork / archive.** The original cog was published without a real author name —
> `info.json` still carries the placeholder `YourName` — and its source repository
> no longer exists. This copy is archived so the code is not lost. Only the
> command alias and the metadata were changed.

Looks a username up across the site list of the
[Sherlock project](https://github.com/sherlock-project/sherlock) and renders the
hits as paginated embeds. No requirements — `aiohttp` ships with Red.

| Command | What it does |
| --- | --- |
| `[p]sherlock search <username>` | Full search, live progress, paginated results (aliases: `find`, `lookup`) |
| `[p]sherlock quick <username>` | One-shot search, only the accounts that exist |
| `[p]sherlock site <site> <username>` | Check a single site, e.g. `[p]sherlock site GitHub johndoe` |
| `[p]sherlock sites` | How many sites are loaded, and a sample of them |
| `[p]sherlock reload` | Re-fetch the site list |
| `[p]sherlock settings` | Current guild configuration |
| `[p]sherlock set <key> <value>` | Change one setting (see below) |
| `[p]sherlock addchannel` / `removechannel` / `clearchannels` | Restrict lookups to channels |

`[p]sk` is an alias for the group — the `osint` alias the original shipped with
was removed because it collides with the `osint` cog in this repo.

## Settings

| Key | Default | Range | Meaning |
| --- | --- | --- | --- |
| `enabled` | `true` | true/false | Master switch for the guild |
| `cooldown` | `30` | 0–300 s | Per-user pause between searches |
| `timeout` | `10` | 3–30 s | HTTP timeout per site |
| `results_per_page` | `15` | — | Hits per embed page |
| `show_not_found` | `false` | true/false | List sites with no account too |
| `nsfw_filter` | `true` | true/false | Hide NSFW sites outside NSFW channels |

The site list is fetched from the Sherlock dataset on load and kept in memory;
`[p]sherlock reload` fetches it again. No API key, no account, no proxy. Requests
come from the bot host, so that IP is visible to the sites being queried.

## Troubleshooting

* **"No sites loaded"** — the raw GitHub URL was unreachable. Check outbound
  HTTPS from the bot host, then `[p]sherlock reload`.
* **Everything reports "not found"** — the dataset changed shape upstream or the
  host is being rate-limited. Compare with `[p]sherlock site GitHub <name>`.
* **Command not found** — you are on an old install where the group is still
  aliased to `osint`, which loses against the `osint` cog. Update with
  `[p]cog update sherlockosint`.
* **Search feels slow** — that is the nature of it: every site is queried with a
  `timeout`-bounded request.
