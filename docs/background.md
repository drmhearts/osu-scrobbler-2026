# Background

Open **START.cmd**. Press **b** to toggle Windows sign-in startup. The tray monitor
starts watching when osu! opens. Press **q** to hide the view, click the pink icon
to reopen it, and press **x** or tray → Exit to stop.

Only this app's HKCU startup entry changes; no administrator rights or services.
The launcher finds `Desktop\tosu\tosu.exe` or `Downloads\tosu\tosu.exe` and owns only
processes it starts. A running tosu is reused. Set another path with:

```powershell
.\.venv\Scripts\python.exe -m osu_scrobbler config set tosu_path "C:\path\tosu.exe"
```
