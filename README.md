# osu! scrobbler

Title. Also a metadata extractor!

[Download for Windows](https://github.com/drmhearts/osu-scrobbler-2026/releases/latest/download/osu-scrobbler-windows.zip)

1. Extract the download and double-click **START.cmd**.
2. Connect Last.fm once and choose whether to start with Windows (recommended).
3. Open osu! (stable or lazer). If you want to use commands or configure anything, click the pink icon in your tray.

Python is included. tosu (what is used to see the song/map playing) installs automatically and runs hidden, without its
browser or overlay. Any osu! install location works. The app installs in your
local user folder.

| Key | Action |
| --- | --- |
| **i** | Track info |
| **f** | Force scrobble |
| **t** | Timer: half, full, or seconds |
| **b** | Toggle Windows startup |
| **u** | Update and restart |
| **?** | More controls |
| **q** | Hide the window |

Later starts stay in the tray. **x** or tray → Exit stops the app.

Main menu and gameplay trigger automatic scrobbling after at least half the song has been played. In song selection,
you can still identify tracks and force scrobble / grab metadata. Pauses, seeks and disconnects do not count.
I try to use musicbrainz to find a matching album, but if it's not found, the track will be treated as a single.

No audio capture, analytics or gameplay uploads.

[Source](https://github.com/drmhearts/osu-scrobbler-2026) ·
[Details](https://github.com/drmhearts/osu-scrobbler-2026/tree/main/docs)
