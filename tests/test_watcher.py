import asyncio
from types import SimpleNamespace

import pytest

from osu_scrobbler.config import Config
from osu_scrobbler.models import Beatmap, LookupResult, Phase, Telemetry, Track
from osu_scrobbler.watcher import Watcher, display_info


class DeferredMusicBrainz:
    def __init__(self):
        self.futures = {}
        self.corrections = []

    async def lookup(self, beatmap):
        future = asyncio.get_running_loop().create_future()
        self.futures[beatmap.selection_key] = future
        return await future

    def set_correction(self, beatmap, track):
        self.corrections.append((beatmap, track))


def beatmap(name="A"):
    return Beatmap(name, "Track " + name, "Artist", duration=120)


def track(name="A"):
    return Track("Track " + name, "Artist", "Album", "Artist", 120, recording_id=name)


def controlled_time(monkeypatch):
    clock = SimpleNamespace(mono=100.0, wall=1700000000.0)
    monkeypatch.setattr("osu_scrobbler.watcher.time", SimpleNamespace(monotonic=lambda: clock.mono, time=lambda: clock.wall))
    return clock


async def cleanup(watcher):
    for task in list(watcher.tasks):
        task.cancel()
    await asyncio.gather(*watcher.tasks, return_exceptions=True)
    watcher.storage.close()


async def test_identification_starts_at_selection_and_stale_lookup_cannot_replace_new_map(tmp_path, monkeypatch):
    controlled_time(monkeypatch)
    mb = DeferredMusicBrainz()
    watcher = Watcher(tmp_path, Config(), musicbrainz=mb, output=lambda value: None)
    try:
        await watcher.ingest(Telemetry(beatmap("A"), Phase.MENU))
        await asyncio.sleep(0)
        assert "A" in mb.futures  # No gameplay was needed to start identification.
        await watcher.ingest(Telemetry(beatmap("B"), Phase.MENU))
        await asyncio.sleep(0)
        mb.futures["B"].set_result(LookupResult(track("B")))
        await asyncio.sleep(0)
        mb.futures["A"].set_result(LookupResult(track("A")))
        await asyncio.sleep(0)
        assert watcher.engine.track.recording_id == "B"
        assert watcher.status()["beatmap"]["selection_key"] == "B"
        assert not watcher.storage.history()
    finally:
        await cleanup(watcher)


async def test_late_identification_never_counts_time_after_last_observed_gameplay(tmp_path, monkeypatch):
    clock = controlled_time(monkeypatch)
    mb = DeferredMusicBrainz()
    watcher = Watcher(tmp_path, Config(), musicbrainz=mb, output=lambda value: None)
    try:
        await watcher.ingest(Telemetry(beatmap(), Phase.MENU))
        await asyncio.sleep(0)
        clock.mono = 101
        await watcher.ingest(Telemetry(beatmap(), Phase.PLAYING, 0))
        clock.mono = 129.9
        await watcher.ingest(Telemetry(beatmap(), Phase.PLAYING, 28.9))
        assert watcher.engine.active_seconds == pytest.approx(28.9)
        clock.mono = 150  # Lookup returns during a stalled/disconnected transport.
        mb.futures["A"].set_result(LookupResult(track()))
        await asyncio.sleep(0)
        assert watcher.engine.active_seconds == pytest.approx(28.9)
        assert not watcher.storage.history()
    finally:
        await cleanup(watcher)


async def test_manual_choice_wins_over_delayed_lookup_and_persists(tmp_path, monkeypatch):
    controlled_time(monkeypatch)
    mb = DeferredMusicBrainz()
    watcher = Watcher(tmp_path, Config(), musicbrainz=mb, output=lambda value: None)
    try:
        await watcher.ingest(Telemetry(beatmap(), Phase.MENU))
        await asyncio.sleep(0)
        answer = await watcher.command({"command": "config", "action": "correct",
                                         "track": {"title": "防人想歌", "artist": "夏川陽子"}})
        assert answer["saved_correction"]["album_fallback"] is True
        mb.futures["A"].set_result(LookupResult(track()))
        await asyncio.sleep(0)
        assert watcher.engine.track.title == "防人想歌"
        assert mb.corrections[0][0].selection_key == "A"
    finally:
        await cleanup(watcher)


async def test_force_unresolved_metadata_is_temporary_and_exact(tmp_path, monkeypatch):
    clock = controlled_time(monkeypatch)
    watcher = Watcher(tmp_path, Config(), dry_run=True, fixture=True, output=lambda value: None)
    try:
        await watcher.ingest(Telemetry(beatmap(), Phase.MENU))
        watcher._bind(None)  # Ambiguous MusicBrainz outcome.
        result = await watcher.command({"command": "force"})
        assert result["dry_run"] is True
        assert result["exact_metadata"]["album_fallback"] is True
        assert result["playback_start"] == int(clock.wall)
        assert watcher.engine.track is None  # Manual fallback cannot authorize later automation.
        assert not watcher.storage.history()
    finally:
        await watcher.musicbrainz.aclose()
        await cleanup(watcher)


async def test_dryrun_does_not_consume_persisted_force_credit(tmp_path):
    watcher = Watcher(tmp_path, Config(), dry_run=True, fixture=True, output=lambda value: None)
    try:
        credits = [[track().identity, beatmap().track_key]]
        watcher.storage.set_state("force_credits", credits)
        watcher.engine.restore_force_credits(credits)
        await watcher.ingest(Telemetry(beatmap(), Phase.PLAYING, 0), 0, 1700000000, fixture_track=track())
        assert watcher.engine.suppressed
        assert watcher.storage.get_state("force_credits") == credits
    finally:
        await watcher.musicbrainz.aclose()
        await cleanup(watcher)


async def test_disconnected_force_is_rejected(tmp_path):
    watcher = Watcher(tmp_path, Config(), dry_run=True, fixture=True, output=lambda value: None)
    try:
        await watcher.ingest(Telemetry(beatmap(), Phase.MENU), 0, 1700000000)
        await watcher.ingest(Telemetry(None, Phase.DISCONNECTED, connected=False), 1, 1700000001)
        assert "error" in await watcher.command({"command": "force"})
        assert not watcher.previewed
    finally:
        await watcher.musicbrainz.aclose()
        await cleanup(watcher)


def test_info_separates_unknown_and_fallback_from_verified_release_fields():
    result = display_info(Track("防人想歌", "夏川陽子", "防人想歌", "夏川陽子", album_fallback=True))
    assert result["release"]["date"] == "unknown"
    assert result["release"]["labels"] == "unknown"
    assert result["release"]["album_source"] == "track-title fallback"
    assert result["recording"]["id"] == "unknown"
    assert result["selected_metadata"]["artist"] == "夏川陽子"


@pytest.mark.parametrize("duration", [-1, 0, float("inf"), float("nan"), "100"])
async def test_invalid_manual_duration_never_saved(tmp_path, duration):
    mb = DeferredMusicBrainz()
    watcher = Watcher(tmp_path, Config(), fixture=True, musicbrainz=mb, output=lambda value: None)
    try:
        await watcher.ingest(Telemetry(beatmap(), Phase.MENU), 0, 1700000000)
        answer = await watcher.command({"command": "config", "action": "correct",
                                        "track": {"title": "Track", "artist": "Artist", "duration": duration}})
        assert "error" in answer
        assert not mb.corrections
    finally:
        await cleanup(watcher)
