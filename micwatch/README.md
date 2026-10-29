# MicWatch

Moves members to another voice channel when they keep their **microphone switched
on** — or actually **talk** — for longer than a configurable threshold (default:
30 s). Made for the "afk with an open mic" problem: point `target` at a
quarantine channel and let it sort itself out.

## Modes

| Mode | Reads | Needs |
| --- | --- | --- |
| `mic` (default) | the mute button state — mic switched on, even when silent | **Move Members** only |
| `speak` | real voice activity (the green ring) | the bot sits in the channel: **Connect**, plus voice support in the bot venv |

`speak` mode only sees *transitions*: someone who was already talking when the
bot joined is picked up at their next start/stop, not retroactively.

## Install

Voice support is only needed for `speak` mode:

```bash
pip install -U "Red-DiscordBot[voice]"        # pynacl + davey
# or, on a running bot:  [p]pipinstall pynacl davey
```

Then **restart the bot** — a reload is not enough, discord.py decides at import
time whether voice is available.

## Commands (`[p]micwatch`, aliases `micmon`, `mwatch`)

| Command | Effect |
| --- | --- |
| `toggle` | enable/disable, lists missing settings |
| `mode [mic\|speak]` | show/set the detection mode |
| `threshold [seconds]` | seconds before the move (default 30, 3–3600) |
| `grace [seconds]` | `speak`: tolerated silence before the counter resets (default 2) — **the important knob** |
| `hold [seconds]` | `speak`: how long one speaking signal is trusted without a new event |
| `rearm [seconds]` | how long a moved member is left alone (default 60, 0–3600) |
| `target [channel\|off]` | destination channel; `off`/`clear` clears it |
| `watch add\|remove\|list\|clear` | monitored channels (empty = all, `mic` mode only) |
| `immune add\|remove\|list\|clear` | roles/members that are never moved |
| `notify` | toggle the announcement in the target channel |
| `resetmute` | `mic`: muting resets (on) or pauses (off) the counter |
| `ignorebots` | ignore other bots (default on) |
| `message [text]` | announcement text; `{user}` `{mention}` `{seconds}` `{channel}` |
| `join [channel]` | pin the bot into a channel (needed for `speak`) |
| `leave` | disconnect / drop the pin |
| `settings` | configuration overview |
| `status` | live view: tracked members + elapsed seconds (alias `debug`) |
| `reset` | all settings back to defaults |

Default announcement: `{user} kept their microphone open for {seconds}s and was moved.`

## Quick start

```
[p]micwatch target #quarantine
[p]micwatch threshold 30
[p]micwatch watch add #lobby     # optional: only watch this channel
[p]micwatch toggle
```

For real voice activity instead of the mute-button state:

```
[p]micwatch mode speak
[p]micwatch watch add #lobby     # required in this mode
[p]micwatch toggle
```

## Tuning `speak` mode

With the default `grace 2` a real conversation never reaches the threshold —
every normal pause restarts the count. Raise it:

```
[p]micwatch threshold 20     # move after 20 s of talking instead of 30
[p]micwatch grace 8          # pauses up to 8 s do not restart the counter
```

`status` shows `grace left` per tracked member. Some clients never send a stop
event, so the counter keeps running on one old signal — that is what `hold` is
for: with `hold 5` a signal older than five seconds counts as ended.

## When it does not move anybody

`[p]micwatch status` answers it:

| Field | Meaning |
| --- | --- |
| `nothing tracked` | nobody qualifies right now: check "Not monitored", "Watched channels" and the mode |
| `Voice connection` / `Voice hook` | where the bot sits, and whether its hook is still the cog's (`mine`) |
| `Watched channels` | each watched channel with its human count; `← bot` marks the bot's channel — if it sits elsewhere it hears nobody |
| `Voice frames` | speaking stays at 0 while people talk = Discord relays nothing to this client |
| `Not monitored` | who is skipped and why (`higher role than mine`, `immune`, `rearm cooldown`, …) |
| `Move permission` | whether the bot has **Move Members** |

## Known limits

- **Green ring but no reaction** in `speak` mode means Discord relays no media
  state to that session. Try `[p]micwatch dave` (then `leave` + `join`), or fall
  back to `mode mic`, which works regardless.
- Voice connect timeouts (`Timed out connecting to voice`) are between the host
  and Discord's voice endpoint (UDP) — a host that tunnels or blocks UDP keeps
  timing out no matter what the cog does. The cog retries with a cool-down.
- A member counts as movable when their **highest role is not above the bot's**.
