# Windows background mode

`START.cmd` prepares the app and asks whether to enable sign-in startup.
It guides authorization once, clears successful installer output, and opens the
welcome terminal. Future clicks reuse setup. `enable-background.cmd` explicitly
enables startup, including on fresh installations.

`tray --install` registers this profile in the current user's sign-in startup,
then launches a hidden `pythonw.exe` tray monitor. No administrator rights or
changes to osu! shortcuts are needed. The project/venv must remain at the same
path; reinstall startup after moving them.
Before watching, the launcher checks published releases and applies verified
updates in a separate environment. Offline or failed installs keep the working
version. No new download or Last.fm authorization is needed for later releases.
[Updates and rollback](updates.md).

The monitor checks process names once a second (`osu!.exe`, `osulazer.exe`, `tosu.exe`).
Other names are discarded immediately; command lines and process history are not read.
It starts one watcher when a game appears, reuses an existing watcher, and stops
its owned watcher after all game processes close. A tray lock and watcher lock
prevent duplicate instances. Already-running games are detected as well.

Click the icon to open/focus the terminal. It uses authenticated loopback IPC and
up to 50 in-memory messages, expiring after five minutes; closing it never sends `quit`. The keys perform the same
commands as manual watch, with `s` for status and `q` to hide. Right-click **Exit**
stops background monitoring; `tray --uninstall` removes sign-in startup and stops
this profile's monitor. Older monitors must be stopped through their **Exit** menu.

When necessary, the monitor starts the configured `tosu_path` (or Desktop/tosu).
It terminates only that owned child, never osu! or a pre-existing tosu. If no tosu
is found, the watcher waits for it and the terminal explains how to configure it.
The saved Last.fm key, secret and session reload automatically from the OS vault
whenever the watcher starts, including after Windows sign-in/reboots. The visible
terminal shows a welcome banner, GitHub link and short command guide. History and
export settings reuse the normal data directory.
The scrobbler writes no activity logs. Its owned tosu launches with `--update=false`;
tosu still performs its own GitHub update check. [Privacy and removal](privacy.md).

Tests cover startup quoting/removal, game lifecycle, ownership, duplicate guards,
terminal IPC/hide, and temporary message retention using mocks/local sockets. This Linux
cloud cannot test the actual Windows tray/sign-in UI; a Windows smoke test should
check icon click, closing the console, game restart, and sign-in startup. Lazer
telemetry is source-backed and fixture-tested; live validation remains required.
