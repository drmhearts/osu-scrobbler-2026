# Recording and release matching

MusicBrainz and Discogs are independent, read-only sources. MusicBrainz supplies
canonical recording IDs, credits and release membership. Discogs adds verified
tracklist appearances, including EPs and compilations. Search summaries alone
never establish that a song belongs to a release.

## Confidence and release selection

Recording confidence uses normalized title (50%), artist/featured credits (38%),
duration (10%) and search relevance (2%). Title and artist must both match strongly,
and confidence must reach 0.86. Unicode, transliteration and recording aliases
are supported. Explicit remix/live/instrumental versions must agree. Game/TV cuts
can match longer recordings, with that difference retained. Generic Discogs titles
such as Intro need stronger evidence. Exact recording IDs/ISRCs take priority;
an unrelated Discogs title cannot override a known MusicBrainz recording ID.

Embedded MusicBrainz releases are limited to 25 and ordered by identifier. We
additionally browse up to 100 releases for the verified recording and inspect up
to three distinct release groups' tracklists. Discogs reserves one general search
and one each for EPs and compilations (20 results each), then verifies up to four
unique release/master candidates. Duplicate single pressings cannot consume the
whole search budget. Compilations require matching per-track artist credits;
Various Artists is never recording evidence. Headings are ignored; subtracks are
checked independently.

Only safe recording/release associations enter deterministic ranking:

1. Explicit release/release-group identifiers, then a strong release-title hint.
2. Recording confidence, with small bonuses for release-title, track-position
   and date agreement; near-equal scores are compared in 0.02 bands.
3. Official and real releases; supported Album/EP/Compilation/Soundtrack/Mixtape
   releases ahead of Single and unknown formats when evidence is comparable.
4. Known multi-track releases, original editions before deluxe/reissue variants,
   original artist releases before compilations/soundtracks when otherwise equal.
5. Earlier date, exact score, MusicBrainz on a tie, stable title and identifier.

Dates and counts are tie-breaking evidence, not proof of recording identity.
Explicit compilation or single hints override original-release preferences.
Weak recordings cannot be rescued by an attractive album title. Indistinguishable
MusicBrainz recordings still require manual choice.

Classification uses explicit MusicBrainz primary/secondary types or Discogs format
descriptions. Album, EP, Compilation, Single, Soundtrack and Mixtape are retained.
Useful secondary types are exposed while raw source types remain available.
LP/track count alone cannot establish Album or EP; multi-track singles stay Singles.
Missing release classification is **Unknown**, never a verified Single.

Source agreement retains both sets of IDs and raw release metadata; conflicting
types or edition years are not merged. Discogs IDs never enter MusicBrainz ID or
Last.fm MBID fields. A known MB recording ID follows a Discogs release only with
strong independent title, artist and duration agreement. Cached/structured metadata
keeps title, artist, date, type, position/disc/count, IDs, confidence, evidence and
ranked release alternatives. `info --full` exposes details and coverage warnings.

## Controls and optional credentials

Anonymous Discogs search and release endpoints worked in live checks on
2026-10-14. If the API returns 401/403, coverage is reported as incomplete and
MusicBrainz continues. Add an optional personal access token using `discogs auth`
or **d** in the terminal; `discogs status` checks configuration and `discogs remove`
removes the saved token. Hidden input goes into a separate OS credential vault
entry, never config, logs, URLs or caches. `OSU_LIFE_HACKS_DISCOGS_TOKEN` is an
optional environment override. Restart the watcher after credential changes.
`config set discogs_enabled false` disables Discogs after restarting. Preferences
live in `metadata-config.json` so older launchers still parse their configuration.

Standard .osu files have no album, track-number, ISRC or music-database-ID fields.
The model/adapter accepts explicit hints when supplied; Source, difficulty and
arbitrary Tags are never guessed to be an album. `status` and `config choose N`
select a release; `config correct` accepts `--release-type EP` with title/artist/
album arguments. Persistent manual corrections beat caches and delayed lookups,
and apply across difficulties.

## Bounds, caching and playback

Sources run concurrently. MusicBrainz requests are at least one second apart;
Discogs spacing is 2.5 seconds anonymously or 1.1 with a token. Rate-limit responses
and exhausted-budget headers start a bounded cooldown. Discogs stops after 18
seconds and keeps verified partial results. The coordinator caps each parallel
source at 20 seconds and optional linked Discogs follow-up at eight. It reports
budget exhaustion and incomplete coverage. No unbounded retries, scraping,
third-party proxy or audio upload is used.

Duplicate in-flight lookups coalesce. The durable cache holds at most 256 entries:
complete matches seven days, misses one day, Discogs responses one day, incomplete
results five minutes. Adapter failures never become valid empty API responses.
New cache namespaces ignore older limited matches; corrections remain intact.
Keys include metadata hints and configured-source mode. `privacy --clear-cache`
removes disposable data.

Existing playback/scrobbling timing is preserved. Metadata lookups never block
active-time counting or force submissions. While pending or unresolved, beatmap
title/artist remain usable with a track-title album marked as unknown release.
Late results never rewrite queued/accepted history or submit a play twice.
Last.fm authorization and game readers remain independent of source availability.

References: [MusicBrainz API](https://musicbrainz.org/doc/MusicBrainz_API),
[MusicBrainz rate limits](https://musicbrainz.org/doc/MusicBrainz_API/Rate_Limiting),
[Discogs API](https://www.discogs.com/developers),
[Discogs maintained client](https://github.com/discogs/discogs_client).
Regression tests use mocked HTTP. Live checks validate public API access and
response structures, not catalog-wide identification accuracy.
