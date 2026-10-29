# twitterfixer

> **Fork / archive.** Original by **tombly**; that repository has been deleted.
> Archived here so the code is not lost — only `info.json` was rewritten.

Replaces Twitter and X links with an embed, so posts render with text, author,
date and engagement numbers instead of a bare link or a broken preview. Photos
are attached, videos and GIFs are re-encoded and attached as files. If a file is
too large for the server, the message is replaced by a plain `vxtwitter.com`
link instead.

There are **no commands** — the cog is a listener for messages containing a
Twitter/X status URL.

## Requirements

* `ffmpy` (Python package, installed by `[p]cog install`)
* `ffmpeg` on the host — `apt install ffmpeg`. Without it, re-encoding fails and
  only the metadata embed is sent.
* **Manage Messages** for the bot (to replace its own placeholder) and
  **Embed Links** in the channel; the author needs *Embed Links* too.

## Troubleshooting

* **Nothing happens at all** — the author must have *Embed Links*, the bot needs
  *Manage Messages*. Both are checked silently.
* **Embed appears but no video** — `ffmpeg` is missing on the host, or the media
  URL expired before download.
* **A plain vxtwitter link shows up** — that is the intended fallback for files
  above the upload limit, not an error.
* **Works in one channel, not another** — check the bot's *Manage Messages* there.
* **Duplicate embeds after an edit** — Discord's own preview is cached; the cog
  only acts on new messages.

## Licence

The original repository is gone and shipped no licence file. This repository
only mirrors the code for preservation; the rights stay with the original author.
