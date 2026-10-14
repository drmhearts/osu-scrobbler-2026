# Background operation

Open **START.cmd**. **b** toggles Windows sign-in startup; **q** hides the console;
click the pink tray icon to reopen it; **x** or tray → Exit stops the app.

The tray starts one watcher when osu! opens and stops that watcher when all game
processes exit. Readers run inside the watcher. No external reader is installed,
launched, reused or stopped. Python is bundled in the Windows download.

Only this app's HKCU startup entry changes. No administrator rights or service
are required. Startup and credentials retain their existing internal identifiers
so rebranding does not duplicate startup entries or require reauthorization.
The profile/runtime remains `%LOCALAPPDATA%\osu-scrobbler` for migration safety.
The visible title and package/command name are osu! life hacks / `osu-life-hacks`.
The extracted download can be removed after setup; the installed START remains.
