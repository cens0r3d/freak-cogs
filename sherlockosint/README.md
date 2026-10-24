# sherlockosint

> **Fork / archive.** The original cog was published without a real author name —
> `info.json` still carries the generator placeholder `YourName` — and its source
> repository no longer exists. This copy is archived here so the code is not lost.
> Only the command alias and the metadata were changed; the logic is untouched.

Looks a username up across the site list maintained by the
[Sherlock project](https://github.com/sherlock-project/sherlock) and renders the
hits as paginated embeds.

| Command | What it does |
| --- | --- |
| `[p]sherlock search <username>` | Full search, live progress, paginated results (aliases: `find`, `lookup`) |
| `[p]sherlock quick <username>` | One-shot search, only the accounts that exist |
| `[p]sherlock site <site> <username>` | Check a single site, e.g. `[p]sherlock site GitHub johndoe` |
| `[p]sherlock sites` | How many sites are loaded, and a sample of them |
| `[p]sherlock reload` | Re-fetch the site list from the Sherlock dataset |
| `[p]sherlock settings` | Current guild configuration |
| `[p]sherlock set <key> <value>` | Change one setting (see below) |
| `[p]sherlock addchannel [#channel]` | Restrict lookups to one channel |
| `[p]sherlock removechannel [#channel]` | Allow a channel again |
| `[p]sherlock clearchannels` | Drop the channel restriction |

`[p]sk` is an alias for the group — the `osint` alias the original shipped with
was removed because it collides with the `osint` cog in this repo. Two cogs
answering to the same command name means the second one refuses to load.

## Settings

| Key | Default | Range | Meaning |
| --- | --- | --- | --- |
| `enabled` | `true` | true/false | Master switch for the guild |
| `cooldown` | `30` | 0–300 s | Per-user pause between searches |
| `timeout` | `10` | 3–30 s | HTTP timeout per site |
| `results_per_page` | `15` | — | Hits per embed page |
| `show_not_found` | `false` | true/false | List sites with no account too |
| `nsfw_filter` | `true` | true/false | Hide NSFW sites outside NSFW channels |

## Data source

The site list comes from

```
https://raw.githubusercontent.com/sherlock-project/sherlock/master/sherlock_project/resources/data.json
```

It is fetched on load and cached in memory. `[p]sherlock reload` fetches it again.
No API key, no account, no proxy.

Every check is a plain `HEAD`/`GET` against `https://<site>/<username>` and reads
the HTTP status plus a few markers. Requests originate from the bot host, so that
IP is visible to the sites being queried. Nothing is brute-forced or scanned —
this is the same enumeration Sherlock itself performs.

## Troubleshooting

* **"No sites loaded"** — the raw GitHub URL was unreachable. Check outbound
  HTTPS from the bot host, then `[p]sherlock reload`.
* **Everything reports "not found"** — the dataset changed shape upstream or the
  host is being rate-limited. Compare with `[p]sherlock site GitHub <name>`,
  which uses the same code path.
* **Command not found** — you are on an old install where the group is still
  aliased to `osint`, which loses against the `osint` cog. Update with
  `[p]cog update sherlockosint`.
* **Search feels slow** — that is the nature of it: every site is queried with a
  `timeout`-bounded request. Raising the timeout helps flaky sites, lowering the
  page size makes the output shorter, not faster.

## Requirements

None. `aiohttp` ships with Red, so `[p]cog update` installs nothing on the host.
