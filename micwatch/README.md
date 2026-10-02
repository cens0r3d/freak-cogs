# MicWatch

Moves members to another voice channel when they keep their **microphone switched on** — or
actually **talk** — for longer than a configurable threshold (default: 30 s).

## What the bot can and cannot see

| Signal | Available without joining voice? | What it means |
|---|---|---|
| `voice.self_mute` / server `mute` (mode `mic`) | **yes** — voice state updates arrive for every voice channel | mic switched **on** (mute button state), not "is talking" |
| Speaking events (mode `speak`) | **no** — Discord relays them only to clients in the same voice channel | real voice activity (the green ring) |

`mic` mode needs no voice connection, no PyNaCl, no extra permissions beyond **Move Members**.
It also counts somebody sitting silently with an open mic.

`speak` mode makes the bot sit in the watched channel (joined muted + deafened so it never
shows up as a speaker) and read the `Speaking` frames (voice gateway opcode 5) from the voice
websocket. That requires voice support in the bot venv (`pynacl` + `davey`) and the **Connect** permission.

Known limit of `speak` mode: Discord only relays *transitions*. Someone who was already talking
when the bot joined the channel is picked up at their next start/stop, not retroactively.

## Install

Copy the folder to `~/.local/share/Red-DiscordBot/data/<instance>/cogs/CogManager/cogs/micwatch/`,
then `[p]unload micwatch` / `[p]load micwatch` (or `[p]load micwatch` the first time).
For `speak` mode, install voice support in the bot venv first:

```bash
pip install -U "Red-DiscordBot[voice]"        # pynacl + davey
# or, on a running bot:  [p]pipinstall pynacl davey
```

`pynacl` does the voice transport encryption, `davey` the Discord E2EE (DAVE) session that discord.py 2.6+ negotiates — since 2.6/2.7 discord.py refuses to build a `VoiceClient` when either one is missing.

Afterwards **restart the bot** (a cog reload is not enough): discord.py decides at import time whether voice is available, so a freshly installed library is invisible to the running process.

## Commands (`[p]micwatch`, aliases `micmon`, `mwatch`)

| Command | Effect |
|---|---|
| `toggle` | enable/disable, lists missing settings |
| `mode [mic\|speak]` | show/set the detection mode |
| `threshold [seconds]` | seconds before the move (default 30, 3–3600) |
| `grace [seconds]` | `speak`: tolerated silence before the counter resets (default 2, 0–60) |
| `rearm [seconds]` | how long a moved member is left alone (default 60, 0–3600) |
| `target [channel\|off]` | destination channel, `off`/`clear`/`aus` clears it |
| `watch add\|remove\|list\|clear` | monitored channels (empty = all, `mic` mode only) |
| `immune add\|remove\|list\|clear` | roles/members that are never moved |
| `notify` | toggle the announcement in the target channel |
| `resetmute` | `mic`: muting resets (on) or pauses (off) the counter |
| `ignorebots` | ignore other bots (default on, alias `bots`) |
| `message [text]` | announcement text; `{user}` `{mention}` `{seconds}` `{channel}` |
| `join [channel]` | pin the bot into a channel (needed for `speak`) |
| `leave` | disconnect / drop the pin |
| `settings` | configuration overview (aliases `show`, `config`) |
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

## How it works

* A 2 s `discord.ext.tasks` loop reconciles the tracked members with the live voice states
  (so enabling the cog or a reload does not lose anybody) and fires the moves.
* `mic` mode keeps a start timestamp per member; the counter runs while `self_mute`/`mute` are
  false, and is reset (or paused, see `resetmute`) when they mute or change channels.
* `speak` mode wraps `VoiceClient.ws._hook` — discord.py hands every voice gateway frame to it
  and does not handle opcode 5 itself — and keeps a per-member burst (`start`, `end`, `talking`).
  A silent gap longer than `grace` resets the burst.
* After a move the member is ignored for `rearm` seconds, so staff can move them back without an
  instant ping-pong, and a member who outranks the bot is skipped instead of raising.
* `speak` mode connects to the watched channel with members, follows them if they move to another
  watched channel, and disconnects when nobody is left.
