# osu! life hacks 0.7.0

- Last.fm joins MusicBrainz and Discogs as a read-only release source. API album
  tracklists and a bounded Featured On fallback verify actual track appearances.
- Unified scoring uses cross-provider consensus before choosing a release.
  Artist relationship and track membership outrank Last.fm listener popularity.
- Raw game credits remain separate from canonical credits. Incoming feat./ft./
  featuring uses the primary artist unless official metadata retains the guest.
  Album artist comes from release metadata only.
- Pending scrobbles omit unknown album fields and proceed on time. Track-title
  album fallback follows completed unresolved lookup, with Unknown release type.
- Short concurrent stages, persistent caches and optional bounded local diagnostics
  improve coverage without holding up playback or delivery.
- Fast provider chains get a short settling window before cancellation; Windows
  IPC cleanup retries briefly when another client is reading its control marker.
- Mocked regressions cover The Empress -> 暴君 and wotaku / イルミナティ ->
  アンティーク, release types, collaborations, compilations and source failures.

The Last.fm API uses the application key saved by authentication or
OSU_LIFE_HACKS_LASTFM_API_KEY. Public pages can be unavailable or change structure.
Last.fm-only formal release types stay Unknown until another source corroborates
them. Live public checks returned HTTP 406 and no API key was available for live
catalog verification; the named regressions pass against mocked provider responses.

Download osu-life-hacks-windows.zip for the standalone Windows application.
Legacy updater aliases, wheels, source package and SHA256 checksums are included.
Game-reader compatibility is unchanged: Stable signatures and lazer 2026.1005.0
x64 still need target-installation validation.

©Hky Sigh, 2026
