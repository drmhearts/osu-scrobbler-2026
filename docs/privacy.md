# Privacy

osu! life hacks has no analytics, ads, installation tracking, crash uploads or remote
control. Its source is [MIT licensed](../LICENSE).
Dependencies retain their own licenses.

| Data | Use and retention |
| --- | --- |
| Process names | Tray mode checks once a second for osu! Stable and lazer. Other names are discarded immediately; no command lines or process history. |
| Local game memory | Read-only handles decode music metadata, audio clocks, screen state, replay/spectator flags and health. Bounded signature scans retain only matching addresses. No memory dump, player name, score, chat or account identifier is retained. Selected map paths are used transiently for local timing, never exported or uploaded. |
| Terminal messages | At most 50 messages in memory, expiring after five minutes and cleared on tray exit. No new activity log files. |
| Optional metadata debug | Disabled by default. When enabled, raw/canonical music metadata, provider IDs, candidates and matching evidence stay in two local JSONL files of roughly 256 KiB each. No credentials or request bodies. Cleared by privacy --clear-cache. |
| Local databases | Submission music fields, timestamps/status and duplicate safeguards; settings, manual corrections and a lookup cache. History is retained for queue recovery and export. Full lookup details are excluded from submission records. |
| Lookup cache | At most 256 entries; successful matches expire after seven days, ambiguous/missing matches after one. Expired entries are removed when the app opens or accesses the cache. Corrections remain until cleared. |
| Last.fm credentials | API key/secret and session key in the OS vault. No password, osu! credentials or plaintext credential file. |
| Updates | Public version/digest and current/previous program environments in the local data directory. No user identity, music history or credentials sent with release checks. |

**Online requests:** MusicBrainz, Discogs and Last.fm metadata receive song title/artist searches,
optional release hints and music identifiers over HTTPS. Optional Discogs tokens
are sent only in the Authorization header to api.discogs.com; credentials use a
separate OS vault entry, never local config or caches. Discogs can be disabled
with `config set discogs_enabled false` after restarting. Read-only Last.fm metadata
uses the saved application API key (or OSU_LIFE_HACKS_LASTFM_API_KEY), without secret,
session or signature. Public track/album pages can also supply matching evidence.
Disable this source with `config set lastfm_metadata_enabled false`, then restart.
Last.fm scrobbling separately receives authorized song metadata, playback time
and authentication. These services can see your IP address and apply their own
privacy policies. Selected songs are searched before gameplay; live `--dry-run`
still uses metadata sources, while fixture demos make no external requests.

Automatic startup updates contact GitHub for public release metadata/downloads.
Bundled Windows updates include dependencies; legacy/source installs may also
contact the configured package index. Those services see your IP address and
requested releases/packages. Checks use a generic app/version header;
no installation ID or gameplay data. Disable checks with `config set auto_update false`.
Dry-run/fixture starts skip update checks. [Update behavior](updates.md).

There is no external game-reader process, installation, network feed or offset download.

**Your controls:** Background startup requires choosing **y** in first-run setup,
pressing **b** in START. **b** toggles startup; **x** stops the monitor;
**Exit** stops it immediately. No administrator rights, Windows service, global
keyboard hook, recursive file scan or changes to osu! are used.

Run `privacy` to see the local data directory. After stopping the tray/watcher,
run `privacy --clear-cache` to remove lookup cache and old activity logs while
keeping history, queued submissions and corrections. To remove all local music
data/settings, delete that directory after stopping the app; exports elsewhere
must be deleted separately. Remove its `osu-scrobbler` entry in Windows Credential
Manager and revoke the application in Last.fm settings to remove authorization.
Local deletion does not remove scrobbles already sent to Last.fm.

Timer preferences and the install-folder location are stored locally. The latter
refreshes this app's known launcher files; user-edited files are preserved. Older
installs may read their own Python launch chain's executable paths once to find
the install folder. No other process paths or command lines are collected.

Discogs response caches expire after one day; incomplete source results retry
after five minutes. Source preferences are in metadata-config.json. Remove the
osu-life-hacks-discogs/token vault entry with `discogs remove` to revoke local
access, and remove OSU_LIFE_HACKS_DISCOGS_TOKEN if set in your environment.
Last.fm raw metadata and complete selected releases expire after seven days;
complete unresolved metadata results retry after an hour. Normalized lookup keys
retain Unicode identity and optional release hints; API keys are excluded.
