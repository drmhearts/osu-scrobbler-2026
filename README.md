# osu! scrobbler

osu! music on Last.fm. Windows · stable + lazer · MIT · no analytics.

[Download for Windows](https://github.com/drmhearts/osu-scrobbler-2026/releases/latest/download/osu-scrobbler-windows.zip)

1. Extract the download and double-click **START.cmd**.
2. Connect Last.fm once and choose whether to start with Windows.
3. Open osu!. Click the pink tray icon when you want commands.

Python is included. tosu installs automatically and runs hidden, without its
browser or overlay. Any osu! install location works. The app installs in your
local user folder; no administrator access is needed.

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

Automatic scrobbles count main-menu listening and personal gameplay. Song selection
identifies music and allows force. Pauses, seeks and disconnects do not count.
Default timer: half the audio length. Settings and Last.fm authorization are remembered.
Unknown matches use map title/artist; missing album uses title again.

START checks verified releases and keeps the whole app current. No fresh install
is needed. Saved history and settings stay local; credentials use Windows Credential
Manager. No audio capture, analytics or gameplay uploads.

[Source](https://github.com/drmhearts/osu-scrobbler-2026) ·
[Details](https://github.com/drmhearts/osu-scrobbler-2026/tree/main/docs)
