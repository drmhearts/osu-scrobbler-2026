## Try osu! scrobbler 0.2.6

Automatic scrobbling now applies **only to the main menu and personal gameplay**.
Song selection and multiplayer lobbies still identify songs and allow manual
**force**, but never submit automatically.

The threshold is now **half the song's length**, with no 29-second minimum or
four-minute cap. The actual osu! audio length takes priority over database length.
Unknown duration waits for identification; force remains available. Pauses, seeks
and disconnect gaps do not count. Existing saved threshold settings migrate
automatically, and duplicate protection remains in place.

Includes verified tray startup and beatmap metadata fallback when MusicBrainz
cannot identify a song. Album defaults to track title; album artist defaults to
artist. Saved authorization, settings and history are preserved.

Download **osu-scrobbler-windows-source.zip** below and extract it into a permanent folder.

1. Install Python 3.11+ and extract [tosu](https://github.com/tosuapp/tosu/releases/latest) into `Desktop\tosu`.
2. Open the extracted scrobbler folder and double-click **START.cmd**.
3. Follow the one-time Last.fm authorization prompts and choose whether to start with Windows.
4. Setup clears the terminal and shows the welcome screen. Open osu! stable or lazer and play a map.

No environment creation or pip commands need to be typed. API keys and authorization
are remembered in Windows Credential Manager. Click the pink tray icon to reopen
the terminal; closing the view keeps background scrobbling running. Use the icon's
**Exit** menu to stop it.

The automatic threshold is half the song in active gameplay or main-menu listening.
Automatic updates use verified published releases and preserve settings/history.
No analytics, tracking IDs or gameplay uploads are included. Source is MIT licensed.

This is an early testing release distributed as Python source, not a standalone
Windows executable. Native Windows setup/tray behavior and live lazer still need
broader validation. Automated tests cover setup, timing, privacy, delivery, updates
and rollback. See the README and `docs` folder for details.

The `.whl` asset is used by the updater. `SHA256SUMS.txt` contains checksums for
both downloads.
