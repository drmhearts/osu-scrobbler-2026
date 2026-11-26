osu! life hacks
===============

Scrobble music from osu! to Last.fm, identify releases, and download full
beatmapsets in bulk. Supports osu!lazer and osu!stable. New download setups
prefer lazer when both clients are available.

GET STARTED

1. Download the latest Windows ZIP from:
   https://github.com/drmhearts/osu-scrobbler-2026/releases/latest
2. Extract the ZIP and double-click START.cmd.
3. The first launch opens a visible setup screen. Connect or skip the osu!
   public API and Last.fm separately, then choose whether to start with Windows.
   Last.fm authorization opens in your normal default browser. API secrets
   are saved in the Windows credential vault.
4. The terminal stays visible after setup, even when Windows startup is on.
   Open osu! and listen. Use the terminal or pink tray icon for controls.

The ZIP includes Python. Firefox bulk downloads use a small add-on in your
regular Firefox profile; Chrome and Edge use a separate remembered profile.
Firefox does not need geckodriver or a downloaded browser for this feature.
Public map searches and library scans do not need sign-in, but osu! requires it
for full archive downloads. A public osu! API connection cannot authorize
archive downloads. No external game reader is
needed. To run from source, install with:
   python -m pip install -e ".[test]"
   python -m osu_scrobbler setup

EVERYDAY CONTROLS

  i / v / s   Track info, full details, watcher status
  f / t       Force scrobble, change listening timer
  e / a       Export accepted history, toggle automatic export
  m           Download maps: start, filter, pause, resume, cancel, import, scan
  r / o / d   Connect Last.fm, osu! public API, optional Discogs lookup
  b / u       Toggle Windows startup, check/install an update
  h           Help topics: scrobbling, downloading, export, collections
  q / x       Hide the console, stop the app

The command line also accepts "osu-life-hacks help TOPIC" or
"python -m osu_scrobbler help TOPIC". During a maps setup prompt, type "help"
or "help TOPIC" without losing your place; type "back" to leave the form.

SCROBBLING AND RELEASES

Main-menu music and your own gameplay count after the chosen listening
threshold. Selection alone, pauses, seeks, replays and spectating do not count.
MusicBrainz, Discogs and Last.fm are used for release matching. Track info and
full details show candidate releases. A numbered candidate or manual metadata
correction can be saved through the config command. Unmatched releases remain
unknown. See docs/musicbrainz.md for more.

DOWNLOADS

Open maps with "m". In Firefox, first choose "p Pair Firefox". In Firefox,
open about:debugging, choose This Firefox > Load Temporary Add-on, and select
firefox-extension\manifest.json beside START.cmd. Paste the code from the app
into the add-on's toolbar popup, then sign in to osu! normally in Firefox.
Temporary add-ons must be loaded again after each Firefox restart; permanent
installation requires Mozilla signing. Chrome/Edge need no add-on.

Pick a destination or type "custom", sign in once to osu!,
set optional filters, then start. The default is 25 recently ranked standard
beatmapsets, newest first. The count measures full beatmapsets, which may include
other modes and difficulties. Jobs save queues, completed IDs and cooldowns.
Pause, cancel and restart can be resumed. The terminal shows a live progress
bar and transfer activity. Firefox keeps its original archive under its normal
Downloads\osu-life-hacks folder; the app validates a copy before import.
A library scan error appears before browser sign-in.
See docs/downloads.md for setup, filters and recovery.

At batch start, you can choose an existing or new collection name. The app
records which downloaded sets still need import and collection assignment.
Assign them through the game's Manage Collections UI after import. The app
does not write into either client's collection database.

OTHER COMMANDS

"export [file.txt]" writes accepted history as UTF-8 text. "queue list" shows
Last.fm delivery history. "tray --install" enables sign-in startup and
"tray --uninstall" disables it. "update --check" checks for a release;
"update --rollback" restores the prior runtime. "privacy" explains data and
network requests. "config show" lists non-secret settings. Run "setup" again
to revisit both connections, or "osu-api preview" to check the public API.

UNINSTALL

Double-click UNINSTALL.cmd in the extracted Windows folder. It stops the app,
removes its Windows sign-in startup entry and its cached runtime. Then delete
the extracted program folder shown by the command. Your osu! libraries, saved
app data, downloads, history and credentials are preserved. See docs/updates.md.

TROUBLESHOOTING

Live game reading requires Windows x64. The Stable reader uses signatures; the
lazer memory reader currently targets 2026.1005.0 x64. An unsupported build
waits with a compatibility message. See docs/telemetry.md for diagnostics.

Downloads use osu!'s normal website and full-media archives. Website changes,
browser verification, expired sign-in or server rate limits can pause a job.
Firefox downloads use your regular session and browser cookies without copying
them into the app. Website verification or rate limits can still pause a batch;
the add-on cannot bypass them. Public osu! API credentials cannot authorize
archives.
Local pacing cannot guarantee avoidance of limits. Lazer imports remain queued
until confirmed there. Collection membership is a separate manual step.
See docs/privacy.md and docs/updates.md.

©Hky Sigh, 2026
