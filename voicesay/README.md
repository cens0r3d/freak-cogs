# VoiceSay

Sends an attached audio file as a **native Discord voice message** — the kind
with a play button and a waveform — to a text channel or a DM.

```
[p]voicesay #general        (with an audio file attached)
[p]voicesay @someone        (opens a DM first)
[p]voicesay 1234567890123   (channel or user id)
```

Text after the destination becomes the message content above the voice message:

```
[p]voicesay #voice-notes listen to this
```

## Install

```
[p]repo add freak-cogs https://github.com/cens0r3d/freak-cogs
[p]cog install freak-cogs voicesay
[p]load voicesay
```

**ffmpeg** and **ffprobe** are optional but recommended: without them only
`.ogg` and `.opus` attachments can be sent, and the waveform is a flat
placeholder. `apt install ffmpeg` covers both.

## The command

`[p]voicesay <channel|user|id> [text]` — no subcommands, no settings, no aliases.

- **Destination** — a `#channel`, an `@user` (the cog opens the DM), or a raw id,
  which is resolved as a channel first and as a user second.
- **Attachment** — only the **first** attachment of the message is used, and it
  must be an audio file (Discord has to report an `audio/*` type).
- **Text** — optional, sent along with the voice message.
- **Permissions** — the bot needs **Send Messages** and **Attach Files** in the
  target channel (`Attach Files` in the DM case is implied).

## Format

Anything that is not OGG Opus is re-encoded with `ffmpeg` to Opus at 32 kbit/s.
That is the voice-message format, so the result is fine for voice notes and
noticeably lossy for music — upload an `.ogg`/`.opus` file to skip the conversion.

## Troubleshooting

| Symptom | Cause / fix |
| --- | --- |
| "You need to attach an audio file" | The command was sent without an attachment |
| "The attachment must be an audio file" | Discord reported a non-audio type — a video file with audio in it counts as `video/*`, so extract the audio first |
| "ffmpeg is required to convert this audio file" | ffmpeg is missing and the file was not `.ogg`/`.opus`. Install ffmpeg, or upload OGG Opus |
| "Could not find a channel or user with that ID" | The id is wrong, or the user is not reachable for that lookup |
| "Could not open a DM" | The user has DMs closed or shares no guild with the bot |
| "I don't have permission to send messages or attachments" | **Send Messages** / **Attach Files** missing in the target channel |
| "Failed to send the voice message" | Discord rejected the upload — the bot log has the traceback. Usually a size limit or a broken container |

## Data

Nothing is stored — no settings, no per-user data, no copy of the audio. The
attachment is relayed to the destination and dropped afterwards, so `[p]mydata`
returns nothing for this cog.
