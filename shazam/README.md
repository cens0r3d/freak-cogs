# shazam

Song recognition for Discord, built on [shazamio](https://github.com/shazamio/ShazamIO).

| Command | What it does |
| --- | --- |
| `[p]shazam` | recognize the attached file, the message you replied to, or a media URL |
| `[p]shazam <url>` | recognize a direct audio or video URL |
| `[p]shazam <url> debug` | same, plus the windows that were tried, in the footer |
| `[p]shazamver` | version of the cog and of shazamio |

Attach a file, reply to a message that has one, or pass a URL that points
straight at audio or video. Discord attachment links expire after a while - reply
to the message instead of copying its URL.

Three 12 second windows are tried: the start, a third of the way in, and the last
12 seconds. Same length, different position - 12 s is what the Shazam apps send,
and a different position covers the case where the music only starts later.

No API key, no account, no cost. The API is reverse-engineered, so it can change
without notice; `shazamio` 0.9.0 or newer is required.

| Symptom | Cause |
| --- | --- |
| no match on all three windows | music too quiet or buried under speech |
| `that url returned text/html` | expired Discord link, reply with the file instead |
| `could not read any audio` | image, text file or broken container |
| `Recognition failed` | Shazam did not answer, try again in a moment |

Credits: the original `shazam` cog comes from
[Seina-Cogs](https://github.com/japandotorg/Seina-Cogs) by japandotorg (MIT).
Recognition is [shazamio](https://github.com/shazamio/ShazamIO) (MIT). This cog
is not affiliated with Shazam.
