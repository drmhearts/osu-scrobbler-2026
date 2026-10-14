import copy
import json
from pathlib import Path

import pytest

from osu_scrobbler.models import Phase, Track
from osu_scrobbler.playback import PlaybackEngine
from osu_scrobbler.telemetry import TosuAdapter


@pytest.fixture
def frames():
    path = Path(__file__).parent / "fixtures/tosu_lazer_states.json"
    return {frame["name"]: frame for frame in json.loads(path.read_text("utf-8"))}


def sample(adapter, frame):
    return adapter.parse(frame["payload"], companion=frame["tokens"])


def test_lazer_selection_identifies_before_gameplay_and_reports_audio_length(frames):
    event = sample(TosuAdapter(), frames["selected"])
    assert event.phase == Phase.MENU
    assert event.beatmap.title_unicode == "防人想歌"
    assert event.beatmap.duration == 112
    assert event.beatmap.audio_path == ""  # Audio paths are unnecessary and never retained.
    assert event.beatmap.selection_key.startswith("lazer:")


def test_lazer_half_song_threshold_pause_disconnect_and_fresh_retry(frames):
    adapter = TosuAdapter()
    engine = PlaybackEngine()
    def observe(name, at):
        engine.update(sample(adapter, frames[name]), at, 1700000000 + at)
        if engine.track is None and engine.beatmap:
            engine.bind_track(Track.fallback(engine.beatmap))
        return engine.next_submission(at, 1700000000 + at)
    assert observe("selected", 0) is None
    assert observe("playing", 1) is None
    assert engine.phase == Phase.PLAYING
    assert observe("playing_progress", 29.9) is None
    assert engine.active_seconds == pytest.approx(28.9)
    assert observe("paused", 29.9) is None
    assert observe("resumed", 60) is None
    engine.update(adapter.disconnected(), 60, 1700000060)
    assert observe("resumed", 90) is None
    accepted = observe("resumed", 117.125)  # The next sample crosses half the song in active play.
    assert accepted is not None and accepted.playback_start == 1700000001
    engine.mark_success(accepted, 1700000117.125, monotonic=117.125)
    assert observe("resumed", 118) is None
    assert observe("retry", 119) is None
    assert engine.active_seconds == 0
    second = observe("resumed", 175)
    assert second is not None and second.attempt_id != accepted.attempt_id


@pytest.mark.parametrize("change", [{"name": ""}, {"name": "someone else"}, {"banchoStatus": {"number": 6}}])
def test_lazer_spectating_or_unknown_local_identity_never_counts(frames, change):
    frame = copy.deepcopy(frames["playing"])
    frame["payload"]["profile"].update(change)
    event = sample(TosuAdapter(), frame)
    assert event.phase == Phase.PAUSED and not event.interval_reliable


def test_lazer_replay_results_and_failure_are_explicit(frames):
    adapter = TosuAdapter()
    sample(adapter, frames["playing"])
    assert sample(adapter, frames["replay"]).phase == Phase.MENU
    assert sample(adapter, frames["results"]).phase == Phase.RESULTS
    assert sample(adapter, frames["failed"]).phase == Phase.FAILED


def test_stable_to_lazer_switch_cannot_accumulate_the_old_attempt(frames):
    adapter = TosuAdapter()
    stable = copy.deepcopy(frames["playing"])
    stable["payload"]["client"] = "stable"
    first = sample(adapter, stable)
    progress = copy.deepcopy(frames["playing_progress"])
    progress["payload"]["client"] = "stable"
    sample(adapter, progress)
    switched_midplay = sample(adapter, frames["playing_progress"])
    assert switched_midplay.beatmap.selection_key != first.beatmap.selection_key
    assert switched_midplay.phase == Phase.PAUSED and not switched_midplay.interval_reliable
    new_start = sample(adapter, frames["playing"])
    assert new_start.phase == Phase.PLAYING and new_start.attempt_token != first.attempt_token


@pytest.mark.parametrize("change", [{"status": 8}, {"md5": "wrong map"}, {"time": 100}, {"rawStatus": 5}])
def test_lazer_companion_must_describe_the_same_personal_gameplay(frames, change):
    frame = copy.deepcopy(frames["playing"])
    frame["tokens"].update(change)
    event = sample(TosuAdapter(), frame)
    assert event.phase != Phase.PLAYING and not event.interval_reliable
