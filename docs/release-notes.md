# osu! life hacks 0.8.0

- Added a persistent bulk beatmap downloader under m maps, the tray menu and the
  maps CLI. Defaults to 25 recently ranked osu!standard beatmapsets, newest first.
- Optional mode, genre, category, inclusive UTC ranked-date, successful set-count,
  full difficulty-length and host mapper filters. Mode/length must match the same
  difficulty; a full set may contain additional difficulties and modes.
- Dedicated visible Edge/Chrome sign-in with normal Windows-protected session
  storage; no game-token extraction or browser-verification bypass.
- Read-only Stable/lazer library deduplication, transactional queues/history,
  validated full-media archives and exclusive placement. Lazer uses its own
  archive importer; downloaded, queued and confirmed-import states stay distinct.
- Separate background worker, shared persistent conservative pacing/cooldowns,
  pause/resume/cancel and crash recovery. Tray Exit and updates pause transfers.
  Server limits can still apply, including downloads made elsewhere.
- Updated Pillow to 13.x for published security fixes; other direct dependencies
  were checked against current releases. Reader offsets remain unchanged: regular
  lazer 2026.1005.0 is supported; tachyon builds require independent validation.
- Added download setup/recovery/privacy documentation and tools/publish.ps1 for
  maintainers to test, push and publish through the existing CI release pipeline.

Windows x64 with installed Edge/Chrome is required for browser downloads. The
Windows package bundles Python and the browser driver; no separate browser is
installed automatically. Website format or verification changes can pause jobs.
Lazer's retained hash-store files cause conservative duplicate skips; queued import
is not proof of active database membership. Confirm imports in the game.

Tests use isolated libraries and routed browser fixtures. No live sign-in,
account quota or game import was exercised against a player's library.

©Hky Sigh, 2026
