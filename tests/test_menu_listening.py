"""Menu audio counts only while the source reports an advancing song clock."""

import asyncio
import copy
import json
from pathlib import Path

import pytest

from osu_scrobbler.models import Phase, Track
from osu_scrobbler.playback import PlaybackEngine
from osu_scrobbler.telemetry import TosuAdapter


def menu_frame(client, state, position):
    frames = json.loads((Path(__file__).parent / "fixtures/tosu_v2_states.json").read_text("utf-8"))
    frame = copy.deepcopy(frames[0])
    frame["payload"]["client"] = client
    frame["payload"]["state"]["number"] = state
    frame["payload"]["beatmap"]["time"]["live"] = position * 1000
    frame["tokens"].update(status=1, rawStatus=state, time=position)
    return frame


def observe(adapter, engine, frame, at):
    event = adapter.parse(frame["payload"], companion=frame["tokens"], monotonic=at)
    engine.update(event, at, 1700000000 + at)
    if engine.track is None and engine.beatmap:
        engine.bind_track(Track.fallback(engine.beatmap))
    return engine.next_submission(at, 1700000000 + at)


@pytest.mark.parametrize("client", ["stable", "lazer"])
@pytest.mark.parametrize("state", [0])
def test_main_menu_scrobbles_after_half_the_song_and_only_once(client, state):
    adapter, engine = TosuAdapter(), PlaybackEngine()
    assert observe(adapter, engine, menu_frame(client, state, 35), 0) is None
    assert observe(adapter, engine, menu_frame(client, state, 36), 1) is None
    assert engine.phase is Phase.LISTENING
    assert observe(adapter, engine, menu_frame(client, state, 91.9), 56.9) is None
    submission = observe(adapter, engine, menu_frame(client, state, 92), 57)
    assert submission is not None
    assert submission.playback_start == 1700000001
    assert submission.track.album == submission.track.title
    engine.mark_success(submission, 1700000057, monotonic=57)
    assert observe(adapter, engine, menu_frame(client, state, 102), 67) is None


@pytest.mark.parametrize("client", ["stable", "lazer"])
@pytest.mark.parametrize("state", [5, 11, 12, 13])
async def test_song_select_identifies_and_allows_force_without_automatic_scrobbling(tmp_path, client, state):
    from osu_scrobbler.config import Config
    from osu_scrobbler.models import LookupResult
    from osu_scrobbler.watcher import Watcher

    matched = Track("Matched title", "Matched artist", "Album", "Album artist", 112, verified=True)
    class MusicBrainz:
        async def lookup(self, beatmap):
            return LookupResult(matched)
        async def info(self, track):
            return track
    adapter = TosuAdapter()
    watcher = Watcher(tmp_path, Config(), dry_run=True, musicbrainz=MusicBrainz(), output=lambda _: None)
    try:
        for at in (0, 1, 200):
            frame = menu_frame(client, state, 35 + at)
            event = adapter.parse(frame["payload"], companion=frame["tokens"], monotonic=at)
            await watcher.ingest(event, at, 1700000000 + at)
            await asyncio.sleep(0)
        assert watcher.engine.active_seconds == 0
        assert watcher.engine.track == matched
        assert not watcher.previewed
        assert (await watcher.command({"command": "info"}))["info"]["selected_metadata"]["title"] == "Matched title"
        result = await watcher.command({"command": "force"})
        assert result["exact_metadata"]["title"] == "Matched title"
        assert len(watcher.previewed) == 1 and watcher.previewed[0].manual
    finally:
        for task in list(watcher.tasks):
            task.cancel()
        await asyncio.gather(*watcher.tasks, return_exceptions=True)
        watcher.storage.close()


def test_switching_to_song_select_cannot_trigger_a_qualified_automatic_submission():
    adapter, engine = TosuAdapter(), PlaybackEngine()
    observe(adapter, engine, menu_frame("stable", 0, 35), 0)
    observe(adapter, engine, menu_frame("stable", 0, 36), 1)
    assert observe(adapter, engine, menu_frame("stable", 0, 91.9), 56.9) is None
    assert observe(adapter, engine, menu_frame("stable", 5, 92.1), 57.1) is None
    assert engine.phase is Phase.MENU
    assert engine.next_submission(57.1, 1700000057.1, manual=True) is not None


def test_menu_pause_and_seek_do_not_count_as_listening():
    adapter, engine = TosuAdapter(), PlaybackEngine()
    for at, position in [(0, 35), (1, 36), (10, 45)]:
        assert observe(adapter, engine, menu_frame("stable", 0, position), at) is None
    assert engine.active_seconds == 9
    assert observe(adapter, engine, menu_frame("stable", 0, 45), 40) is None
    assert engine.phase is Phase.PAUSED and engine.active_seconds == 9
    assert observe(adapter, engine, menu_frame("stable", 0, 60), 41) is None
    assert engine.active_seconds == 9  # Forward seek adds no listening time.
    assert observe(adapter, engine, menu_frame("stable", 0, 61), 42) is None
    assert observe(adapter, engine, menu_frame("stable", 0, 107.9), 88.9) is None
    assert observe(adapter, engine, menu_frame("stable", 0, 108), 89) is not None


@pytest.mark.parametrize("change", [{"status": 8}, {"md5": "another-song"}, {"time": 1000}, {"rawStatus": 2}])
def test_menu_requires_matching_listening_evidence(change):
    adapter, engine = TosuAdapter(), PlaybackEngine()
    observe(adapter, engine, menu_frame("stable", 0, 35), 0)
    for at in (1, 30, 60):
        frame = menu_frame("stable", 0, 35 + at)
        frame["tokens"].update(change)
        assert observe(adapter, engine, frame, at) is None
    assert engine.active_seconds == 0


def test_menu_loop_is_a_fresh_listen_and_disconnect_never_counts_the_gap():
    adapter, engine = TosuAdapter(), PlaybackEngine()
    observe(adapter, engine, menu_frame("lazer", 0, 35), 0)
    observe(adapter, engine, menu_frame("lazer", 0, 36), 1)
    first = observe(adapter, engine, menu_frame("lazer", 0, 92), 57)
    engine.mark_success(first, 1700000057, monotonic=57)
    assert observe(adapter, engine, menu_frame("lazer", 0, 0), 58) is None
    assert observe(adapter, engine, menu_frame("lazer", 0, 1), 59) is None
    engine.update(adapter.disconnected(), 60, 1700000060)
    assert observe(adapter, engine, menu_frame("lazer", 0, 2), 88) is None
    second = observe(adapter, engine, menu_frame("lazer", 0, 58), 144)
    assert second is not None and second.attempt_id != first.attempt_id


async def test_menu_fallback_reaches_lastfm_when_lookup_is_unavailable(tmp_path):
    from osu_scrobbler.config import Config
    from osu_scrobbler.lastfm import DeliveryResult
    from osu_scrobbler.watcher import Watcher

    class MusicBrainz:
        async def lookup(self, beatmap):
            raise OSError("offline")

    delivered = []

    class LastFm:
        async def submit(self, submission):
            delivered.append(submission)
            return DeliveryResult("accepted", accepted=1)

    adapter = TosuAdapter()
    watcher = Watcher(tmp_path, Config(), musicbrainz=MusicBrainz(), lastfm=LastFm(), output=lambda _: None)
    try:
        for at in (0, 1, 56.9, 57):
            frame = menu_frame("stable", 0, 35 + at)
            event = adapter.parse(frame["payload"], companion=frame["tokens"], monotonic=at)
            await watcher.ingest(event, at, 1700000000 + at)
            await asyncio.sleep(0)
            await watcher.deliver_once()
            assert len(delivered) == (1 if at == 57 else 0)
        assert delivered[0].track.album == delivered[0].track.title == "防人想歌"
        assert delivered[0].track.album_artist == delivered[0].track.artist
        assert watcher.lookup_status.startswith("MusicBrainz unavailable")
    finally:
        for task in list(watcher.tasks):
            task.cancel()
        await asyncio.gather(*watcher.tasks, return_exceptions=True)
        watcher.storage.close()
