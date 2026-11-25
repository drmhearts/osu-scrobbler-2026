# osu! life hacks 0.9.0

- New download setups prefer detected osu!lazer while keeping Stable available.
  Prompts show valid choices, examples and corrections; contextual help works
  without losing setup progress. Removed the terminal character art.
- Added `help TOPIC` for scrobbling, downloads, export, collections and the other
  controls. Reworked the README around setup and everyday use.
- Download count now accepts any positive whole number of beatmapsets. Large batches
  show a pacing/history warning and may still meet osu!'s server limits; saved
  progress, cooldowns and duplicate protection remain in place.
- Optional collection plans remember an existing or new name per batch and show
  sets awaiting import or in-game collection assignment. Neither client exposes a
  supported external collection write interface here, so assignment remains a
  clearly marked manual step and is never reported as automatically verified.

The regular lazer memory reader still targets 2026.1005.0 x64. Browser downloads
require Windows with installed Edge or Chrome. This release was tested with
isolated libraries and browser fixtures, not a player's live osu! account.

©Hky Sigh, 2026
