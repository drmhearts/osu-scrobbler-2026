## osu! scrobbler 0.3.0

- One launcher: **START.cmd**. Startup, updates and controls are built in.
- Minimal terminal, soft color and a small Reimu welcome.
- **t** → half, full, or custom seconds. Half is the default; choices are saved.
- Fixed whole-app update activation, running-app restarts and API-rate-limit fallback.
- Known older launchers refresh automatically; user-modified files are preserved.

Download **osu-scrobbler-windows-source.zip**, extract, and open **START.cmd**.
Python 3.11+ and [tosu](https://github.com/tosuapp/tosu/releases/latest) are required.
Existing users can reopen START or press **u**. Authorization, settings and history
are preserved. Main menu/gameplay count automatically; song selection supports force.

MIT licensed. No analytics or audio capture. The wheel is for the updater;
`SHA256SUMS.txt` verifies both downloads.
