# osu! life hacks 0.9.1

- Tidied the beatmap download menu into clear action groups, with color for the
  heading, job state, and progress.
- Added a live terminal progress bar for completed beatmapsets. The current
  transfer has an activity indicator while its total byte size is unknown.
- Kept the job count, queue, last saved set, pause/resume controls, and plain text
  display when terminal color is unavailable or disabled.

The bar counts completed full beatmapsets, not individual difficulties or bytes.
Downloads continue to use the same saved checkpoints and conservative pacing.

©Hky Sigh, 2026
