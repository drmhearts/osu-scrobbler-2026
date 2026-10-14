# Matching

MusicBrainz searches use title/artist (including Unicode), romanization, featured
credits, version labels, and duration. Different remixes and close matches offer
optional manual selection; transliteration alone does not prove identity. Short game cuts
may match full recordings, with the difference retained in detailed metadata.

Scrobbling never waits for a lookup. Until a confident match or saved correction
is available, beatmap title/artist are used, album is the track title, and album
artist is the artist. Missing, ambiguous, unavailable and slow lookups still
qualify after half the song's length in active listening. Audio duration comes
from the internal audio-clock reader first, then the database. Unknown duration prevents automatic submission;
force remains available. A late match
does not resubmit an already queued or accepted play.

`status` lists candidates; `config choose N` or `config correct` saves a correction
across restarts/difficulty changes. Delayed responses cannot replace a newer map
or correction. Matches cache for seven days; ambiguous/missing matches for one.
The cache is capped at 256 entries and expired entries are pruned on startup/access.
Connection failures stay errors, never cached as nonexistent songs.

Available official releases are preferred. Album artist uses release credits or
track artist. With no release, album uses track title and is labelled **title
fallback**, without claiming a verified single. `info --full` separates canonical
recording/release details from corrected submission metadata; missing fields
remain unknown.

Requests are serialized at least one second apart and use
`osu-life-hacks/VERSION (Hky Sigh)` as its visible identifier. The outgoing
User-Agent also includes the existing project contact URL, as required for API
identification; that protocol field is not shown in application output.
Other clients sharing your IP can still cause rate limiting.

Sources: [API](https://musicbrainz.org/doc/MusicBrainz_API),
[rate limits](https://musicbrainz.org/doc/MusicBrainz_API/Rate_Limiting),
[search](https://musicbrainz.org/doc/MusicBrainz_API/Search),
[reference client](https://github.com/alastair/python-musicbrainzngs/blob/master/musicbrainzngs/musicbrainz.py).
The cloud tests use mocked responses; live behavior was subsequently reported by
the user on Windows.
