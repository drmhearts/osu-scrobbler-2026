"""Recorded clock/telemetry sequences exercise the engine without osu! or APIs."""

import pytest

from osu_scrobbler.models import Beatmap, Phase, Telemetry, Track
from osu_scrobbler.playback import DUPLICATE_MESSAGE, DuplicateSuppressed, PlaybackEngine


MAP = Beatmap("map-a", "Song", "Artist", duration=180.0)
TRACK = Track("Song", "Artist", "Album", "Artist", 180.0, recording_id="recording-a")


def observe(engine, clock, phase=Phase.PLAYING, *, beatmap=MAP, position=None, token=None):
    engine.update(Telemetry(beatmap, phase, position, token), clock, 1000 + clock)


def ready(mode="custom", **kwargs):
    engine = PlaybackEngine(mode, **kwargs)
    observe(engine, 0, Phase.MENU)
    engine.bind_track(TRACK)
    return engine


def submit(engine, clock, manual=False):
    return engine.next_submission(clock, 1000 + clock, manual=manual)


def accept(engine, submission, clock):
    assert submission is not None
    engine.mark_success(submission, 1000 + clock, monotonic=clock)


def test_selection_is_available_before_gameplay_and_force_timestamp_is_now():
    engine = ready()
    assert engine.beatmap == MAP
    assert engine.track == TRACK
    assert engine.attempt_id is None
    assert submit(engine, 100) is None
    forced = submit(engine, 100, manual=True)
    assert forced.manual
    assert forced.playback_start == 1100
    assert forced.attempt_id.startswith("selection:")


def test_28_point_9_is_too_short_and_29_is_reserved_once():
    engine = ready()
    observe(engine, 0, position=0)
    observe(engine, 28.9, position=57.8)  # Speed mods must not double elapsed time.
    assert submit(engine, 28.9) is None
    observe(engine, 29, position=58)
    submission = submit(engine, 29)
    assert submission.playback_start == 1000
    engine.mark_pending(submission)
    for clock in (29, 30, 100):
        assert submit(engine, clock) is None
    accept(engine, submission, 100)
    assert submit(engine, 101) is None


def test_pause_and_disconnect_exclude_time_without_resetting_attempt():
    engine = ready()
    observe(engine, 0)
    attempt = engine.attempt_id
    observe(engine, 10, Phase.PAUSED)
    observe(engine, 110, Phase.PLAYING)
    observe(engine, 120)
    engine.update(Telemetry(None, Phase.DISCONNECTED, connected=False), 121, 1121)
    observe(engine, 220)
    assert engine.attempt_id == attempt
    assert engine.active_seconds == 20
    observe(engine, 228.9)
    assert submit(engine, 228.9) is None
    observe(engine, 229)
    assert submit(engine, 229) is not None


def test_unreliable_interval_cannot_cross_29_second_threshold():
    engine = ready()
    observe(engine, 0)
    observe(engine, 28.9)
    assert submit(engine, 28.9) is None
    engine.update(Telemetry(MAP, Phase.PAUSED, interval_reliable=False), 29.1, 1029.1)
    assert engine.active_seconds == 28.9
    assert submit(engine, 29.1) is None
    observe(engine, 30)
    observe(engine, 30.1)
    assert submit(engine, 30.1) is not None


@pytest.mark.parametrize("boundary", [Phase.MENU, Phase.RESULTS, Phase.FAILED])
def test_fresh_attempt_does_not_accumulate_prior_short_attempt(boundary):
    engine = ready()
    observe(engine, 0)
    first = engine.attempt_id
    observe(engine, 20, boundary)
    observe(engine, 21)
    assert engine.attempt_id != first
    observe(engine, 31)
    assert engine.active_seconds == 10
    assert submit(engine, 31) is None


@pytest.mark.parametrize("restart", ["position", "token"])
def test_restart_during_playing_resets_timer(restart):
    engine = ready()
    observe(engine, 0, position=0, token="one")
    observe(engine, 20, position=20, token="one")
    prior = engine.attempt_id
    observe(engine, 21, position=0 if restart == "position" else 21,
            token="two" if restart == "token" else "one")
    assert engine.attempt_id != prior
    assert engine.active_seconds == 0
    assert submit(engine, 29) is None


def test_disconnected_reconnect_to_menu_exposes_fresh_attempt_boundary():
    engine = ready()
    observe(engine, 0)
    observe(engine, 10)
    prior = engine.attempt_id
    observe(engine, 20, Phase.DISCONNECTED)
    observe(engine, 100, Phase.MENU)
    observe(engine, 101)
    assert engine.attempt_id != prior
    assert engine.active_seconds == 0


def test_disconnected_qualified_attempt_waits_until_reconnected():
    engine = PlaybackEngine()
    observe(engine, 0)
    observe(engine, 29)
    observe(engine, 30, Phase.DISCONNECTED)
    engine.bind_track(TRACK)
    assert submit(engine, 100) is None
    observe(engine, 101, Phase.RESULTS)
    assert submit(engine, 101) is not None


def test_late_metadata_binding_keeps_active_time_and_selection_change_clears_metadata():
    engine = PlaybackEngine()
    observe(engine, 0)
    observe(engine, 29)
    assert submit(engine, 29) is None
    engine.bind_track(TRACK)
    assert submit(engine, 29) is not None
    episode = engine.selection_episode
    other = Beatmap("map-b", "Another", "Artist")
    observe(engine, 30, Phase.MENU, beatmap=other)
    assert engine.track is None
    assert engine.selection_episode != episode
    assert engine.active_seconds == 0


@pytest.mark.parametrize("ended_phase", [Phase.PAUSED, Phase.RESULTS, Phase.FAILED, Phase.MENU])
def test_qualifying_attempt_can_submit_when_terminal_frame_or_metadata_arrives_late(ended_phase):
    engine = PlaybackEngine()
    observe(engine, 0)
    observe(engine, 29, ended_phase)
    assert engine.active_seconds == 29
    engine.bind_track(TRACK)
    submission = submit(engine, 100)
    assert submission is not None
    assert submission.playback_start == 1000
    assert engine.active_seconds == 29


def test_force_suppresses_exactly_next_attempt_and_qualifying_retry_emits_once():
    engine = ready()
    forced = submit(engine, 0, manual=True)
    accept(engine, forced, 0)
    with pytest.raises(DuplicateSuppressed, match="^" + DUPLICATE_MESSAGE.replace(".", "\\.") + "$"):
        submit(engine, 1, manual=True)
    observe(engine, 1)
    observe(engine, 30)
    assert engine.suppressed
    assert submit(engine, 30) is None
    with pytest.raises(DuplicateSuppressed) as error:
        submit(engine, 30, manual=True)
    assert str(error.value) == DUPLICATE_MESSAGE
    observe(engine, 31, Phase.RESULTS)
    observe(engine, 32)
    observe(engine, 60.9)
    assert submit(engine, 60.9) is None
    observe(engine, 61)
    second = submit(engine, 61)
    assert second is not None
    assert second.attempt_id != forced.attempt_id
    assert submit(engine, 61) is None
    accept(engine, second, 61)
    assert submit(engine, 100) is None


def test_force_during_play_suppresses_current_and_next_attempt():
    engine = ready()
    observe(engine, 0)
    forced = submit(engine, 5, manual=True)
    assert forced.playback_start == 1000
    accept(engine, forced, 5)
    engine.bind_track(TRACK)  # Late metadata must not consume the next-play credit.
    assert submit(engine, 40) is None
    observe(engine, 41, Phase.RESULTS)
    observe(engine, 42)
    assert engine.suppressed
    assert submit(engine, 71) is None
    observe(engine, 72, Phase.RESULTS)
    observe(engine, 73)
    assert submit(engine, 102) is not None


def test_force_after_unsubmitted_ended_attempt_uses_manual_time():
    engine = ready()
    observe(engine, 0)
    observe(engine, 10, Phase.RESULTS)
    forced = submit(engine, 20, manual=True)
    assert forced.playback_start == 1020
    assert forced.attempt_id.startswith("selection:")
    accept(engine, forced, 20)
    with pytest.raises(DuplicateSuppressed):
        submit(engine, 50, manual=True)


def test_difficulty_changes_cannot_evade_force_recording_suppression():
    engine = ready()
    forced = submit(engine, 0, manual=True)
    accept(engine, forced, 0)
    difficulty = Beatmap("map-difficulty-b", "Song", "Artist", duration=180)
    observe(engine, 1, Phase.MENU, beatmap=difficulty)
    engine.bind_track(TRACK)
    observe(engine, 2, beatmap=difficulty)
    assert submit(engine, 40) is None
    assert engine.suppressed


def test_both_success_interval_and_fresh_independent_play_are_required():
    engine = ready()
    observe(engine, 0)
    first = submit(engine, 29)
    # Delivery completes after a second attempt has already started.
    observe(engine, 30, Phase.RESULTS)
    observe(engine, 31)
    observe(engine, 60)
    accept(engine, first, 60)
    assert submit(engine, 60) is None  # Its timer qualifies, success interval does not.
    assert submit(engine, 88.9) is None
    assert submit(engine, 89) is not None


def test_success_guard_uses_monotonic_even_when_wall_clock_jumps():
    engine = ready()
    observe(engine, 0)
    first = submit(engine, 29)
    accept(engine, first, 29)
    observe(engine, 30, Phase.RESULTS)
    observe(engine, 31)
    engine.update(Telemetry(MAP, Phase.PLAYING), 59.9, 100000)
    assert engine.next_submission(59.9, 100000) is None  # Only 28.9 fresh active seconds.
    engine.update(Telemetry(MAP, Phase.PLAYING), 60, 100000)
    assert engine.next_submission(60, 100000) is not None


def test_durable_history_prevents_repeated_unplayed_force():
    engine = ready()
    engine.set_history(TRACK.identity, 900)
    with pytest.raises(DuplicateSuppressed):
        submit(engine, 0, manual=True)
    observe(engine, 0)
    assert submit(engine, 29) is not None


def test_queued_force_then_gameplay_cannot_create_second_pending_submission():
    engine = ready()
    forced = submit(engine, 0, manual=True)
    engine.mark_pending(forced)
    observe(engine, 1)
    assert submit(engine, 40) is None
    accept(engine, forced, 40)
    assert engine.suppressed
    observe(engine, 41, Phase.RESULTS)
    observe(engine, 42)
    assert submit(engine, 71) is not None


def test_force_credit_survives_watcher_restart_and_is_consumed_once():
    engine = ready()
    forced = submit(engine, 0, manual=True)
    accept(engine, forced, 0)
    saved = engine.dump_force_credits()
    assert len(saved) == 1
    restored = ready()
    restored.restore_force_credits(saved)
    restored.set_history(TRACK.identity, 1000)
    observe(restored, 100)
    assert restored.dump_force_credits() == []
    assert restored.suppressed
    assert submit(restored, 129) is None
    observe(restored, 130, Phase.RESULTS)
    observe(restored, 131)
    assert not restored.suppressed
    assert submit(restored, 160) is not None


def test_restored_force_aliases_block_unplayed_fallback_without_recording_history_binding():
    engine = ready()
    forced = submit(engine, 0, manual=True)
    accept(engine, forced, 0)
    restored = PlaybackEngine()
    restored.restore_force_credits(engine.dump_force_credits())
    observe(restored, 10, Phase.MENU)
    restored.bind_track(Track.fallback(MAP))
    with pytest.raises(DuplicateSuppressed) as error:
        submit(restored, 100, manual=True)
    assert str(error.value) == DUPLICATE_MESSAGE


def test_accepted_alias_history_survives_consumed_force_credit_and_restart():
    engine = ready()
    forced = submit(engine, 0, manual=True)
    accept(engine, forced, 0)
    observe(engine, 1)
    assert engine.dump_force_credits() == []
    saved = engine.dump_history()
    restored = PlaybackEngine()
    restored.restore_history(saved)
    observe(restored, 100, Phase.MENU)
    restored.bind_track(Track.fallback(MAP))
    with pytest.raises(DuplicateSuppressed):
        submit(restored, 100, manual=True)
    observe(restored, 101)
    assert submit(restored, 130) is not None


def test_pending_force_survives_restart_acceptance_suppresses_its_next_attempt():
    previous = ready()
    forced = submit(previous, 0, manual=True)
    restored = ready()
    restored.restore_pending(forced)
    assert submit(restored, 10, manual=True) is None
    observe(restored, 11)
    assert submit(restored, 40) is None
    accept(restored, forced, 40)
    assert restored.suppressed
    assert restored.dump_force_credits() == []
    assert submit(restored, 70) is None
    observe(restored, 71, Phase.RESULTS)
    observe(restored, 72)
    assert submit(restored, 101) is not None


def test_pending_force_restored_before_play_retains_credit_if_accepted_in_menu():
    previous = ready()
    forced = submit(previous, 0, manual=True)
    restored = ready()
    restored.restore_pending(forced)
    accept(restored, forced, 10)
    assert len(restored.dump_force_credits()) == 1
    observe(restored, 20)
    assert restored.dump_force_credits() == []
    assert restored.suppressed


def test_late_recording_identity_attaches_restored_pending_force_to_current_attempt():
    previous = ready()
    forced = submit(previous, 0, manual=True)
    restored = PlaybackEngine()
    restored.restore_pending(forced)
    differently_named_map = Beatmap("romanized", "Romanized title", "Romanized artist")
    observe(restored, 1, beatmap=differently_named_map)
    restored.bind_track(TRACK)
    accept(restored, forced, 40)
    assert restored.suppressed
    assert restored.dump_force_credits() == []
    assert submit(restored, 80) is None


def test_pending_aliases_block_unresolved_fallback_force_after_restart():
    canonical = Track("Canonical title", "Canonical artist", "Album", "Canonical artist",
                      180, recording_id="canonical-recording")
    previous = ready()
    previous.bind_track(canonical)
    forced = submit(previous, 0, manual=True)
    saved_aliases = previous.submission_aliases(forced)
    restored = PlaybackEngine()
    restored.restore_pending(forced, aliases=saved_aliases)
    observe(restored, 1, Phase.MENU)
    restored.bind_track(Track.fallback(MAP))
    assert submit(restored, 100, manual=True) is None
    observe(restored, 101)
    accept(restored, forced, 110)
    assert restored.suppressed
    assert restored.dump_force_credits() == []


def test_explicit_failed_manual_retry_attaches_to_already_started_future_attempt():
    engine = ready()
    forced = submit(engine, 0, manual=True)
    engine.mark_failed(forced)
    observe(engine, 1)
    engine.restore_pending(forced)
    assert submit(engine, 40) is None
    accept(engine, forced, 40)
    assert engine.suppressed
    assert engine.dump_force_credits() == []
    assert submit(engine, 80) is None
    observe(engine, 81, Phase.RESULTS)
    observe(engine, 82)
    assert submit(engine, 111) is not None


def test_submission_aliases_reconstruct_submitted_identity_when_not_issued():
    previous = ready()
    forced = submit(previous, 0, manual=True)
    restored = PlaybackEngine()
    assert set(restored.submission_aliases(forced)) == {TRACK.identity, MAP.track_key}


def test_terminal_ignored_delivery_consumes_attempt_without_success_history():
    engine = ready()
    observe(engine, 0)
    rejected = submit(engine, 29)
    engine.mark_failed(rejected)
    assert submit(engine, 100) is None
    observe(engine, 101, Phase.RESULTS)
    observe(engine, 102)
    assert submit(engine, 131) is not None


def test_standard_threshold_duration_rules_and_29_second_minimum():
    engine = ready("standard")
    observe(engine, 0)
    assert submit(engine, 89.9) is None
    assert submit(engine, 90) is not None
    for duration, required in [(30, None), (31, 29), (60, 30), (600, 240)]:
        current = ready("standard")
        current.bind_track(Track("Song", "Artist", "Album", "Artist", duration))
        observe(current, 0)
        assert current.status()["required_seconds"] == required
        if required is None:
            assert submit(current, 1000) is None
        else:
            assert submit(current, required - 0.1) is None
            assert submit(current, required) is not None


def test_unknown_standard_duration_blocks_automatic_but_not_manual():
    engine = PlaybackEngine("standard")
    observe(engine, 0, Phase.MENU, beatmap=Beatmap("unknown", "Song", "Artist"))
    engine.bind_track(Track("Song", "Artist", "Song", "Artist"))
    assert submit(engine, 0) is None
    assert submit(engine, 0, manual=True) is not None


def test_submission_metadata_is_snapshot_of_displayed_metadata():
    engine = ready()
    forced = submit(engine, 0, manual=True)
    engine.track.album = "Edited later"
    assert forced.track.album == "Album"
    engine.track.album = "Album"
