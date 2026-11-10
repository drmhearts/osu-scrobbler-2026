# Beatmap downloads

## First use

On Windows x64, open **m maps** in the regular terminal or **Beatmap downloads**
from the tray. Choose **7 destination**, then **6 sign in**. Sign in yourself in
the dedicated Edge/Chrome window, including any browser verification. Return to
the terminal and press Enter. Choose **1 start recent**.

The default is **25 recently ranked osu!standard beatmapsets, newest first**.
Closing/hiding the console does not stop the worker. The normal terminal reports
job state, successfully saved count, pending count, current set and last saved ID.
Use **m** again for pause, resume or cancel. The browser also shows transfer progress.
Tray Exit, app stop and updates pause active transfers; choose resume after restart.

Existing Windows packages include the Python browser driver; no separate Python,
Node install, API key or downloaded browser is required. An installed, current
Microsoft Edge or Chrome is required. Source installs use:

    python -m pip install -e ".[test]"
    python -m osu_scrobbler maps

The browser transport currently supports Windows only, matching the app's native
game/tray support. Pure filter/library/checkpoint tests remain portable.

## Filters and batch size

Choose **2 filters** before starting a new batch. Enter keeps a setting; a dash
clears an optional constraint or restores its default. Preferences are remembered.

| Filter | Meaning |
| --- | --- |
| Mode | osu!standard (osu), taiko, catch or mania. Native difficulties only; no inferred converts. |
| Genre | osu!'s named website genres; any by default. |
| Category | Ranked by default; approved, loved, qualified, pending, wip, graveyard or any. Ranked and approved are filtered separately, even though the website groups their search results. |
| Ranked dates | Inclusive YYYY-MM-DD **UTC calendar days**, using ranked_date. For any, sets without that field are excluded. Qualified/pending/wip/graveyard reject ranked-date filters instead of silently substituting an update date. |
| Maximum | Successful new beatmapsets in this job, default 25, range 1–10000. Existing/duplicate sets do not consume the count. |
| Length | Inclusive minimum/maximum **full difficulty duration in seconds**, not drain time. |
| Mapper | Exact host mapper username (Unicode/case normalized), or numeric user ID; guest difficulty authors are not substituted for the host. |

**One difficulty must satisfy mode and every selected length constraint together.**
A standard difficulty that is too short plus a mania difficulty of the right
length does not qualify for a standard download. Counts are per beatmapset, not
per difficulty. Full archives can contain additional difficulties or other modes.
Ranked/approved/loved/any listings use ranked date descending; categories without
ranked dates use last update descending. A finite matching listing can end before
the requested maximum. After 200 search pages, the job pauses with its cursor saved.

## Sign-in and privacy

Last.fm and Discogs credentials cannot authorize osu! downloads. The app does not
extract osu! game tokens or read your ordinary browser profile. It creates its own
visible Edge/Chrome profile in the app data directory and leaves cookies in the
browser's Windows-protected storage. Insecure/mock password-store flags are removed.
No cookie/session JSON export, password prompts, plaintext token files or token
logging are used. The existing secure OS credential backend must be available.

The dedicated browser session is reused until osu! expires/revokes it. Choose
sign in again only when asked; progress is independent of session lifetime.
Do not share the browser profile or copy it between machines. Browser verification
is completed by you; the app stops rather than bypassing it. The site may decline
automated browser access even after successful sign-in.

Search uses osu!web's same-origin JSON interface. Downloads click the normal
full-media link, with the server's normal redirect. The app never uses noVideo,
third-party mirrors, borrowed lazer OAuth scopes, alternative accounts or changed
network identities to get around restrictions. Official API v2 archive downloads
require the special lazer scope, so a generic API client is not presented as a
supported substitute. The website interface may change independently of API v2.

## Library protection and destinations

Destination discovery checks known Windows locations and registered client launch
commands. Custom/relocated installations can be selected explicitly and remembered.
Configure both clients if they are installed in custom locations; deduplication
checks all configured and discovered libraries.

Stable scanning reads .osu metadata and pending .osz archives in Songs/Downloads.
It reads BeatmapSetID inside the file, so renamed folders and archives are handled.
Validated archives are published exclusively as <set-id>.osz in Songs, using a
temporary file on that volume and a no-replace atomic publication. Stable performs
its own import when it runs/refreshes. A subsequent scan observes imported .osu
files and updates history from queued_for_import to imported.

Lazer scanning reads ordinary beatmap metadata from its documented content-addressed
files directory, plus exported/pending archives. **It never opens or modifies Realm.**
The store can retain unreferenced/deleted files, so these IDs are conservatively
skipped too; their presence is not claimed as proof of active database membership.
This may skip a removed map you intended to reacquire. The app does not scrub the
store to resolve that ambiguity.

Lazer archives are placed in the app's own lazer-inbox. If its executable is
configured, each .osz is passed to that executable using the client's supported
archive/IPC import workflow. It is reported as **queued_for_import**, not imported
merely because a process launched successfully. Without an executable, it stays
**downloaded**; choose **8 import saved** after configuring the executable. Check
the set in lazer before explicitly recording confirmation with:

    python -m osu_scrobbler maps confirm --set-id 123456

Lazer receipts then say imported_confirmed_by_user. There is no false automatic
acknowledgment of Realm membership. Downloaded, queued and imported states are
distinct in JSON status. To transfer an existing Stable library to lazer, use
lazer's built-in migration; the downloader deliberately skips IDs found in either
client rather than redownloading them.

No existing library file is deleted, replaced, extracted over or reorganized.
Scans do not traverse nested links/junctions. Unreadable existing archives stop a
job instead of risking a duplicate. Archive validation checks ZIP integrity/CRC,
positive requested set IDs, metadata, unsafe paths, encryption and size limits
(2 GiB archive / 8 GiB expanded / 20000 entries). Full media is retained untouched.

## Checkpoints, pause and recovery

The app data directory contains beatmap-downloads/downloads.sqlite3 (transactional
jobs, original filter/destination snapshot, cursor, pending queue, completed IDs,
receipts, last successful set and scan cache), staging, lazer-inbox and browser.
Job snapshots remain unchanged when you edit defaults for the next batch.

Pause/cancel interrupts an in-flight transfer safely. **Cancel preserves the job
and queue** so it can be resumed. Incomplete transfers restart from the beginning;
there is no unsafe HTTP range continuation. Fully validated staged files survive
a crash and can be placed without requesting the same archive again. Placement
before a checkpoint is recovered by checking the existing archive or Stable's
imported metadata. Completion history is written only after successful placement/
verified import; a request or partial download alone never counts.

Completed IDs remain remembered even if you later remove a library map. Pending
queue ownership is unique across jobs in a profile. A global OS-released worker
lock serializes all jobs/profiles/sign-in sessions; shared completion receipts
prevent subsequent jobs from re-requesting placed sets. If another job owns a
pending set, it is skipped; resume the owning job to finish it. Process crashes
release the lock automatically. Restart the app and choose resume; the saved
cursor/queue are retained. Corrupt checkpoints fail closed; preserve them instead
of deleting history to bypass an error.

CLI equivalents:

    python -m osu_scrobbler maps configure --client stable --root "D:\Games\osu!"
    python -m osu_scrobbler maps login
    python -m osu_scrobbler maps start --mode mania --genre electronic --limit 20 --min-length 90 --max-length 240
    python -m osu_scrobbler maps status
    python -m osu_scrobbler maps pause
    python -m osu_scrobbler maps resume
    python -m osu_scrobbler maps cancel
    python -m osu_scrobbler maps scan
    python -m osu_scrobbler --json maps status

Use --job N for an older job. Start creates a new finite batch; resume retains the
old batch's filters, count and maximum. Creating a new batch does not reset history.
A --foreground option is available for supervised runs.
Unused candidates from a completed, full batch remain recorded as deferred, with
ownership released so the next recent batch can download them. Paused/cancelled
unfinished jobs retain their pending ownership for safe resume.

## Throttling

All profiles for the same Windows user share beatmap-download-shared in the
default app data directory, including custom --data-dir profiles. Policy is one
download request per 60 seconds, at most 30 per rolling hour, and at least two
seconds between logical listing/metadata requests. These are conservative app
budgets, **not published osu! download allowances**. Requests are recorded before
dispatch, so failures/crashes still consume budget. Recent request records persist
seven days. Jobs are bound to their original signed-in account, with an outer
global budget that switching accounts cannot reset.

HTTP 429 stops the job, records Retry-After (seconds or HTTP date), and preserves
its queue. Without that header, cooldown is one hour. Resuming cannot clear it,
and no connection request is sent while cooldown is active. Transient errors use
bounded exponential backoff, initially at least one minute, up to one hour;
longer server Retry-After values are retained. Status shows the cooldown end time.
There are no rapid automatic retries or account/mirror/network switching.

osu!'s download allowance is account-aware and includes requests made elsewhere.
**Client pacing cannot guarantee avoidance of server-side limits.** Do not run
other downloaders simultaneously. Shared records are local to this Windows user,
not synchronized across devices.

## Maintenance references

Checked 2026-11-10:
[official API terms and scopes](https://osu.ppy.sh/docs/index.html),
[official download controller](https://github.com/ppy/osu-web/blob/master/app/Http/Controllers/BeatmapsetsController.php),
[official account allowance](https://github.com/ppy/osu-web/blob/master/app/Models/User.php),
[lazer file storage](https://osu.ppy.sh/wiki/en/Client/Release_stream/Lazer/File_storage),
[lazer archive import entry point](https://github.com/ppy/osu/blob/master/osu.Desktop/Program.cs).

Regression tests use isolated libraries, fake sources and a real Edge transport
with an entirely routed mock site. They do not verify a live osu! login, server
quota or a player's actual library. Native game import still needs a manual check.
The downloader does not depend on game-memory offsets; scrobbling's separate
Stable/lazer reader compatibility limits remain unchanged.
