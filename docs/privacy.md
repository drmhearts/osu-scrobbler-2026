# Privacy

The scrobbler has no analytics, ads, installation tracking, crash uploads or remote
control. Its source is [MIT licensed](../LICENSE).
Dependencies and tosu keep their own licenses and behavior.

| Data | Use and retention |
| --- | --- |
| Process names | Tray mode checks once a second for osu! stable, lazer and tosu. Other names are discarded immediately; no command lines or process history. |
| Local tosu feed | Requests song metadata, timing, pause/failure and spectator checks. Player names are compared in memory to exclude spectating; never saved or sent online. No requested scores, ranks, chat, account IDs or file paths. Older tosu may send extra fields; they are discarded. |
| Terminal messages | At most 50 messages in memory, expiring after five minutes and cleared on tray exit. No new activity log files. |
| Local databases | Submission music fields, timestamps/status and duplicate safeguards; settings, manual corrections and a lookup cache. History is retained for queue recovery and export. Full lookup details are excluded from submission records. |
| Lookup cache | At most 256 entries; successful matches expire after seven days, ambiguous/missing matches after one. Expired entries are removed when the app opens or accesses the cache. Corrections remain until cleared. |
| Last.fm credentials | API key/secret and session key in the OS vault. No password, osu! credentials or plaintext credential file. |
| Updates | Public version/digest and current/previous program environments in the local data directory. No user identity, music history or credentials sent with release checks. |

**Online requests:** MusicBrainz receives song title/artist searches and recording/
release IDs over HTTPS. Last.fm receives authorized song metadata, playback time
and authentication. Both services can see your IP address and apply their own
privacy policies. Selected songs are searched before gameplay; live `--dry-run`
still uses MusicBrainz, while fixture demos make no external requests.

Automatic startup updates contact GitHub for public release metadata/downloads.
Bundled Windows updates include dependencies; legacy/source installs may also
contact the configured package index. Those services see your IP address and
requested releases/packages. Checks use a generic app/version header;
no installation ID or gameplay data. Disable checks with `config set auto_update false`.
Dry-run/fixture starts skip update checks. [Update behavior](updates.md).

tosu is a separate LGPL-3.0 program that reads osu! memory. It may read more than
this app requests and has its own local logs and GitHub update checks. The app
downloads an unmodified, checksum-pinned binary from tosu's official GitHub
release when needed. Its managed configuration disables the browser, overlays,
key overlay, PP calculation and mania scroll-speed reads; it serves only on
loopback. `--update=false` disables automatic binary replacement while leaving
the update check. Existing tosu instances keep their settings and behavior.

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
