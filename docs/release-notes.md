# osu! life hacks 0.5.0

- Internal read-only Stable and lazer readers replace the external reader, its
  download/setup code, WebSockets and child-process supervision.
- Lazer targets Windows x64 2026.1005.0; unsupported layouts wait safely.
- New application/package branding, compact artwork, complete command menu,
  and the attribution below. The application no longer displays a source link.
- Existing Last.fm credentials, profile data, timers, tray startup and update
  verification remain in place. Legacy release aliases allow older updaters to migrate.

Download `osu-life-hacks-windows.zip` after this version is published. Source and
wheel packages remain available. No game-reader download is required. Internet
is used for Last.fm, MusicBrainz and application updates.

The internal layouts have synthetic regression coverage, not live gameplay
validation yet. See [compatibility and manual release checks](telemetry.md).
The source license and dependency licenses are preserved.

©Hky Sigh, 2026
