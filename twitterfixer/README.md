# twitterfixer

> **Fork / archive.** The original cog was published by **tombly**; that
> repository has been deleted. This copy is archived here so the code is not
> lost. Only `info.json` was rewritten — the logic is untouched.

Replaces Twitter and X links with an embed built from the vxtwitter API, so
posts render with text, author, date and engagement numbers instead of a bare
link or a broken preview.

There are **no commands**. The cog is a listener: it reacts to messages that
contain a Twitter/X status URL.

## What happens to a link

| Step | Detail |
| --- | --- |
| Detect | Regex over `twitter.com` and `x.com` status URLs, text messages only |
| Skip | Bots, DMs, and members without *Embed Links* |
| Fetch | Metadata from `api.vxtwitter.com` |
| Embed | Author, text, timestamp, thumbnail, likes / retweets / replies |
| Media | Photos are attached, videos and GIFs re-encoded with `ffmpeg` |
| Fallback | If the re-encoded file exceeds the server limit, the original message is replaced by a `vxtwitter.com` link |

The bot needs **Manage Messages** to delete its own placeholder embed, and
**Embed Links** in the channel.

## Requirements

* `ffmpy` (Python package, installed by `[p]cog install`)
* `ffmpeg` on the host — `apt install ffmpeg`. Without it, re-encoding fails and
  only the metadata embed is sent.

## Data source

`https://api.vxtwitter.com/status/<id>` — public, key-free. The link and its
metadata are sent there; the bot host's IP is visible to that service.

## Troubleshooting

* **Nothing happens at all** — the author must have *Embed Links*, the bot needs
  *Manage Messages*. Both are checked silently.
* **Embed appears but no video** — `ffmpeg` is missing on the host, or the media
  URL expired before download.
* **A plain vxtwitter link shows up** — that is the intended fallback for files
  above the upload limit, not an error.
* **Works in one channel, not another** — channel permissions differ. Check the
  bot's *Manage Messages* there.
* **Duplicate embeds after an edit** — Discord's own preview is cached; the cog
  only acts on new messages.

## Licence

The original repository is gone and shipped no licence file. This repository
only mirrors the code for preservation; the rights stay with the original author.
