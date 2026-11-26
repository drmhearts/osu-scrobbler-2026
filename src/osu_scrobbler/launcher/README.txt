osu! life hacks
===============

Extract the Windows ZIP and double-click START.cmd. Connect Last.fm once,
choose Windows startup if you want it, then open osu!stable or osu!lazer.
The pink tray icon and terminal provide controls.

The Windows package includes Python. It reads game state directly, without
an external game reader. Live reading requires Windows x64. Stable uses
signature checks; the lazer reader currently targets 2026.1005.0 x64.
See docs/telemetry.md for reader compatibility.
Firefox map downloads use the add-on in your regular Firefox profile. In the
maps menu, choose p to pair it; load firefox-extension/manifest.json through
Firefox's about:debugging first. Chrome and Edge use a separate browser profile.
Full archives still require an osu! website sign-in.

  i / v / s   Track info, details, watcher status
  f / t       Force scrobble, listening timer
  e / a       Export history, automatic export
  m           Download maps
  r / d       Last.fm, optional Discogs token
  b / u       Windows startup, update
  h           Help
  q / x       Hide console, stop app

Double-click UNINSTALL.cmd in the extracted Windows folder to stop the app
and remove its startup entry and cached runtime. Delete that extracted folder
afterward. Saved app data and osu! libraries remain.

©Hky Sigh, 2026
