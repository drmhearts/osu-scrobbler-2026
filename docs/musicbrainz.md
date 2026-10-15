# Recording and release matching

MusicBrainz, Discogs and Last.fm are independent read-only sources. MusicBrainz
supplies recording IDs and release membership; Discogs adds verified tracklists
and formats; Last.fm supplies track/album information and Featured On candidates.
Search summaries and popularity alone never establish release membership.

## Staged lookup

1. Persistent manual corrections, then the durable cache and known IDs.
2. Parallel MusicBrainz (7 seconds), Discogs (6 seconds), Last.fm API (4.5 seconds).
   A verified multi-track release scoring at least 0.97 can cancel slow work.
   A single does not end the search for comparable multi-track appearances.
3. If needed, Last.fm Featured On and Discogs alternatives/linked releases run
   concurrently, each capped at 4 seconds. Last.fm-only matches compare this small
   list rather than blindly accepting track.getInfo's one album.
4. Only after these attempts finish may track title become the album fallback.
   This fallback has Unknown type and an empty album artist, never a claimed Single.

MusicBrainz browses up to 100 releases beyond its limited embedded list and checks
three release groups. Discogs fast search checks one page of 20 results and two
tracklists; deeper work reserves general/EP/Compilation searches and at most four
tracklists within the remaining budget. Public linked Discogs IDs work even when
search requires a token. Last.fm discovers six Featured On references and verifies
three, ordered by artist relationship then listeners. Completed candidates survive
slow alternatives. Each Last.fm HTTP request has a two-second timeout. No full
discographies, scraping proxies, access-block bypass or audio uploads are used.

Playback timing, force, queuing and delivery run independently. Pending tracks
retain title/primary artist; unknown album and albumArtist are omitted from Last.fm
submissions. Results never rewrite queued/accepted plays or submit a play twice.
The maximum background lookup budget is approximately eleven seconds.

## Credits and safe evidence

Raw osu! artist/title and Unicode spellings remain separate from canonical track
artist, primary artist, featured performers and album artist. Incoming explicit
feat., ft. and featuring suffixes are removed for search/default attribution;
official provider track credits may retain them. Explicit collaborations such as
A & B, A and B and A with B are not stripped. A provider's A & B can match incoming
A feat. B when all names agree.

Album artist comes from actual release metadata, including Various Artists,
groups and producer credits. It is never copied automatically from track artist.
Missing compilation per-track credits are not inferred from album artist. Last.fm
candidates require a matching track in album.getInfo or a public album tracklist;
Featured On links are discovery evidence only. Public HTML must establish its
canonical track/album identity. Redirects and foreign URLs fail closed.

Recording confidence uses title (50%), artist (38%), duration (10%) and search
relevance (2%). Artist/title must both match strongly, versions must agree, and
confidence must reach 0.86. Unicode, transliteration and aliases are supported.
Remix/live/instrumental identity stays strict; game/TV cuts may match longer tracks
with that difference retained. Exact recording IDs/ISRCs take priority and cannot
be overridden by unrelated cross-provider title matches.

## Unified ranking and classification

Eligible candidates share a source-neutral model. Release score combines recording
confidence (86%), membership strength (8%) and album-artist relationship (6%). Each
independent agreeing provider adds 0.07, capped at 0.14, before selection. Duplicate
editions from one provider cannot inflate consensus. Release-title hints add up
to 0.04; position/date agreement adds 0.015/0.005. Scores cap at 0.999.

Deterministic ranking compares exact release IDs and strong release-title hints,
then score bands of 0.02, official status and album-artist relationship. Comparable
credible multi-track releases precede verified singles and unresolved formats.
Original editions precede deluxe/live variants and compilations/soundtracks when
otherwise equal. Last.fm listeners break ties after artist relationship and
membership; date, exact score, provider preference (MusicBrainz/Discogs/Last.fm),
stable title and ID finish the ordering. Popularity never raises recording
confidence or makes an unrelated compilation eligible.

Agreement requires compatible type/year, title, primary artist, album artist,
featured credits and duration. Supporting IDs and raw release metadata are kept.
Last.fm album MBIDs have a separate namespace: they are not assumed to identify a
specific MusicBrainz edition. MB recording IDs follow another source only after
independent strong title/artist/duration agreement. Ambiguous MB recordings need
consensus or manual choice.

Album, EP, Compilation, Single, Soundtrack and Mixtape come from explicit MB types
or Discogs formats. Last.fm does not reliably supply formal release types:
Last.fm-only releases remain Unknown even with verified multi-track membership.
Count establishes a plausible multi-track appearance, not an EP/Album type. An
agreeing source can supply a missing classification. Last.fm date/year stays empty
unless another source supplies it. Available positions, counts, duration, IDs and
score evidence are retained.

## Credentials and cache

Last.fm track.getInfo and album.getInfo require an application API key, without a
session key, secret or signature. The app reuses the key saved by authentication,
or OSU_LIFE_HACKS_LASTFM_API_KEY as an environment override. Public pages can work
without a key, but HTTP 403/406/429 or changed HTML may make them unavailable.
API/web cooldowns are separate; errors never become cached empty successes. Keys
are excluded from cache keys and diagnostic files. To disable metadata only:
`config set lastfm_metadata_enabled false`, then restart.

Discogs may require an optional personal token: `discogs auth` or d, then restart.
`discogs status` checks setup; `discogs remove` removes the OS vault entry.
OSU_LIFE_HACKS_DISCOGS_TOKEN overrides the saved token. Tokens go only in the API
Authorization header. `config set discogs_enabled false` disables the source after
restart. MusicBrainz and scrobble authentication remain independent.

Spacing: MusicBrainz one second, Discogs 2.5 seconds anonymous/1.1 authenticated,
Last.fm 0.25 seconds, plus bounded rate-limit cooldowns. In-flight resolutions
coalesce. The durable cache holds at most 256 entries: complete selected releases
seven days, complete misses one hour, incomplete coverage five minutes, Last.fm
raw metadata seven days and Discogs raw data one day. Keys preserve normalized
Unicode identity, metadata hints and source mode; feat/ft variants share entries
without losing current raw input. Adapter caches reuse persistent IDs/album
identities. New namespaces invalidate old limited matches; corrections survive.

## Diagnostics and corrections

`config set metadata_debug true`, then restart, enables local JSONL diagnostics:
raw/normalized input, source status, candidates, scores/evidence, selected release,
album artist, type, reason, fallback and cache flags. metadata-debug.jsonl and one
rotated file are limited to roughly 256 KiB each. Normal output stays compact.
These files contain music metadata, never credentials or HTTP request bodies.
`privacy --clear-cache` removes them and disposable cache while preserving history
and corrections. Preferences live in metadata-config.json for older launchers.

Standard .osu files lack album/ISRC/track number fields. Source, arbitrary Tags and
difficulty names are never guessed as albums. `config choose N` and `config correct`
provide persistent overrides, including --album-artist and --release-type EP.
Manual changes always beat delayed lookups.

## Validation and limits

Mocked HTTP regressions verify Undead Corporation / The Empress -> 暴君 and
wotaku feat. 初音ミク / イルミナティ -> wotaku / アンティーク, including Unicode,
popularity ranking, cached difficulties and track membership. Catalog expectations
come from the supplied examples; fixture durations/listener counts are synthetic
test data, not a live catalog snapshot. Release types, Various Artists, official
features/collaborations, conflicts, failures and pending delivery are also tested.

Live checks on 2026-10-15 returned HTTP 406 for public Last.fm pages, and no API key
was available. The named examples are verified adapter/coordinator regressions,
not live end-to-end catalog checks. Catalog errors, missing credits, edition
ambiguity, redirects, HTML changes and access blocks may need manual correction.
Stable/lazer memory-layout compatibility is unchanged.

References: [Last.fm track.getInfo](https://www.last.fm/api/show/track.getInfo),
[Last.fm album.getInfo](https://www.last.fm/api/show/album.getInfo),
[MusicBrainz API](https://musicbrainz.org/doc/MusicBrainz_API),
[MusicBrainz limits](https://musicbrainz.org/doc/MusicBrainz_API/Rate_Limiting),
[Discogs API](https://www.discogs.com/developers).
