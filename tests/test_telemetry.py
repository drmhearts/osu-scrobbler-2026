"""Source-shaped fixtures, not a claim that a live osu! process was tested."""

import asyncio
import copy
import json
from dataclasses import asdict
from pathlib import Path

import pytest
from websockets.asyncio.server import serve

from osu_scrobbler.models import Phase
from osu_scrobbler.telemetry import TosuAdapter, _project, TOKEN_FILTERS, V2_FILTERS


@pytest.fixture
def frames():
    path = Path(__file__).parent / "fixtures" / "tosu_v2_states.json"
    return {frame["name"]: frame for frame in json.loads(path.read_text(encoding="utf-8"))}


def snapshot(adapter, frame):
    return adapter.parse(frame["payload"], companion=frame["tokens"])


def test_selection_before_gameplay_preserves_unicode_identifiers_and_audio_duration(frames):
    event = snapshot(TosuAdapter(), frames["selected"])
    assert event.phase is Phase.MENU
    assert event.attempt_token is None
    assert event.beatmap.title_unicode == "防人想歌"
    assert event.beatmap.artist_unicode == "onoken feat. 夏川陽子"
    assert event.beatmap.map_id == 12345
    assert event.beatmap.set_id == 6789
    assert event.beatmap.difficulty == "Hyper"
    assert event.beatmap.duration == 112  # lastObject is 95 seconds, not audio length.


def test_missing_audio_length_is_unknown_not_last_object(frames):
    payload = copy.deepcopy(frames["selected"]["payload"])
    del payload["beatmap"]["time"]["mp3Length"]
    payload["beatmap"]["id"] = 0
    payload["beatmap"]["set"] = -1
    event = TosuAdapter().parse(payload)
    assert event.beatmap.duration is None
    assert event.beatmap.map_id is None
    assert event.beatmap.set_id is None


def test_attempt_token_survives_pause_but_changes_on_retry(frames):
    adapter = TosuAdapter()
    snapshot(adapter, frames["selected"])
    started = snapshot(adapter, frames["playing"])
    progress = snapshot(adapter, frames["playing_progress"])
    paused = snapshot(adapter, frames["paused"])
    resumed = snapshot(adapter, frames["resumed"])
    assert started.attempt_token is not None
    assert started.attempt_token == progress.attempt_token == paused.attempt_token == resumed.attempt_token
    assert paused.phase is Phase.PAUSED
    assert resumed.phase is Phase.PLAYING
    assert snapshot(adapter, frames["retry"]).attempt_token != started.attempt_token


@pytest.mark.parametrize("name,phase", [("failed", Phase.FAILED), ("results", Phase.RESULTS), ("replay", Phase.MENU), ("lazer", Phase.PAUSED), ("exit", Phase.DISCONNECTED)])
def test_non_play_states_do_not_qualify(frames, name, phase):
    event = snapshot(TosuAdapter(), frames[name])
    assert event.phase is phase
    if name == "exit":
        assert event.connected is False


def test_missing_companion_and_pause_evidence_never_counts_gameplay(frames):
    adapter = TosuAdapter()
    payload = copy.deepcopy(frames["playing"]["payload"])
    missing_companion = adapter.parse(payload)
    assert missing_companion.phase is Phase.PAUSED
    assert missing_companion.interval_reliable is False
    del payload["game"]["paused"]
    missing_pause = adapter.parse(payload, companion=frames["playing"]["tokens"])
    assert missing_pause.phase is Phase.PAUSED
    assert missing_pause.interval_reliable is False


def test_known_pause_is_distinct_from_an_unknown_sample_interval(frames):
    adapter = TosuAdapter()
    snapshot(adapter, frames["playing"])
    paused = snapshot(adapter, frames["paused"])
    assert paused.phase is Phase.PAUSED
    assert paused.interval_reliable is True
    unknown = adapter.parse(frames["paused"]["payload"])
    assert unknown.phase is Phase.PAUSED
    assert unknown.interval_reliable is False


def test_failure_without_personal_play_evidence_does_not_credit_unknown_time(frames):
    event = TosuAdapter().parse(frames["failed"]["payload"])
    assert event.phase is Phase.FAILED
    assert event.interval_reliable is False


@pytest.mark.parametrize("name", ["replay", "lazer", "exit"])
def test_ineligible_playback_never_proves_the_preceding_interval(frames, name):
    event = snapshot(TosuAdapter(), frames[name])
    assert event.interval_reliable is False


@pytest.mark.parametrize("change", [{"md5": "another map"}, {"time": 100}, {"status": 8}, {"rawStatus": 5}, {"status": 1}])
def test_companion_must_describe_same_personal_play(frames, change):
    companion = frames["playing"]["tokens"] | change
    assert TosuAdapter().parse(frames["playing"]["payload"], companion=companion).phase is not Phase.PLAYING


def test_spectator_evidence_excludes_play_without_requiring_login(frames):
    payload = copy.deepcopy(frames["playing"]["payload"])
    payload["profile"]["banchoStatus"]["number"] = 6
    assert TosuAdapter().parse(payload, companion=frames["playing"]["tokens"]).phase is Phase.PAUSED
    payload["profile"]["banchoStatus"]["number"] = 0
    payload["play"]["playerName"] = "another player"
    assert TosuAdapter().parse(payload, companion=frames["playing"]["tokens"]).phase is Phase.PAUSED
    payload["profile"]["name"] = ""
    payload["play"]["playerName"] = ""
    assert TosuAdapter().parse(payload, companion=frames["playing"]["tokens"]).phase is Phase.PLAYING


def test_error_disconnect_preserves_attempt_and_unknown_states_do_not_count(frames):
    adapter = TosuAdapter()
    first = snapshot(adapter, frames["playing"])
    lost = adapter.parse({"error": "not_ready"})
    assert lost.phase is Phase.DISCONNECTED and not lost.connected
    assert snapshot(adapter, frames["playing"]).attempt_token == first.attempt_token
    payload = copy.deepcopy(frames["playing"]["payload"])
    payload["state"] = {"number": 123, "name": "some future state"}
    assert adapter.parse(payload, companion=frames["playing"]["tokens"]).phase is Phase.MENU


def test_difficulty_change_uses_distinct_selection_and_attempt(frames):
    adapter = TosuAdapter()
    first = snapshot(adapter, frames["playing"])
    payload = copy.deepcopy(frames["playing"]["payload"])
    payload["beatmap"]["checksum"] = "another difficulty checksum"
    payload["beatmap"]["version"] = "Insane"
    companion = frames["playing"]["tokens"] | {"md5": payload["beatmap"]["checksum"]}
    second = adapter.parse(payload, companion=companion)
    assert first.beatmap.selection_key != second.beatmap.selection_key
    assert first.beatmap.track_key == second.beatmap.track_key
    assert first.attempt_token != second.attempt_token


def test_new_map_midplay_is_not_a_retry_of_previous_selection(frames):
    adapter = TosuAdapter()
    snapshot(adapter, frames["playing"])
    snapshot(adapter, frames["playing_progress"])
    payload = copy.deepcopy(frames["playing_progress"]["payload"])
    payload["beatmap"]["checksum"] = "another selection"
    payload["beatmap"]["time"]["live"] = 15000
    companion = frames["playing"]["tokens"] | {"md5": "another selection", "time": 15}
    event = adapter.parse(payload, companion=companion)
    assert event.phase is Phase.PAUSED
    assert event.attempt_token is None


@pytest.mark.parametrize("url", ["ws://example.org/websocket/v2", "http://localhost/websocket/v2", "ws://localhost/ws", "ws://user:secret@localhost/websocket/v2", "ws://localhost/websocket/v2?command=x"])
def test_local_only_endpoint(url):
    with pytest.raises(ValueError):
        TosuAdapter(url)


@pytest.mark.asyncio
async def test_stream_reconnects_after_actual_local_socket_loss(frames):
    connections = 0

    async def handler(socket):
        nonlocal connections
        if socket.request.path == "/tokens":
            while True:
                await socket.send(json.dumps(frames["playing"]["tokens"]))
                await asyncio.sleep(0.005)
        else:
            connections += 1
            await asyncio.sleep(0.01)
            await socket.send(json.dumps(frames["playing"]["payload"]))
            await socket.close()

    async with serve(handler, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        adapter = TosuAdapter(f"ws://127.0.0.1:{port}/websocket/v2", reconnect_min=0.01, reconnect_max=0.02)
        iterator = adapter.stream()
        first = await asyncio.wait_for(anext(iterator), 1)
        loss = await asyncio.wait_for(anext(iterator), 1)
        second = await asyncio.wait_for(anext(iterator), 1)
        await iterator.aclose()
    assert first.phase is Phase.PLAYING
    assert loss.phase is Phase.DISCONNECTED and not loss.connected
    assert second.phase is Phase.PLAYING
    assert first.attempt_token == second.attempt_token
    assert connections == 2


async def test_stream_requests_minimal_fields_and_drops_extra_personal_data(frames):
    requests = {}
    tokens_ready = asyncio.Event()
    payload = copy.deepcopy(frames["playing"]["payload"])
    payload["profile"]["name"] = "PrivatePlayer"
    payload["play"]["playerName"] = "PrivatePlayer"
    payload["profile"]["userId"] = 999
    payload["play"]["score"] = 987654321
    payload["directPath"] = {"beatmapFile": "C:/Users/Private/map.osu", "beatmapAudio": "private.mp3"}
    tokens = frames["playing"]["tokens"] | {"username": "PrivatePlayer", "score": 987654321}
    async def handler(socket):
        request = await socket.recv()
        assert request.startswith("applyFilters:")
        requests[socket.request.path] = json.loads(request.removeprefix("applyFilters:"))
        if socket.request.path == "/tokens":
            # Emulate an older source that ignores filters: extras must be discarded locally.
            await socket.send(json.dumps(tokens))
            tokens_ready.set()
        else:
            await tokens_ready.wait()
            await asyncio.sleep(0.01)
            await socket.send(json.dumps(payload))
        await socket.wait_closed()
    async with serve(handler, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        adapter = TosuAdapter(f"ws://127.0.0.1:{port}/websocket/v2")
        stream = adapter.stream()
        try:
            event = await asyncio.wait_for(anext(stream), 1)
            assert event.phase is Phase.PLAYING
            encoded = json.dumps(asdict(event))
            assert "Private" not in encoded and "987654321" not in encoded
            assert event.beatmap.audio_path == ""
            assert set(adapter._companion) == {"status", "rawStatus", "md5", "time"}
            assert requests["/tokens"] == TOKEN_FILTERS
            filtered = _project(payload, requests["/websocket/v2"])
            assert "directPath" not in filtered
            assert set(filtered["profile"]) == {"name", "banchoStatus"}
            assert set(filtered["play"]) == {"playerName", "failed"}
            assert requests["/websocket/v2"] == V2_FILTERS
        finally:
            await stream.aclose()


def test_initial_midplay_waits_for_observed_start_and_reconnect_retains_attempt(frames):
    adapter = TosuAdapter()
    late = snapshot(adapter, frames["playing_progress"])
    assert late.phase is Phase.PAUSED
    assert late.attempt_token is None
    first = snapshot(adapter, frames["playing"])
    snapshot(adapter, frames["playing_progress"])
    adapter.disconnected()
    resumed = snapshot(adapter, frames["resumed"])
    assert resumed.phase is Phase.PLAYING
    assert resumed.attempt_token == first.attempt_token
    adapter.disconnected()
    retried = snapshot(adapter, frames["retry"])
    assert retried.phase is Phase.PLAYING
    assert retried.attempt_token != first.attempt_token


@pytest.mark.asyncio
async def test_stale_companion_does_not_count_and_v2_stall_disconnects(frames, monkeypatch):
    from types import SimpleNamespace
    from osu_scrobbler import telemetry

    # Control freshness independently of Windows timer resolution and socket scheduling.
    clock = [0.0]
    monkeypatch.setattr(telemetry, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    send_stale = asyncio.Event()

    async def handler(socket):
        if socket.request.path == "/tokens":
            await socket.send(json.dumps(frames["playing"]["tokens"]))
        else:
            async with asyncio.timeout(3):
                while adapter._companion is None:
                    await asyncio.sleep(0)
            await socket.send(json.dumps(frames["playing"]["payload"]))
            await send_stale.wait()
            await socket.send(json.dumps(frames["playing"]["payload"]))
        # Leave both sockets open: loss must come from the receive timeout.
        await socket.wait_closed()

    async with serve(handler, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        adapter = TosuAdapter(f"ws://127.0.0.1:{port}/websocket/v2", reconnect_min=10, reconnect_max=10, stale_after=1)
        iterator = adapter.stream()
        try:
            first = await asyncio.wait_for(anext(iterator), 3)
            clock[0] = 2.0
            send_stale.set()
            stale = await asyncio.wait_for(anext(iterator), 3)
            loss = await asyncio.wait_for(anext(iterator), 3)
        finally:
            await iterator.aclose()
    assert first.phase is Phase.PLAYING
    assert stale.phase is Phase.PAUSED
    assert loss.phase is Phase.DISCONNECTED


@pytest.mark.parametrize("invalid", [float("nan"), float("inf"), "112000", -1, 0, True, 10**400])
def test_invalid_duration_remains_unknown(frames, invalid):
    payload = copy.deepcopy(frames["selected"]["payload"])
    payload["beatmap"]["time"]["mp3Length"] = invalid
    assert TosuAdapter().parse(payload).beatmap.duration is None
