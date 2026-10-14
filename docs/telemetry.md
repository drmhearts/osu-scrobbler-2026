# Internal readers and compatibility

`readers/memory.py` owns read-only Windows handles and bounded CLR string/pointer
reads. `stable.py` and `lazer.py` resolve their own layouts independently.
`manager.py` discovers only osu! processes, closes exited handles, retries failed
readers after five seconds, and prefers personal gameplay when both clients run.
PID plus process creation time prevents a reused PID from retaining an old handle.
There is no injection, debugger attachment, memory write, helper executable,
runtime download, remote offset service, or WebSocket dependency.

The contract is `Sample`: client, screen, metadata, audio position/length, personal
play and failure. Metadata comprises ordinary/Unicode title and artist, MD5,
map/set IDs and difficulty. Player names, scores, account IDs and chat are not
decoded. Signature scans necessarily inspect bounded memory blocks, but retain
only matching addresses, never a memory dump. A selected map path is used locally
and transiently for optional gameplay-end timing; it never enters a Telemetry event,
database, export or network request.

`InternalAdapter` polls at 100 ms, checks finite values and coherent selection/screen
reads, and feeds the existing selection/attempt state machine. It preserves retry,
menu-loop and duplicate-delivery rules. Pauses are inferred from a frozen clock,
as with the former adapter's source. Forward jumps and intervals over two seconds
are unreliable. An initial gameplay attachment after one second waits for a
visible retry/start; menu listening can begin mid-song. Reconnect preserves the
attempt token but never credits the gap. A process switch resets its selection.

Stable: Windows 32-bit CLR layout, read from a 64-bit host. Eight signatures cover
selected map, state, audio clock/length, ruleset/health, replay, spectator and songs
folder. Each signature must have exactly one executable-page match. Managed strings
have length at +4 and UTF-16 data at +8; pointers are four bytes. Beatmap metadata
and the ruleset/health chain remain version-specific. Replay and non-null spectator
references exclude personal play. Unknown flags, invalid strings and changing
roots disconnect instead of guessing. There is no tested blanket promise across
Stable release channels: a game update can invalidate these signatures/offsets.

Lazer: Windows x64 **2026.1005.0** only. The scaling-size anchor locates candidates;
the OsuGame method-table header and in-memory release string must both match.
Only one validated root is accepted. Reference offsets are centralized in
`lazer.py`; managed strings use length +8/data +12 and eight-byte pointers.
The screen stack and SubmittingPlayer reference pairs establish personal play;
a non-null ReplayScore excludes replay/autoplay, and a base Player without the
submitting pair excludes spectating. Main-menu identity uses OsuGame.menuScreen
(+1616, the reference following osuLogo in this release) and its matching logo.
Unknown screens are metadata-only, never assumed to be main-menu audio.
The Sentry object chain reads only the build's release string, not telemetry or
account information. Builds without that chain also remain unsupported.
NativeAOT, ARM64, custom builds and different lazer versions need a separately
validated layout. Garbage collection can move an object; failed roots trigger
fresh resolution rather than retaining stale pointers.

Audio duration is the game's actual track length, not last hit-object time.
For full-map completion the optional `beatmap.py` parser reads only the selected,
MD5-matching .osu file (16 MiB maximum). It handles circles, slider spans with
inherited timing, spinners and mania holds. Stable paths must stay under its songs
folder; lazer uses its SHA256-addressed storage. Missing files and unsupported
timing leave the endpoint unknown. Half-song timing still works; a full timer
cannot claim a shortened gameplay endpoint without this evidence. Ruleset-specific
slider tail judgements may differ slightly from file timing and require live checks.

## Source evidence and maintenance

Layout facts were inspected in the former reader's pinned source revision
`a4da331515091db0eeb4b7bf363fcbfbbdfe0ded`: its Stable layout and the numeric
2026.1005.0 lazer field table. The new reader does not vendor or execute that
implementation. References for maintainers:

- [Stable layout reference](https://github.com/tosuapp/tosu/blob/a4da331515091db0eeb4b7bf363fcbfbbdfe0ded/packages/tosu/src/memory/stable.ts)
- [Lazer field-layout reference](https://github.com/tosuapp/tosu/blob/a4da331515091db0eeb4b7bf363fcbfbbdfe0ded/packages/tosu/src/assets/offsets.json)
- [osu! game screen ownership](https://github.com/ppy/osu/blob/master/osu.Game/OsuGame.cs)
- [osu! player implementation](https://github.com/ppy/osu/blob/master/osu.Game/Screens/Play/Player.cs)

These are developer references, not links shown by the application. On a new
lazer release, independently inspect the required CLR fields, root header, release
identity and screen pairs; add a version-specific reader/layout and heap fixtures.
Do not silently relax the version gate. For Stable, verify every changed signature
and field on each supported release channel before updating constants.

## Verification and manual checks

Tests build synthetic 32-bit and 64-bit heaps and run the actual pointer/string
decoders. They also cover signatures, invalid flags/versions, Unicode, replay and
spectator exclusion, process reuse, recovery, cancellation, timer progression,
and map-file checks. A Windows test uses ReadProcessMemory against its own small
test buffer to verify native calling conventions and handle cleanup.
Historical normalized fixtures retain regression coverage of selection, completion,
Last.fm delivery, retry and duplicate suppression; they are not live game captures.

No live Stable/lazer session was available during this refactor. Synthetic tests
do not prove that the chosen release layout works on every machine. Before release,
run `python -m osu_scrobbler watch --dry-run` with a supported game and check:

1. Selection and Unicode metadata in all rulesets; different difficulties and songs.
2. Main-menu play/pause/loop and song-selection exclusion.
3. Personal start, pause/resume, retry, failure and results, including speed mods.
4. Replay, autoplay, single/multiplayer spectating and lobby exclusion.
5. Game exit/restart, both clients open, and an unsupported version.

On access failure, run the game and app at matching ordinary-user privilege.
On layout failure, no automatic listening time accrues. Restarting the app cannot
make an unsupported version compatible; use/update a verified layout.
