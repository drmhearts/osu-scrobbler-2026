# osu! life hacks

Listen in osu!, identify songs with MusicBrainz, and scrobble to Last.fm.

1. Extract the Windows package and double-click **START.cmd**.
2. Connect Last.fm once and choose whether to start with Windows.
3. Open osu! Stable or a supported osu!lazer release. Click the pink tray icon for controls.

The Windows package includes Python. The app reads game state directly; no
external game reader, helper download, overlay, or local WebSocket server is needed.
Live reading requires Windows x64. Stable uses signature checks; lazer currently
targets **2026.1005.0 x64**. Other lazer releases wait with a compatibility message.
These internal readers need live validation on the target installation; see
[reader compatibility and diagnostics](docs/telemetry.md).

| Key | Action |
| --- | --- |
| **i** / **v** | Track info / full details |
| **s** / **f** | Status / force scrobble |
| **t** | Timer: half, full, or seconds |
| **e** / **a** | Export / toggle automatic export |
| **r** / **d** | Connect Last.fm / optional Discogs token |
| **b** | Toggle Windows startup |
| **u** | Update and restart |
| **q** / **x** | Hide console / stop app |

All controls appear on the normal welcome screen. In a foreground `watch`
session, **q** quits. Later START launches stay in the tray.

Main-menu music and personal gameplay count toward automatic scrobbling. Song
selection identifies music and permits force; it does not count automatically.
Pauses, seeks, replays, spectating, and unreadable intervals do not count.
MusicBrainz and Discogs identify albums, EPs and compilations using verified
track membership and confidence ranking. Unmatched releases remain unknown.
See [matching and optional Discogs setup](docs/musicbrainz.md). Authentication, queues, history and preferences survive upgrades.

No audio capture, analytics or gameplay uploads. See [privacy](docs/privacy.md),
[background operation](docs/background.md), and [updates](docs/updates.md).

For development: `python -m pip install -e ".[test]"`, then `python -m pytest`.
The command is `osu-life-hacks`; `python -m osu_scrobbler` remains supported.

©Hky Sigh, 2026
