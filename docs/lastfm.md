# Delivery and export

`auth` uses Last.fm desktop authorization: API key/secret, browser approval, then
session key. HTTPS requests are signed with sorted decoded parameters and MD5.
Credentials stay in Windows Credential Manager (or a supported native secure
vault); plaintext fallback and password-based login are not used.

Automatic submission requires **half the song's length** in active gameplay or
main-menu listening, without a fixed minimum or four-minute cap. Song selection
only identifies music and supports manual force. Duration uses the actual audio
length, then database duration; unknown duration needs identification or force.
Unplayed force is custom listening behavior. Automatic timestamps
use observed gameplay start; unplayed force uses the manual request time.

SQLite reserves each submission/attempt before sending. Acceptance requires one
accepted, zero ignored, and per-track ignored code zero; HTTP success alone is
insufficient. Connection failures/API transient errors retry the same submission.
Timeouts after dispatch and interrupted deliveries are **uncertain** and require
explicit retry. Check Last.fm history first: its API has no idempotency key.

Export includes accepted submissions only. Each UTF-8 line has local submission
success time with offset, artist, title, and album, separated by tabs. Tabs/line
breaks in metadata become spaces. Locking, flush, and an export ledger prevent
repeat entries per destination. Repair an incomplete final line before retrying;
export failure never changes delivery or sends another scrobble.

References: [authorization](https://www.last.fm/api/desktopauth),
[signatures](https://www.last.fm/api/authentication),
[criteria](https://www.last.fm/api/scrobbling),
[track.scrobble](https://www.last.fm/api/show/track.scrobble),
[Last.fm reference source](https://github.com/lastfm/liblastfm).
Cloud validation uses mocks; the user subsequently confirmed live Windows
scrobbling and authorization.
