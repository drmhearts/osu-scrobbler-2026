# osu! scrobbler

osu! music on Last.fm. Windows · stable + lazer · MIT · no analytics.

[Download](https://github.com/drmhearts/osu-scrobbler-2026/releases/latest/download/osu-scrobbler-windows-source.zip)

1. Install Python 3.11+ and [tosu](https://github.com/tosuapp/tosu/releases/latest).
2. Put tosu in `Desktop\tosu`. Extract this app into a permanent folder.
3. Double-click **START.cmd**. Connect Last.fm once, then open osu!.

| Key | Action |
| --- | --- |
| **i** | Track info |
| **f** | Force scrobble |
| **t** | Timer: half, full, or seconds |
| **b** | Toggle Windows startup |
| **u** | Update and restart |
| **?** | More controls |
| **q** | Hide the window |

Click the pink tray icon to reopen. **x** or tray → Exit stops the app.

Automatic scrobbles count main-menu listening and personal gameplay. Song selection
identifies music and allows force. Pauses, seeks and disconnects do not count.
Default timer: half the audio length. Settings and Last.fm authorization are remembered.
Unknown matches use map title/artist; missing album uses title again.

START checks verified releases and keeps the whole app current. No fresh install
is needed. Saved history and settings stay local; credentials use Windows Credential
Manager. No audio capture, analytics or gameplay uploads.

[Source](https://github.com/drmhearts/osu-scrobbler-2026) ·
[Details](https://github.com/drmhearts/osu-scrobbler-2026/tree/main/docs)
