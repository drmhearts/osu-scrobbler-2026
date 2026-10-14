# Background

Open **START.cmd**. Press **b** to toggle Windows sign-in startup. The tray monitor
starts watching when osu! opens. Press **q** to hide the view, click the pink icon
to reopen it, and press **x** or tray → Exit to stop.

Only this app's HKCU startup entry changes; no administrator rights or services.
Python is bundled; tosu downloads from its official release with a pinned SHA256.
The managed tosu runs hidden with its browser and overlays disabled. An already
running tosu is reused without changing its settings. Only owned children stop
when osu! closes. Stable and lazer are detected by process name, regardless of
their installation folder; no drive scan is needed.

The runtime is copied to `%LOCALAPPDATA%\osu-scrobbler`; Windows startup uses
that copy. The extracted download can be removed after setup. Its local
**START.cmd** also remains available in that folder. Subsequent starts stay in
the tray. Choosing Windows startup closes the first-time setup window after the
welcome; clicking the icon opens it again.
