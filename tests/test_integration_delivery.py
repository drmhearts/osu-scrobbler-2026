"""Delivery retries and terminal outcomes through the actual watcher/store."""
from types import SimpleNamespace

import pytest

from osu_scrobbler.config import Config
from osu_scrobbler.lastfm import DeliveryResult
from osu_scrobbler.models import Beatmap, Phase, Telemetry, Track
from osu_scrobbler.storage import Storage
from osu_scrobbler.watcher import Watcher


class FakeLastFm:
    def __init__(self, *results):
        self.results = list(results)
        self.calls = []
    async def submit(self, submission):
        self.calls.append(submission)
        if not self.results:
            raise AssertionError("Unexpected Last.fm delivery")
        value = self.results.pop(0)
        if isinstance(value, BaseException):
            raise value
        return value


class FakeMusicBrainz:
    async def aclose(self):
        pass


class Clock:
    now = 0.0
    def time(self):
        return 1700000000 + self.now
    def monotonic(self):
        return self.now


@pytest.fixture
def setup(tmp_path, monkeypatch):
    import osu_scrobbler.watcher as watcher_module
    clock = Clock()
    monkeypatch.setattr(watcher_module, "time", clock)
    map = Beatmap("map", "曲", "歌手", duration=180)
    track = Track("曲", "歌手", "アルバム", "歌手", 180, recording_id="recording")
    stores = []
    def make(client, config=None, dry_run=False):
        store = Storage(tmp_path / "history.sqlite3")
        stores.append(store)
        return Watcher(tmp_path, config or Config(), fixture=True, dry_run=dry_run,
                       musicbrainz=FakeMusicBrainz(), lastfm=client, storage=store, output=lambda _: None)
    async def ingest(watcher, phase, at, token=None):
        clock.now = at
        await watcher.ingest(Telemetry(map, phase, position=at, attempt_token=token), at, clock.time(), fixture_track=track)
    yield make, ingest, clock
    for store in stores:
        store.close()


@pytest.mark.asyncio
async def test_offline_retry_retains_submission_and_does_not_become_new_play(setup):
    make, ingest, clock = setup
    client = FakeLastFm(DeliveryResult("retry", "offline"), DeliveryResult("accepted", accepted=1))
    watcher = make(client)
    await ingest(watcher, Phase.MENU, 0)
    await ingest(watcher, Phase.PLAYING, 1, "attempt-1")
    await ingest(watcher, Phase.PLAYING, 30, "attempt-1")
    await watcher.deliver_once()
    queued = watcher.storage.pending()[0]
    assert watcher.storage.get(queued.id)["status"] == "retry"
    await watcher.deliver_once()
    assert len(client.calls) == 1  # Backoff excludes unchanged immediate retry.
    clock.now = 61
    await watcher.deliver_once()
    assert len(client.calls) == 2
    assert client.calls[0] == client.calls[1] == queued
    assert len(watcher.storage.history()) == 1
    assert watcher.storage.get(queued.id)["status"] == "accepted"
    assert client.calls[1].playback_start == 1700000001
    await ingest(watcher, Phase.PLAYING, 70, "attempt-1")
    assert watcher.storage.pending() == []


@pytest.mark.asyncio
async def test_uncertain_timeout_requires_explicit_delivery_retry(setup):
    make, ingest, clock = setup
    client = FakeLastFm(DeliveryResult("uncertain", "unknown outcome"), DeliveryResult("accepted", accepted=1))
    watcher = make(client)
    await ingest(watcher, Phase.MENU, 0)
    await ingest(watcher, Phase.PLAYING, 1, "attempt-1")
    await ingest(watcher, Phase.PLAYING, 30, "attempt-1")
    await watcher.deliver_once()
    item = watcher.storage.history()[0]
    assert item["status"] == "uncertain"
    clock.now = 100
    await watcher.deliver_once()
    assert len(client.calls) == 1
    assert not (await watcher.command({"command": "retry", "id": item["id"]}))["requeued"]
    assert (await watcher.command({"command": "retry", "id": item["id"], "accept_duplicate_risk": True}))["requeued"]
    await watcher.deliver_once()
    assert len(client.calls) == 2
    assert client.calls[0].id == client.calls[1].id
    assert len(watcher.storage.history()) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["ignored", "failed"])
async def test_terminal_force_failure_does_not_block_genuinely_later_attempt(setup, status):
    make, ingest, clock = setup
    client = FakeLastFm(DeliveryResult(status, "terminal rejection"))
    watcher = make(client)
    await ingest(watcher, Phase.MENU, 0)
    force = await watcher.command({"command": "force"})
    await watcher.deliver_once()
    assert watcher.storage.get(force["id"])["status"] == status
    await ingest(watcher, Phase.PLAYING, 1, "new-attempt")
    await ingest(watcher, Phase.PLAYING, 30, "new-attempt")
    assert len(watcher.storage.history()) == 2
    assert len(watcher.storage.pending()) == 1
    assert not watcher.storage.pending()[0].manual


@pytest.mark.asyncio
async def test_export_error_after_acceptance_never_requeues_or_resubmits(setup, monkeypatch):
    make, ingest, clock = setup
    client = FakeLastFm(DeliveryResult("accepted", accepted=1))
    watcher = make(client, Config(auto_export=True))
    await ingest(watcher, Phase.MENU, 0)
    await ingest(watcher, Phase.PLAYING, 1, "attempt")
    await ingest(watcher, Phase.PLAYING, 30, "attempt")
    def fail_export(_):
        raise UnicodeDecodeError("utf8", b"x", 0, 1, "invalid existing export")
    monkeypatch.setattr(watcher.storage, "export", fail_export)
    await watcher.deliver_once()
    assert watcher.storage.history()[0]["status"] == "accepted"
    assert watcher.storage.pending() == []
    clock.now = 100
    await watcher.deliver_once()
    assert len(client.calls) == 1


@pytest.mark.asyncio
async def test_dry_run_never_delivers_existing_offline_queue(setup):
    make, ingest, clock = setup
    source = make(FakeLastFm())
    await ingest(source, Phase.MENU, 0)
    await ingest(source, Phase.PLAYING, 1, "attempt")
    await ingest(source, Phase.PLAYING, 30, "attempt")
    assert len(source.storage.pending()) == 1
    client = FakeLastFm()
    dry = make(client, dry_run=True)
    await dry.deliver_once()
    assert client.calls == []
    assert len(dry.storage.pending()) == 1
    assert "error" in await dry.command({"command": "retry", "id": dry.storage.pending()[0].id})


@pytest.mark.asyncio
async def test_cancelling_inflight_delivery_records_uncertainty_immediately(setup):
    import asyncio
    make, ingest, clock = setup
    started = asyncio.Event()
    release = asyncio.Event()
    class WaitingLastFm:
        async def submit(self, submission):
            started.set()
            await release.wait()
            return DeliveryResult("accepted", accepted=1)
    watcher = make(WaitingLastFm())
    await ingest(watcher, Phase.MENU, 0)
    await ingest(watcher, Phase.PLAYING, 1, "attempt")
    await ingest(watcher, Phase.PLAYING, 30, "attempt")
    task = asyncio.create_task(watcher.deliver_once())
    await asyncio.wait_for(started.wait(), 1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert watcher.storage.history()[0]["status"] == "uncertain"
    assert watcher.storage.pending() == []


@pytest.mark.asyncio
async def test_dry_run_auth_reload_never_reads_credentials(setup, monkeypatch):
    import osu_scrobbler.watcher as watcher_module
    make, _, _ = setup
    def forbidden_credentials():
        raise AssertionError("Dry run must not read credentials")
    monkeypatch.setattr(watcher_module, "CredentialStore", forbidden_credentials)
    watcher = make(FakeLastFm(), dry_run=True)
    assert await watcher.command({"command": "auth-reload"}) == {"dry_run": True}


@pytest.mark.asyncio
async def test_auth_reload_waits_for_inflight_delivery_before_closing_client(setup, monkeypatch):
    import asyncio
    import osu_scrobbler.watcher as watcher_module
    from osu_scrobbler.lastfm import Credentials
    make, ingest, clock = setup
    started = asyncio.Event()
    release = asyncio.Event()
    class WaitingLastFm:
        closed = False
        async def submit(self, submission):
            started.set()
            await release.wait()
            assert not self.closed
            return DeliveryResult("accepted", accepted=1)
        async def aclose(self):
            self.closed = True
    client = WaitingLastFm()
    replacement = FakeLastFm()
    monkeypatch.setattr(watcher_module, "CredentialStore", lambda: SimpleNamespace(load=lambda: Credentials("key", "secret", "session")))
    monkeypatch.setattr(watcher_module, "LastFmClient", lambda credentials: replacement)
    watcher = make(client)
    await ingest(watcher, Phase.MENU, 0)
    await ingest(watcher, Phase.PLAYING, 1, "attempt")
    await ingest(watcher, Phase.PLAYING, 30, "attempt")
    delivery = asyncio.create_task(watcher.deliver_once())
    await asyncio.wait_for(started.wait(), 1)
    reload_task = asyncio.create_task(watcher.command({"command": "auth-reload"}))
    await asyncio.sleep(0)
    assert not reload_task.done()
    assert not client.closed
    release.set()
    await delivery
    assert await reload_task == {"authorized": True}
    assert client.closed
    assert watcher.lastfm is replacement


@pytest.mark.asyncio
async def test_successful_force_credit_persists_and_is_consumed_once(setup):
    make, ingest, clock = setup
    source = make(FakeLastFm(DeliveryResult("accepted", accepted=1)))
    await ingest(source, Phase.MENU, 0)
    force = await source.command({"command": "force"})
    await source.deliver_once()
    assert source.storage.get(force["id"])["status"] == "accepted"
    credits = source.storage.get_state("force_credits")
    assert credits
    restarted = make(FakeLastFm())
    restarted.engine.restore_force_credits(credits)
    await ingest(restarted, Phase.MENU, 1)
    await ingest(restarted, Phase.PLAYING, 2, "suppressed-attempt")
    await ingest(restarted, Phase.PLAYING, 31, "suppressed-attempt")
    assert restarted.engine.suppressed
    assert restarted.storage.pending() == []
    assert restarted.storage.get_state("force_credits") == []
    await ingest(restarted, Phase.MENU, 40)
    await ingest(restarted, Phase.PLAYING, 41, "later-attempt")
    await ingest(restarted, Phase.PLAYING, 70, "later-attempt")
    assert len(restarted.storage.pending()) == 1
    assert not restarted.storage.pending()[0].manual


@pytest.mark.asyncio
async def test_dry_run_does_not_consume_real_persistent_force_credits(setup):
    make, ingest, clock = setup
    source = make(FakeLastFm(DeliveryResult("accepted", accepted=1)))
    await ingest(source, Phase.MENU, 0)
    await source.command({"command": "force"})
    await source.deliver_once()
    original = source.storage.get_state("force_credits")
    assert original
    dry = make(FakeLastFm(), dry_run=True)
    dry.engine.restore_force_credits(original)
    await ingest(dry, Phase.MENU, 1)
    await ingest(dry, Phase.PLAYING, 2, "preview-attempt")
    await ingest(dry, Phase.PLAYING, 31, "preview-attempt")
    assert dry.engine.dump_force_credits() == []  # Preview consumes its own in-memory copy.
    assert dry.storage.get_state("force_credits") == original


async def test_explicit_failed_force_retry_reserves_original_delivery_again(setup):
    make, ingest, clock = setup
    watcher = make(FakeLastFm(DeliveryResult("failed", "bad session")))
    await ingest(watcher, Phase.MENU, 0)
    force = await watcher.command({"command": "force"})
    await watcher.deliver_once()
    assert watcher.storage.get(force["id"])["status"] == "failed"
    assert (await watcher.command({"command": "retry", "id": force["id"]}))["requeued"]
    await ingest(watcher, Phase.PLAYING, 1, "attempt")
    await ingest(watcher, Phase.PLAYING, 30, "attempt")
    pending = watcher.storage.pending()
    assert len(pending) == 1
    assert pending[0].id == force["id"]
    assert pending[0].manual
