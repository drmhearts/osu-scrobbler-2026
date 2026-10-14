# Telemetry and compatibility

The replaceable adapter reads local tosu WebSockets: `/websocket/v2` for selected
map metadata and `/tokens` for personal-play evidence. Default host:
`127.0.0.1:24050`. No game-memory writes, osu! login, or osu! API key.

V2 supplies ordinary/Unicode title and artist, checksum/map/set IDs, difficulty,
and `beatmap.time.mp3Length` (audio milliseconds, not hit-object
length). `game.paused`, `play.failed`, and state distinguish gameplay from menus.
Song position detects rewinds and confirms menu audio playback; monotonic
intervals measure real active listening.
Connection filters request only the fields used by the adapter, dropping unrelated
fields from older sources locally. File paths are never requested or retained;
player names are compared only in memory. [Data use](privacy.md).

Personal gameplay requires stable, explicit pause/failure fields, matching map
checksums, and fresh `/tokens` status 2/rawStatus 2 at a nearby position. Watching,
stale/missing evidence, mismatched player identity, and unreliable intervals do
not count. Both stable and lazer gameplay are enabled. Lazer additionally requires
nonempty, matching local profile and score-player names: tosu's private spectator
flag is not in v2, and its companion feed can label single-player spectating as
playing. Unknown identity stays paused; no osu! password/API key is requested.
Feeds are sampled independently, so unusual spectator/client variants still need
live checks. Client-prefixed selection keys prevent stable/lazer timers mixing.

Main-menu/song-selection audio also counts on stable and lazer. Matching fresh
companion status 1 and a normally advancing audio position establish listening.
Frozen clocks and seeks add no time; a rewind starts a fresh loop. Menu listening
can attach mid-song, counting only newly observed playback, never earlier time.
No microphone, audio capture, files or new personal fields are needed.

Initial mid-gameplay attachment waits for an observed beginning (position <=1s).
An existing attempt survives disconnect; a visible rewind/menu/results/failure
establishes the next one. Start timestamps have telemetry sampling precision.
Stalls trigger disconnect after two seconds; reconnect backoff is 0.5–10 seconds.

Research: Mapping Tools reads the current beatmap through
[StructuredOsuMemoryReader](https://github.com/olibomby/Mapping_Tools/blob/37478ee53876658d008e172f4a4c114eccbd3acf/Mapping_Tools.Infrastructure/Editor/CurrentBeatmapMemoryReader.cs).
This app delegates memory compatibility to tosu. Inspected source revision:
[a4da3315](https://github.com/tosuapp/tosu/tree/a4da331515091db0eeb4b7bf363fcbfbbdfe0ded),
especially [v2 fields](https://github.com/tosuapp/tosu/blob/a4da331515091db0eeb4b7bf363fcbfbbdfe0ded/packages/tosu/src/api/types/v2.ts),
[status mapping](https://github.com/tosuapp/tosu/blob/a4da331515091db0eeb4b7bf363fcbfbbdfe0ded/packages/tosu/src/api/utils/scStatus.ts),
and [companion fields](https://github.com/tosuapp/tosu/blob/a4da331515091db0eeb4b7bf363fcbfbbdfe0ded/packages/tosu/src/api/utils/buildResultSC.ts).
`session.playCount` and `replayUIVisible` are not reliable attempt/replay evidence.

Fixtures are source-shaped **synthetic** snapshots, with local socket tests for
loss/reconnect. The user subsequently reported stable gameplay and real delivery
working on Windows. That confirms one stable installation. Lazer uses additional synthetic fixtures
for selection, 29-second timing, pause, disconnect, retry, replay, and identity
guards; live lazer and all spectator variants are not claimed validated. Before real use on another setup, check
selection, pause/resume, retry, failure/results, replay, and exit in dry-run mode.

Lazer source evidence at the same revision: [reader](https://github.com/tosuapp/tosu/blob/a4da331515091db0eeb4b7bf363fcbfbbdfe0ded/packages/tosu/src/memory/lazer.ts)
(`globalPrecise`, replay/spectator state, hash-addressed audio files) and
[shared pause detection](https://github.com/tosuapp/tosu/blob/a4da331515091db0eeb4b7bf363fcbfbbdfe0ded/packages/tosu/src/states/global.ts).
