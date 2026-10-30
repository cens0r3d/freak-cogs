# shazam

Song recognition for Discord, built on [shazamio](https://github.com/shazamio/ShazamIO).
A rewrite of the unmaintained `shazam` cog — same job, fewer ways to fail.

**Credits:** the original `shazam` cog comes from
[Seina-Cogs](https://github.com/japandotorg/Seina-Cogs) by
[japandotorg](https://github.com/japandotorg) (MIT) — the command surface and the
idea are theirs. Recognition does
[shazamio](https://github.com/shazamio/ShazamIO) (MIT), which talks to Shazam's
reverse-engineered API. The API itself is Shazam's; this cog is not affiliated
with them.

## Commands

| Command | Description |
|---|---|
| `[p]shazam` | with a file attached, as a reply to a message with a file, or with a direct media URL |
| `[p]shazam <url>` | direct media URL (must point at audio/video, not at an HTML page) |
| `[p]shazam ... debug` | same, plus the windows that were tried in the embed footer |
| `[p]shazamver` | which version is loaded |

`[p]` is your prefix. Aliases: `sht`, `wasistdas`.

## Was anders ist als beim alten Cog

1. **Drei Fenster statt einem.** Erst die ersten 12 Sekunden, dann 12 Sekunden ab
   einem Drittel, zuletzt die letzten 12 Sekunden. Gleiche Länge, andere Lage —
   gemessen: ein *längeres* Fenster erkennt schlechter (mehr Rauschen pro
   Signatur), ein *anderes* Fenster hilft, wenn die Musik später einsetzt.
   12 s ist zugleich die Länge, die die Shazam-Apps selbst senden.
2. **Rohe Antwort statt Modellschicht.** Der alte Cog schickte die Antwort durch
   `shazamio`-Pydantic-Modelle; ein unvollständiger Payload wurde zum Traceback.
   Hier wird das Dict direkt gelesen, fehlende Felder sind einfach leer.
3. **Jeder Fehlerfall ist ein Satz.** Kein Audio, kein Treffer, abgelaufener
   Discord-Link, zu große Datei — alles landet als Nachricht im Channel.
4. **Content-Type-Prüfung bei URLs.** Ein abgelaufener Discord-CDN-Link liefert
   HTML; das wird abgefangen, bevor ffmpeg daran scheitert.
5. **Antworten auf alte Nachrichten.** Ist die referenzierte Nachricht nicht im
   Cache, wird sie per ID nachgeladen.
6. **Kein Tempfile.** Der Anhang wird über `attachment.read()` gelesen.

## Datenquelle

Shazam über die reverse-engineerte API, angesprochen mit `shazamio` 0.9.0
(Shazam-Core 2.0.0, Dekodierung und Signatur in Rust). Kein API-Key, kein Konto,
keine Kosten. Kein offizieller Zugang — Shazam kann die API jederzeit ändern.

`shazamio` 0.9.0 (26.10.2026) ist die erste Fassung, die auf dem aktuellen
Shazam-Endpoint neu gebaut wurde und mit 12-Sekunden-Fenstern arbeitet.
Core 2.0.0 dekodiert selbst (symphonia/rubato) und braucht kein ffmpeg mehr zum
Einlesen — ffmpeg bleibt nur für das Schneiden der Fenster nötig.

## Abhängigkeiten

* `shazamio` ≥ 0.9.0 (zieht `pydub`, `aiohttp`, `shazamio-core` ≥ 2.0.0 mit)
* `ffmpeg` im PATH — nur noch zum Schneiden der Fenster, das Dekodieren macht
  Shazam-Core seit 2.0.0 selbst

## Troubleshooting

| Symptom | Ursache | Lösung |
|---|---|---|
| „I could not recognize any song … start, middle, end: no match" | Musik zu leise, von Sprache überlagert, oder nicht in Shazams Datenbank | Ausschnitt nehmen, in dem die Musik klar vorne liegt |
| „that url returned text/html …" | Discord-Anhang-Link abgelaufen | Datei erneut hochladen und mit `[p]shazam` **antworten** |
| „I could not read any audio out of that file" | Bild, Textdatei oder kaputter Container | anderes Format probieren (mp3, m4a, ogg, opus, wav, mp4) |
| „that file is larger than 25 MB" | Discord-Limit | kürzen oder komprimieren |
| Timeout / „Recognition failed" | Shazam-API antwortet nicht | nochmal versuchen; das Log zeigt den Grund |

## Grenzen

* Nur der **erste** Anhang einer Nachricht wird geprüft.
* Kein Fortschrittsbalken, keine Warteschlange — eine Anfrage pro Aufruf.
* Keine lokale Datenbank: ohne Shazam-API erkennt der Cog nichts.
