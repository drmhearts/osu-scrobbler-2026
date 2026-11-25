# osu! life hacks

osu! life hacks scrobbles the music you hear in osu! to Last.fm, looks up album and
release information, and downloads full beatmapsets in bulk. It supports osu!lazer
and osu!stable; when both are available, new download setups prefer lazer.

## Install and start

1. Download the [latest Windows package](https://github.com/drmhearts/osu-scrobbler-2026/releases/latest), extract it, and double-click **START.cmd**.
2. Connect Last.fm once when asked. The app remembers authorization in the OS credential vault.
3. Open osu! and listen. Use the terminal or pink tray icon for controls.

The package includes Python. Bulk downloads use an installed Edge or Chrome browser
and a separate, remembered osu! website sign-in. No tosu or external game reader is
needed. To run from source: `python -m pip install -e ".[test]"`, then
`python -m osu_scrobbler setup`.

## Everyday controls

| Terminal | Function |
| --- | --- |
| **i**, **v**, **s** | Track info, full details, watcher status |
| **f**, **t** | Force a scrobble, change listening timer |
| **e**, **a** | Export accepted history to UTF-8 text, toggle automatic export |
| **m** | Download beatmapsets; start, filter, pause, resume, cancel, import and scan |
| **r**, **d** | Connect Last.fm, configure optional Discogs release lookup |
| **b**, **u** | Toggle Windows startup, check/install an update |
| **h** | Help topics (including scrobbling, downloading, export and collections) |
| **q**, **x** | Hide the background console, stop the app |

The command line also accepts `osu-life-hacks help TOPIC` or
`python -m osu_scrobbler help TOPIC`. Try `help scrobble`, `help download`,
`help export`, or `help collections`. During a maps setup prompt, type `help` or
`help TOPIC` to see an explanation without losing your place; type `back` to leave
the current form with saved settings intact.

## What each function does

- **Scrobble/watch:** Main-menu music and your own gameplay count after the chosen
  listening threshold. Selection alone, pauses, seeks, replays and spectating do
  not count. `watch --dry-run` previews without submitting. `force` submits the
  selected track immediately. Last.fm setup uses `auth` or **r**.
- **Track details and corrections:** **i**/**v** show the selected track and release
  candidates. `config choose N` saves a numbered candidate; `config correct` saves
  a manual title, artist and optional release data. `config clear-correction`
  removes it. MusicBrainz, Discogs and Last.fm are used for release matching.
- **Timer and status:** **t** accepts `h` (half song), `f` (full song), or seconds.
  **s**/`status` shows selection, progress and delivery state.
- **Download:** **m** opens maps. Pick a detected destination or type `custom`, sign
  in once to osu!, set optional filters, then start. Default: 25 recently ranked
  osu!standard beatmapsets, newest first. Count is a positive whole number of
  **beatmapsets**, with no fixed maximum. A full set may include other modes or
  difficulties. Jobs save their queue, completed IDs and cooldowns; pause, cancel
  and restart can be resumed. The terminal shows a live bar for completed sets and
  an activity indicator while an archive transfers. See [download setup and filters](docs/downloads.md).
- **Collections:** When starting a batch, optionally choose an existing or new
  collection name. The app records which downloaded sets still need import and
  collection assignment. Add them through the game's Manage Collections UI after
  import; `maps collection` lists the pending sets. The app does not write client
  collection databases or claim an unverified assignment.
- **Export:** **e** or `export [file.txt]` writes accepted history as UTF-8 text.
  Without a path, it uses the saved `export_path` or `scrobbles.txt` in the app
  data folder. **a** toggles automatic export; `config set export_path PATH` sets
  its destination.
- **Queue:** `queue list` shows Last.fm delivery history. `queue retry ID` retries
  a failed delivery; uncertain submissions require `--accept-duplicate-risk`.
- **Startup/tray:** **b** or `tray --install` enables the Windows sign-in monitor;
  `tray --uninstall` disables it. `console` opens the live terminal. Closing the
  console leaves the tray running; **x**/`quit` stops the app.
- **Updates and privacy:** **u**/`update --check` checks for releases; `update`
  installs one and `update --rollback` restores the prior install. `privacy`
  explains local data and network requests; `privacy --clear-cache` preserves
  history and corrections. `config show` lists non-secret settings.

## Troubleshooting and limits

Live game reading requires Windows x64. The internal Stable reader uses signatures;
the lazer memory reader currently targets **2026.1005.0 x64**. An unsupported game
build waits with a compatibility message. See [reader diagnostics](docs/telemetry.md).

Downloads use osu!'s normal website and full-media archive link. Website changes,
browser verification, an expired session or server rate limits can pause a job.
Conservative local pacing and the saved request ledger cannot guarantee avoidance
of limits, especially if the account downloads elsewhere. Lazer imports are handed
to the client and remain **queued** until confirmed there. Collection membership
is a separate manual step. See [download recovery](docs/downloads.md),
[privacy](docs/privacy.md), and [updates](docs/updates.md).

©Hky Sigh, 2026
