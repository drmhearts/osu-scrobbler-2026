# osu! life hacks 0.6.0

- MusicBrainz and Discogs reconcile real albums, EPs, compilations, soundtracks
  and mixtapes before single fallback. Missing classification stays Unknown.
- MusicBrainz browses recording releases beyond its limited embedded list.
  Dedicated Discogs EP/compilation searches check actual tracks and artist credits.
- Deterministic confidence ranking respects identifiers, release hints, duration,
  track position, dates and editions; weak title-only matches are rejected.
- Release types, dates, positions, IDs, source evidence and alternatives are kept.
  Corrections take priority and source outages remain independent.
- Anonymous Discogs lookup works where supported. Optional `discogs auth` or **d**
  stores a personal access token securely in the OS credential vault.
- Bounded requests, rate-limit cooldowns and versioned caches limit background
  work. Existing Last.fm, playback timing and game readers retain their behavior.

Download `osu-life-hacks-windows.zip` for the standalone Windows application.
Legacy updater aliases, wheels, source package and SHA256 checksums are included.
See [matching rules and limits](https://github.com/drmhearts/osu-scrobbler-2026/blob/v0.6.0/docs/musicbrainz.md).
Catalog gaps and ambiguous versions can still require manual choice. Public API
access was checked live; matching regressions use mocked responses. Game-reader
compatibility limits remain the same as 0.5.0.

©Hky Sigh, 2026
