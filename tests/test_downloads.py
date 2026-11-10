"""Isolated libraries and fake sources only: never touch a player's installation."""

import asyncio
from dataclasses import replace
from datetime import datetime, timezone
import io
import json
from pathlib import Path
from types import SimpleNamespace
from zipfile import ZipFile, ZipInfo, ZIP_STORED

import pytest

from osu_scrobbler.cli import parser
from osu_scrobbler.downloads.archive import archive_ids, metadata_ids, publish
from osu_scrobbler.downloads.commands import configure, describe, menu
from osu_scrobbler.downloads.library import scan, scan_all
from osu_scrobbler.downloads.limiter import Limiter, retry_seconds
from osu_scrobbler.downloads.models import AuthRequired, Destination, DownloadError, Filters, RateLimited, TransientError
from osu_scrobbler.downloads.service import Engine, import_archive, worker, worker_active
from osu_scrobbler.downloads.store import Store


def map_text(set_id=101, mode=0):
    return f"osu file format v14\n[General]\nMode:{mode}\n[Metadata]\nTitle:曲\nArtist:歌手\nBeatmapSetID:{set_id}\n".encode("utf-8-sig")


def osz(set_id=101, mode=0):
    output = io.BytesIO()
    with ZipFile(output, "w", ZIP_STORED) as archive:
        for name, data in (("Artist - 曲.osu", map_text(set_id, mode)), ("video.mp4", b"video-media"),
                           ("background.jpg", b"background-media"), ("storyboard.osb", b"[Events]\n")):
            archive.writestr(ZipInfo(name, (2000, 1, 1, 0, 0, 0)), data)
    return output.getvalue()


def item(set_id=101, **changes):
    return {"id": set_id, "status": "ranked", "ranked_date": "2026-11-09T23:00:00Z",
            "genre_id": 3, "creator": "Mapper", "user_id": 123, "artist": "歌手", "title": "曲",
            "beatmaps": [{"mode_int": 0, "total_length": 180}], **changes}


@pytest.fixture(autouse=True)
def no_real_installations(monkeypatch):
    monkeypatch.setattr("osu_scrobbler.downloads.service.discover", lambda: [])
    monkeypatch.setattr("osu_scrobbler.downloads.commands.discover", lambda: [])


@pytest.fixture
def stable(tmp_path):
    root = tmp_path / "Stable"
    (root / "Songs").mkdir(parents=True)
    (root / "Downloads").mkdir()
    return Destination("stable", str(root))


class FastLimiter(Limiter):
    spacing = request_spacing = 0
    hourly = 10000


class Source:
    def __init__(self, maps=None, *, error=None, account="42", content=None):
        self.maps = [item()] if maps is None else maps
        self.error, self.account, self.content = error, account, content
        self.downloads, self.searches, self.connections = [], [], 0
        self.cancelled = False

    async def connect(self):
        self.connections += 1
        return self.account

    async def search(self, filters, cursor):
        self.searches.append(cursor)
        return self.maps, None

    async def details(self, set_id):
        return item(set_id)

    async def download(self, set_id, path, progress):
        self.downloads.append(set_id)
        path.write_bytes(self.content if self.content is not None else osz(set_id))
        if self.error:
            raise self.error
        progress(path.stat().st_size)


@pytest.fixture
def checkpoint(tmp_path):
    store = Store(tmp_path / "state")
    limiter = FastLimiter(tmp_path / "shared")
    try:
        yield store, limiter
    finally:
        limiter.close()
        store.close()


@pytest.mark.parametrize("mode,number", [("osu", 0), ("taiko", 1), ("catch", 2), ("mania", 3)])
def test_mode_and_length_must_match_one_difficulty(mode, number):
    filters = Filters(mode=mode, min_length=120, max_length=200).validate()
    assert filters.matches(item(beatmaps=[{"mode_int": number, "total_length": 180}]))
    assert not filters.matches(item(beatmaps=[{"mode_int": number, "total_length": 60},
                                               {"mode_int": (number + 1) % 4, "total_length": 180}]))


def test_all_filters_compose_with_unicode_and_inclusive_utc_dates():
    filters = Filters(genre="anime", mapper="ｍＡＰＰＥＲ", ranked_from="2026-11-09",
                      ranked_to="2026-11-10", min_length=180, max_length=180)
    assert filters.matches(item())
    assert not filters.matches(item(creator="Different"))
    assert not filters.matches(item(genre_id=4))
    assert not filters.matches(item(status="loved"))
    assert not filters.matches(item(ranked_date=None))
    assert filters.matches(item(ranked_date="2026-11-10T01:00:00+02:00"))
    assert not filters.matches(item(ranked_date="2026-11-11T00:00:00Z"))
    assert replace(filters, mapper="123").matches(item())


@pytest.mark.parametrize("category", ["qualified", "pending", "wip", "graveyard"])
def test_categories_without_ranked_dates_reject_date_filter(category):
    with pytest.raises(ValueError, match="no ranked date"):
        Filters(category=category, ranked_from="2026-11-01").validate()
    assert Filters(category=category).query()["sort"] == "updated_desc"


@pytest.mark.parametrize("values", [{"limit": 0}, {"limit": True}, {"min_length": float("nan")},
                                    {"min_length": 10, "max_length": 2}, {"ranked_from": "2026-2-3"},
                                    {"ranked_from": "2026-11-10", "ranked_to": "2026-11-09"}])
def test_invalid_filters_are_rejected_before_requests(values):
    with pytest.raises(ValueError):
        Filters(**values).validate()


def test_default_query_and_any_date_semantics():
    assert Filters().query() == {"m": "0", "s": "ranked", "sort": "ranked_desc"}
    assert Filters(category="any", ranked_from="2026-01-01").matches(item())
    assert not Filters(category="any", ranked_from="2026-01-01").matches(item(status="qualified", ranked_date=None))
    assert Filters(category="any").matches(item(status="qualified", ranked_date=None))
    assert "creator=" in Filters(mapper='O"Brien').query()["q"]
    assert Filters(mapper="123").query()["q"] == "creator=123"


def test_renamed_stable_maps_and_archives_and_pending_downloads_are_read_only(stable):
    songs = stable.songs
    renamed = songs / "arbitrarily renamed"
    renamed.mkdir()
    (renamed / "custom.osu").write_bytes(map_text(101))
    (songs / "renamed archive.osz").write_bytes(osz(102))
    (songs.parent / "Downloads" / "pending.osz").write_bytes(osz(103))
    sentinel = songs / "do not touch.txt"
    sentinel.write_text("keep", encoding="utf-8")
    before = {str(p): p.read_bytes() for p in songs.parent.rglob("*") if p.is_file()}
    assert scan(stable) == {101, 102, 103}
    assert before == {str(p): p.read_bytes() for p in songs.parent.rglob("*") if p.is_file()}


def test_lazer_hash_storage_and_exports_read_metadata_without_realm(tmp_path, monkeypatch):
    root = tmp_path / "lazer"
    (root / "files" / "a" / "ab").mkdir(parents=True)
    (root / "files" / "a" / "ab" / ("a" * 64)).write_bytes(map_text(201, 3))
    (root / "files" / "media").write_bytes(b"not a beatmap")
    (root / "exports").mkdir()
    (root / "exports" / "renamed.osz").write_bytes(osz(202))
    realm = root / "client.realm"
    realm.write_bytes(b"never-open-or-modify-this-database")
    original = Path.open
    def protected(path, *args, **kwargs):
        assert path != realm
        return original(path, *args, **kwargs)
    monkeypatch.setattr(Path, "open", protected)
    assert scan(Destination("lazer", str(root))) == {201, 202}
    assert original(realm, "rb").read() == b"never-open-or-modify-this-database"


def test_cached_library_scans_notice_new_files_and_renamed_metadata(stable, checkpoint):
    store, _ = checkpoint
    file = stable.songs / "map.osu"
    file.write_bytes(map_text(201))
    assert scan(stable, cache=store) == {201}
    file.rename(stable.songs / "new.osu")
    file.write_bytes(map_text(202))
    assert scan(stable, cache=store) == {201, 202}


@pytest.mark.parametrize("data", [b"partial PK", b"<html>sign in</html>", osz(999)])
def test_corrupt_or_wrong_id_archive_never_placed(tmp_path, data):
    source, target = tmp_path / "part", tmp_path / "library" / "101.osz"
    source.write_bytes(data)
    with pytest.raises(DownloadError):
        publish(source, target, 101)
    assert not target.exists()


def test_full_media_crc_and_exclusive_collision(stable, tmp_path):
    source = tmp_path / "part"
    source.write_bytes(osz())
    target = stable.songs / "101.osz"
    publish(source, target, 101)
    assert target.read_bytes() == source.read_bytes()
    with ZipFile(target) as archive:
        assert {"video.mp4", "background.jpg", "storyboard.osb"}.issubset(archive.namelist())
    target.write_bytes(b"existing unrelated file")
    with pytest.raises(DownloadError):
        publish(source, target, 101)
    assert target.read_bytes() == b"existing unrelated file"
    corrupt = bytearray(osz())
    index = corrupt.index(b"video-media")
    corrupt[index] ^= 1
    source.write_bytes(corrupt)
    with pytest.raises(DownloadError):
        archive_ids(source, expected=101, full=True)


@pytest.mark.parametrize("unsafe", ["../outside.osu", "/absolute.osu", "C:/escape.osu", "x:stream.osu"])
def test_unsafe_archive_paths_are_rejected(tmp_path, unsafe):
    file = tmp_path / "archive"
    with ZipFile(file, "w") as archive:
        archive.writestr(unsafe, map_text())
    with pytest.raises(DownloadError):
        archive_ids(file, expected=101, full=True)


async def test_newest_first_and_limit_counts_sets_not_difficulties(stable, checkpoint):
    store, limiter = checkpoint
    job = store.create(Filters(limit=1), stable)
    source = Source([item(202, beatmaps=[{"mode_int": 0, "total_length": 180}] * 5), item(201)])
    await Engine(store, limiter, source).run(job)
    assert source.downloads == [202]
    assert store.job(job)["last_id"] == 202 and store.job(job)["count"] == 1
    assert store.job(job)["state"] == "complete" and store.job(job)["pending"] == 0
    assert store.db.execute("SELECT COUNT(*) FROM queue WHERE job=? AND state='deferred'", (job,)).fetchone()[0] == 1
    assert store.receipts()[0]["state"] == "queued_for_import"


async def test_library_history_and_pending_queue_deduplicate(stable, checkpoint):
    store, limiter = checkpoint
    (stable.songs / "renamed.osz").write_bytes(osz(101))
    old = store.create(Filters(), stable)
    store.page(old, [item(102)], None)
    job = store.create(Filters(), stable)
    source = Source([item(101), item(102), item(103), item(103)])
    await Engine(store, limiter, source).run(job)
    assert source.downloads == [103]
    assert store.known() == limiter.known() == {103}
    again = store.create(Filters(), stable)
    source = Source([item(103)])
    await Engine(store, limiter, source).run(again)
    assert not source.downloads


async def test_duplicate_detection_in_other_configured_client(stable, tmp_path, checkpoint):
    store, limiter = checkpoint
    lazer = tmp_path / "lazer"
    (lazer / "files").mkdir(parents=True)
    (lazer / "files" / "hash").write_bytes(map_text())
    store.remember("libraries", [Destination("lazer", str(lazer)).to_dict()])
    job = store.create(Filters(), stable)
    source = Source()
    await Engine(store, limiter, source).run(job)
    assert not source.downloads and store.job(job)["count"] == 0


async def test_interrupted_archive_restarts_only_that_set_after_restart(stable, checkpoint):
    store, limiter = checkpoint
    job = store.create(Filters(), stable)
    failed = Source(content=b"partial PK")
    await Engine(store, limiter, failed).run(job)
    assert store.job(job)["state"] == "error" and not store.known()
    good = Source()
    await Engine(store, limiter, good).run(job)
    assert good.downloads == [101] and store.job(job)["count"] == 1


async def test_complete_staged_download_recovers_without_new_download(stable, checkpoint):
    store, limiter = checkpoint
    job = store.create(Filters(), stable)
    store.page(job, [item()], None)
    stage = store.root / "staging"
    stage.mkdir()
    (stage / f"job-{job}-set-101.part").write_bytes(osz())
    store.transition(job, 101, "downloading")
    source = Source()
    await Engine(store, limiter, source).run(job)
    assert source.downloads == [] and store.known() == {101}


async def test_crash_after_placement_before_checkpoint_is_recovered(stable, checkpoint):
    store, limiter = checkpoint
    job = store.create(Filters(), stable)
    store.page(job, [item()], None)
    store.transition(job, 101, "validated")
    (stable.songs / "101.osz").write_bytes(osz())
    source = Source()
    await Engine(store, limiter, source).run(job)
    assert not source.downloads and store.job(job)["count"] == 1
    assert store.known() == limiter.known() == {101}


@pytest.mark.parametrize("control,state", [("pause", "paused"), ("cancel", "cancelled")])
async def test_pause_cancel_during_transfer_preserves_queue_and_resumes(stable, checkpoint, control, state):
    store, limiter = checkpoint
    job = store.create(Filters(), stable)
    entered = asyncio.Event()
    class Blocked(Source):
        async def download(self, set_id, path, progress):
            path.write_bytes(b"partial PK")
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                self.cancelled = True
    blocked = Blocked()
    task = asyncio.create_task(Engine(store, limiter, blocked).run(job))
    await asyncio.wait_for(entered.wait(), 2)
    store.control(job, control)
    await asyncio.wait_for(task, 2)
    assert blocked.cancelled and store.job(job)["state"] == state
    assert not list(stable.songs.glob("*.osz")) and not store.known()
    store.control(job, "run")
    good = Source()
    await Engine(store, limiter, good).run(job)
    assert good.downloads == [101] and store.job(job)["count"] == 1


@pytest.mark.parametrize("error,state", [(AuthRequired("Sign in again"), "auth_required"),
                                         (RateLimited(120), "cooldown"), (TransientError(60), "cooldown")])
async def test_expired_auth_rate_limits_and_transient_errors_keep_progress(stable, checkpoint, error, state):
    store, limiter = checkpoint
    job = store.create(Filters(), stable)
    await Engine(store, limiter, Source(error=error, content=b"partial")).run(job)
    assert store.job(job)["state"] == state and store.job(job)["pending"] == 1
    assert store.job(job)["count"] == 0 and not store.receipts()
    if state == "cooldown":
        assert limiter.ready_at("another-account") > limiter.clock()


async def test_cooldown_stops_even_connection_requests_after_resume(stable, checkpoint):
    store, limiter = checkpoint
    job = store.create(Filters(), stable)
    limiter.cooldown(3600)
    source = Source()
    task = asyncio.create_task(Engine(store, limiter, source).run(job))
    await asyncio.sleep(0.03)
    store.control(job, "pause")
    await asyncio.wait_for(task, 2)
    assert source.connections == 0 and source.downloads == []


async def test_changed_account_cannot_resume_original_job(stable, checkpoint):
    store, limiter = checkpoint
    job = store.create(Filters(), stable)
    store.update(job, account="42")
    source = Source(account="43")
    await Engine(store, limiter, source).run(job)
    assert not source.downloads and not source.searches
    assert store.job(job)["state"] == "auth_required"


def test_shared_persistent_account_and_global_limiter_and_retry_after(tmp_path):
    now = [10000]
    first = Limiter(tmp_path, clock=lambda: now[0])
    second = Limiter(tmp_path, clock=lambda: now[0])
    try:
        assert first.reserve("42", 101) is None
        assert second.reserve("42", 101) == now[0] + 60
        assert second.reserve("43", 102) == now[0] + 60
        now[0] += 60
        assert second.reserve("42", 102) is None
        for n in range(28):
            now[0] += 60
            assert first.reserve("42", 103 + n) is None
        now[0] += 60
        assert first.ready_at("42") == 13600
        first.cooldown(7200)
        assert second.ready_at("43", "connect") == now[0] + 7200
        assert retry_seconds("120") == 120
        assert retry_seconds("Thu, 01 Jan 1970 03:20:00 GMT", now=10000) == 2000
        assert retry_seconds("nonsense") == 3600
    finally:
        first.close()
        second.close()


async def test_lazer_handoff_preserves_full_archive_and_reports_queue_not_import(tmp_path, checkpoint):
    store, limiter = checkpoint
    root = tmp_path / "lazer"
    (root / "files").mkdir(parents=True)
    (root / "client.realm").write_bytes(b"unchanged")
    exe = tmp_path / "osu!.exe"
    exe.touch()
    destination = Destination("lazer", str(root), str(exe))
    job = store.create(Filters(), destination)
    handed = []
    await Engine(store, limiter, Source(), importer=lambda dst, path: handed.append((dst, path)) or True).run(job)
    assert len(handed) == 1 and handed[0][0].executable == str(exe)
    assert handed[0][1].read_bytes() == osz()
    assert store.receipts()[0]["state"] == "queued_for_import"
    assert (root / "client.realm").read_bytes() == b"unchanged"
    assert list((root / "files").iterdir()) == []
    store.confirm_lazer(101)
    assert store.receipts()[0]["state"] == "imported_confirmed_by_user"


async def test_lazer_without_executable_is_downloaded_and_handoff_can_retry(tmp_path, checkpoint):
    store, limiter = checkpoint
    root = tmp_path / "lazer"
    (root / "files").mkdir(parents=True)
    job = store.create(Filters(), Destination("lazer", str(root)))
    await Engine(store, limiter, Source()).run(job)
    assert store.receipts()[0]["state"] == "downloaded"
    assert not import_archive(Destination("lazer", str(root)), store.receipts()[0]["path"])


async def test_second_worker_cannot_stop_first_or_download_same_map(stable, tmp_path, monkeypatch):
    from osu_scrobbler.downloads import service
    store = Store(tmp_path / "profile" / "beatmap-downloads")
    job = store.create(Filters(), stable)
    entered, proceed = asyncio.Event(), asyncio.Event()
    class Session(Source):
        async def __aenter__(self):
            return self
        async def __aexit__(self, *args):
            pass
        async def download(self, set_id, path, progress):
            entered.set()
            await proceed.wait()
            await super().download(set_id, path, progress)
    session = Session()
    monkeypatch.setattr(service, "Limiter", FastLimiter)
    shared = tmp_path / "shared"
    task = asyncio.create_task(worker(tmp_path / "profile", job, source_factory=lambda _: session, shared=shared))
    try:
        await asyncio.wait_for(entered.wait(), 2)
        assert worker_active(shared)
        assert await worker(tmp_path / "profile", job, source_factory=lambda _: session, shared=shared) is False
        assert store.job(job)["control"] == "run"
        proceed.set()
        await task
        assert session.downloads == [101] and not worker_active(shared)
    finally:
        proceed.set()
        await task
        store.close()


def test_cli_preferences_filters_and_compact_status(stable, checkpoint):
    store, _ = checkpoint
    configure(store, "stable", stable.root)
    assert configure(store).to_dict() == stable.to_dict()
    job = store.create(Filters(mode="mania", mapper="Japanese 名"), stable)
    assert store.preference("filters")["mapper"] == "Japanese 名"
    assert "0/25 sets" in describe(store, job)["message"]
    args = parser().parse_args(["maps", "start", "--mode", "catch", "--limit", "5", "--genre", "anime"])
    assert args.mode == "catch" and args.limit == 5


async def test_menu_has_all_controls_without_mutating_library(stable, checkpoint):
    store, _ = checkpoint
    output = []
    async def back(text):
        return "0"
    await menu(store.root.parent, output=output.append, prompt=back)
    joined = "\n".join(output)
    assert all(control in joined for control in ("start recent", "filters", "pause", "resume", "cancel", "sign in", "destination", "import saved"))


async def test_restart_recovers_archive_already_imported_by_stable(stable, checkpoint):
    store, limiter = checkpoint
    job = store.create(Filters(), stable)
    store.page(job, [item()], None)
    store.transition(job, 101, "validated")
    directory = stable.songs / "renamed imported map"
    directory.mkdir()
    (directory / "map.osu").write_bytes(map_text())
    source = Source()
    await Engine(store, limiter, source).run(job)
    assert not source.downloads and store.job(job)["count"] == 1
    assert store.receipts()[0]["state"] == "imported"


def test_unsafe_windows_aliases_and_cancelled_validation_preserve_staging(tmp_path):
    from osu_scrobbler.downloads.service import Interrupted
    file = tmp_path / "part"
    for name in ("./map.osu", "CON.osu", "folder./map.osu", "a//map.osu"):
        with ZipFile(file, "w") as archive:
            archive.writestr(name, map_text())
        with pytest.raises(DownloadError):
            archive_ids(file, expected=101, full=True)
    file.write_bytes(osz())
    def cancelled():
        raise Interrupted()
    with pytest.raises(Interrupted):
        archive_ids(file, expected=101, full=True, check=cancelled)
    assert file.read_bytes() == osz()


def test_exit_pauses_jobs_and_does_not_clear_queue(stable, checkpoint):
    from osu_scrobbler.downloads.service import pause_active
    store, _ = checkpoint
    job = store.create(Filters(), stable)
    store.page(job, [item()], None)
    # Use the same directory layout as the app.
    import shutil
    parent = store.root.parent / "profile"
    target = parent / "beatmap-downloads"
    store.close()
    shutil.copytree(store.root, target)
    pause_active(parent)
    check = Store(target)
    try:
        assert check.job(job)["control"] == "pause" and check.job(job)["pending"] == 1
    finally:
        check.close()
    # Restore fixture-owned connection for its finalizer.
    store.db = __import__("sqlite3").connect(store.root / "downloads.sqlite3")


async def test_lazer_import_uses_executable_configured_after_download(tmp_path, checkpoint, monkeypatch):
    from osu_scrobbler.downloads import commands
    store, _ = checkpoint
    root = tmp_path / "lazer"
    (root / "files").mkdir(parents=True)
    job = store.create(Filters(), Destination("lazer", str(root)))
    # execute() uses the app's normal subdirectory.
    profile = tmp_path / "profile"
    (profile / "beatmap-downloads").mkdir(parents=True)
    own = Store(profile / "beatmap-downloads")
    try:
        job = own.create(Filters(), Destination("lazer", str(root)))
        own.page(job, [item()], None)
        archive = own.root / "101.osz"
        archive.write_bytes(osz())
        own.complete(job, 101, archive, "downloaded")
        exe = tmp_path / "chosen-lazer.exe"
        exe.touch()
        configure(own, "lazer", root, exe)
        handed = []
        monkeypatch.setattr(commands, "import_archive", lambda dest, path: handed.append(dest.executable) or True)
        args = parser().parse_args(["--data-dir", str(profile), "maps", "import"])
        result = await commands.execute(args)
        assert handed == [str(exe)] and "queued" in result["message"]
    finally:
        own.close()


def test_minimal_checkpoint_does_not_store_unknown_upstream_token_fields(stable, checkpoint):
    store, _ = checkpoint
    job = store.create(Filters(), stable)
    store.page(job, [item(access_token="should-never-be-stored")], None)
    raw = store.db.execute("SELECT metadata FROM queue").fetchone()[0]
    assert "should-never" not in raw and "access_token" not in raw


def test_changing_client_does_not_inherit_old_executable(stable, checkpoint, tmp_path):
    store, _ = checkpoint
    exe = Path(stable.root) / "osu!.exe"
    exe.touch()
    configure(store, "stable", stable.root, exe)
    root = tmp_path / "lazer"
    (root / "files").mkdir(parents=True)
    changed = configure(store, "lazer", root)
    assert changed.executable == ""


async def test_transient_backoff_grows_and_is_preserved_after_reopening(stable, checkpoint):
    store, limiter = checkpoint
    job = store.create(Filters(), stable)
    source = Source(error=TransientError(1), content=b"partial")
    await Engine(store, limiter, source).run(job)
    first = limiter.ready_at("42")
    assert store.job(job)["failures"] == 1
    # Simulate elapsed cooldown, not a production reset/control.
    with limiter.db:
        limiter.db.execute("DELETE FROM cooldown")
    await Engine(store, limiter, source).run(job)
    assert store.job(job)["failures"] == 2
    assert limiter.ready_at("42") - first > 50


def test_shared_limiter_directory_is_independent_of_portable_profile(tmp_path, monkeypatch):
    from osu_scrobbler.downloads.service import shared_root
    import os
    monkeypatch.setenv("LOCALAPPDATA" if os.name == "nt" else "XDG_DATA_HOME", str(tmp_path))
    assert shared_root() == tmp_path / "osu-scrobbler" / "beatmap-download-shared"


async def test_cooldown_prevents_browser_creation_after_worker_restart(stable, tmp_path):
    profile = tmp_path / "profile"
    store = Store(profile / "beatmap-downloads")
    shared = tmp_path / "shared"
    limiter = Limiter(shared)
    try:
        job = store.create(Filters(), stable)
        limiter.cooldown(3600)
        constructed = []
        task = asyncio.create_task(worker(profile, job, source_factory=lambda _: constructed.append(True), shared=shared))
        await asyncio.sleep(0.03)
        store.control(job, "cancel")
        await asyncio.wait_for(task, 2)
        assert not constructed and store.job(job)["state"] == "cancelled"
    finally:
        limiter.close()
        store.close()


def test_native_import_passes_one_archive_to_explicit_client_without_shell(tmp_path, monkeypatch):
    from osu_scrobbler.downloads import service
    root = tmp_path / "lazer"
    (root / "files").mkdir(parents=True)
    exe = tmp_path / "lazer with spaces.exe"
    exe.touch()
    archive = tmp_path / "full archive.osz"
    archive.write_bytes(osz())
    calls = []
    monkeypatch.setattr(service.subprocess, "Popen", lambda args, **kwargs: calls.append((args, kwargs)))
    assert import_archive(Destination("lazer", str(root), str(exe)), archive)
    assert calls[0][0] == [str(exe), str(archive.resolve())]
    assert calls[0][1].get("shell", False) is False


async def test_next_finite_batch_can_download_unused_candidates_from_completed_batch(stable, checkpoint):
    store, limiter = checkpoint
    first = store.create(Filters(limit=1), stable)
    source = Source([item(202), item(201)])
    await Engine(store, limiter, source).run(first)
    assert source.downloads == [202]
    second = store.create(Filters(limit=1), stable)
    following = Source([item(202), item(201)])
    await Engine(store, limiter, following).run(second)
    assert following.downloads == [201] and store.job(second)["count"] == 1
