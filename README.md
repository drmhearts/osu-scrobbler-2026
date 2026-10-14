# osu! scrobbler

A small Windows app: **osu! stable / lazer → tosu → MusicBrainz → Last.fm**.
Identifies music when you select a map. [MIT licensed](LICENSE); no analytics.
[Privacy and data removal](docs/privacy.md).

[Download for Windows](https://github.com/drmhearts/osu-scrobbler-2026/releases/latest/download/osu-scrobbler-windows-source.zip)

## Install

1. Install Python **3.11+** and extract [tosu for Windows](https://github.com/tosuapp/tosu/releases/latest)
   into `Desktop\tosu`. Extract this app into its permanent folder.
2. Double-click **`START.cmd`**. It creates the environment, installs dependencies,
   and guides Last.fm authorization. Choose whether to start at Windows sign-in.
3. When setup succeeds, the terminal clears and shows the welcome screen. Open
   osu! stable or lazer, select a map, and play. No commands need typing.

Last.fm application key/secret and browser approval are needed once. Future clicks
reuse the saved setup. Startup installs verified published updates automatically.
Settings, history and Last.fm authorization are preserved.
[Update details](docs/updates.md).

## Background startup

Choose **y** during setup for Windows sign-in startup. To enable it later,
double-click **`enable-background.cmd`**; it also handles missing environments.
The tray monitor starts scrobbling when osu! opens.

Click the **pink icon near the clock** (check the `^` hidden-icons menu) to show
the terminal. Closing that window or pressing **q** hides it; scrobbling continues.
Right-click → **Exit** to stop. **`disable-background.cmd`** stops the monitor and
removes startup. Keep the project at its installed location.

The launcher finds `Desktop\tosu\tosu.exe` automatically. For another location:

```powershell
.\.venv\Scripts\python.exe -m osu_scrobbler config set tosu_path "C:\path\to\tosu.exe"
```

It starts/stops its owned tosu process; existing instances are reused.
[Background details](docs/background.md).

## Manual use

Keep **tosu** and **osu! stable or lazer** running. From the same project folder:

```powershell
.\.venv\Scripts\python.exe -m osu_scrobbler auth
.\.venv\Scripts\python.exe -m osu_scrobbler watch
```

`auth` runs once: enter your Last.fm key/secret and approve in the browser.
The key, secret and session stay in Windows Credential Manager and reload
automatically at background startup and after reboots. `watch --dry-run` previews without
sending to Last.fm; MusicBrainz searches still happen. No osu! login is needed.

The terminal opens with a welcome banner, GitHub link and key controls,
then shows track and scrobble messages. Keys while watching:

| Key | Action |
| --- | --- |
| **f** | Force the selected track immediately |
| **i** / **v** | Brief info / full recording and release details |
| **e** | Export successful scrobbles |
| **a** | Toggle automatic export; remembers the setting |
| **q** | Quit manual watch / hide background terminal |

## Commands

Prefix commands below with `.\.venv\Scripts\python.exe -m osu_scrobbler`.
Run them from a second terminal in the project folder while watching.

| Command | Action |
| --- | --- |
| `tray --install` / `tray --uninstall` | Enable / remove background startup |
| `console` | Show the background terminal |
| `status` | Current track, timer, and match candidates |
| `info [--full]` | Brief info or complete available metadata and links |
| `force` | Submit immediately; shows the exact metadata |
| `export [file.txt]` | Export accepted history; also works with watcher stopped |
| `config set auto_export true` | Enable automatic export |
| `config set export_path "C:\Users\you\Documents\scrobbles.txt"` | Set export destination |
| `config set threshold_mode standard` | Switch to Last.fm listening criteria |
| `config choose 1` | Save a numbered match candidate |
| `config correct --title "Song" --artist "Artist" --album "Album"` | Save a correction |
| `config clear-correction` | Remove the current correction |
| `config show` / `queue list` | Settings / submission history |
| `queue retry ID --accept-duplicate-risk` | Explicitly retry an uncertain delivery |
| `privacy [--clear-cache]` | Explain data use / clear cache and older activity logs |
| `update [--check]` / `update --rollback` | Install/check a release / restore the previous version |
| `config set auto_update false` | Disable automatic update checks |

Use `--json` **before** commands for raw output. `--data-dir PATH` selects a profile;
default: `%LOCALAPPDATA%\osu-scrobbler`. Exports are UTF-8, tab-separated text.

## Behavior

- Default: scrobble after **29 seconds of active listening**, in gameplay, the
  main menu or song selection. Menu audio must have an advancing playback clock;
  paused audio, seeks and disconnects do not count. Every retry/loop has its own timer.
- The default and unplayed force are **custom behavior**. Standard mode requires
  duration over 30 seconds and half the track or 4 minutes, plus a 29-second minimum.
- Successful force suppresses the next attempt of that track. A later attempt
  needs 29 seconds of fresh play **and** 29 seconds since the previous success.
- Lookup never blocks scrobbling: missing, ambiguous, slow or unavailable matches
  use the beatmap title/artist. Unknown albums use **track title**; album artist
  defaults to artist. Candidates and saved corrections remain optional.
- Offline submissions stay queued. Uncertain outcomes require explicit retry;
  Last.fm cannot guarantee exactly-once delivery. Export failures never resubmit.
- Lazer requires matching local/player names, compared only in memory. Replays and
  spectating do not count. Mid-song attachment waits for a new start. [Details](docs/telemetry.md).

No-network demo (two previews, none sent):

```powershell
.\.venv\Scripts\python.exe -m osu_scrobbler --data-dir .demo-data watch --dry-run --fixture .\tests\fixtures\scrobble_demo.json
```

More: [matching](docs/musicbrainz.md) · [delivery and export](docs/lastfm.md).
