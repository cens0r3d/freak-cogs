# goabase

New Goa/Psytrance parties from [goabase.net](https://www.goabase.net) as Discord
embeds, posted to a channel as they appear in the site's catalogue.

One message per party: flyer as the image, when/where/entry/line-up as fields,
and link buttons for tickets, map and the party page.

```
[p]goabase channel #parties
[p]goabase countries CH DE AT
[p]goabase interval 30
[p]goabase preview 3
```

Install as `goabase`, load with `[p]load goabase`. The command is `[p]goabase`,
alias `[p]gbase`, and every subcommand works as a slash command too.

## Commands

| Command | What it does |
| --- | --- |
| `[p]goabase channel [#channel]` | Set the announcement channel (no argument clears it) |
| `[p]goabase countries [codes…]` | Watchlist of ISO country codes (max 20); no argument lists the current set and the countries that currently have parties. Setting a list switches worldwide mode off |
| `[p]goabase worldwide [on\|off]` | Ignore the country list and watch the whole site (no argument toggles) |
| `[p]goabase interval <5-360>` | Minutes between checks (default 30) |
| `[p]goabase timezone <zone>` | Timezone party times are rendered in (default `Europe/Zurich`) |
| `[p]goabase days <1-365>` | Only announce parties starting within this window (default 120) |
| `[p]goabase batch <1-10>` | At most this many announcements per poll (default 5) |
| `[p]goabase pause` / `resume` | Stop and restart announcements, settings kept |
| `[p]goabase reseed` | Mark every current party as already announced |
| `[p]goabase status` | Channel, state, countries, interval, known parties, next check |
| `[p]goabase next [n]` | Upcoming parties as a list, nothing posted |
| `[p]goabase show <link\|id>` | Post the embed for one party on demand |
| `[p]goabase autoembed [on\|off]` | Answer posted goabase party links with the embed (default off) |
| `[p]goabase autochannel [#channel]` | Limit link embeds to one channel (no argument: everywhere) |
| `[p]goabase preview [n]` | Post the next n parties right now, exactly as an announcement looks |

Settings need **Manage Server**; `status` and `next` are open to everyone. The bot
needs **View Channel**, **Send Messages** and **Embed Links** in the target
channel.

## Link embeds

`[p]goabase autoembed` makes the cog answer a posted party link with the same
embed, as a reply to that message. Up to three embeds per message, deduplicated,
never deleting anything and never mentioning anyone. Other hosts and non-party
pages are ignored; a party that is not on goabase stays silent.

## Country codes and worldwide

Any ISO 3166-1 alpha-2 code is accepted (`CH`, `DE`, `AT`, …). The list printed by
`[p]goabase countries` is not a whitelist — it is exactly the countries that
currently have upcoming parties, read live from the site.

`[p]goabase worldwide` drops the filter and watches the whole catalogue (~320
parties). That is where the flood controls matter: `days` keeps the channel tied
to the near future and `batch` caps how many announcements go out per poll.

## What to expect

- **The first poll after setup posts nothing.** It records the current catalogue
  as the baseline; `preview` is there to see the embeds anyway.
- Changing `channel`, `countries` or `worldwide` resets the baseline, so a new
  watchlist never dumps its backlog into the channel.
- Party ids stay in the "already announced" set for 45 days, then get pruned.
- Deduplication is per party id, so a restart or a re-poll never double-posts.

## Troubleshooting

| Symptom | Cause / fix |
| --- | --- |
| Nothing is posted after setup | Expected: the first poll is the baseline. `[p]goabase status` shows the id count, `preview 3` posts immediately |
| `Announcement channel cleared` | The channel was deleted; the cog clears the setting instead of failing every poll. Set it again |
| Missing permissions message | The bot needs View Channel, Send Messages and Embed Links in that channel |
| `Could not read the party list` | goabase.net was unreachable or answered a non-JSON page. Check `status`, then `reseed` |
| Parties from a neighbouring country appear | Country codes are authoritative on the site: a border-town party is listed under its own country. Widen `countries` |
| Times look shifted | `[p]goabase timezone` decides the rendering; the data carries the party's own UTC offset |
| Line-up shows odd fragments | Organisers paste free text there; the party page has the full text |

## Requirements

Red-DiscordBot 3.5.0+, discord.py 2.x, Python 3.9+ (`zoneinfo`). No extra
packages — `aiohttp` ships with Red. Outbound HTTPS to `www.goabase.net` and
`img.goabase.net` is all it needs.
