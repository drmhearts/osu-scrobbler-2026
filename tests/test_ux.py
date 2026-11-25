"""User-facing flows and collection plans use isolated profiles and libraries."""

import sqlite3

import pytest

from osu_scrobbler.cli import parser, execute
from osu_scrobbler.downloads import commands
from osu_scrobbler.downloads.commands import collection_guidance, count_warning, menu, progress_bar, progress_notice
from osu_scrobbler.downloads.limiter import Limiter
from osu_scrobbler.downloads.models import AuthRequired, Destination, DownloadError, Filters
from osu_scrobbler.downloads.store import Store
from osu_scrobbler.welcome import welcome


async def scripted(values, prompts):
    iterator = iter(values)
    async def answer(label):
        prompts.append(label)
        return next(iterator)
    return answer


async def test_help_topics_and_prompt_help_preserve_destination_progress(tmp_path, monkeypatch):
    lazer = tmp_path / "lazer"
    (lazer / "files").mkdir(parents=True)
    monkeypatch.setattr(commands, "discover", lambda: [])
    prompts, output = [], []
    answer = await scripted(["7", "custom", "help collections", "wrong", "lazer", str(lazer), "", "0"], prompts)
    await menu(tmp_path, output=output.append, prompt=answer)
    saved = Store(tmp_path / "beatmap-downloads")
    try:
        assert saved.preference("destination")["client"] == "lazer"
        assert saved.preference("destination")["root"] == str(lazer)
    finally:
        saved.close()
    assert any("Type exactly 'lazer' or 'stable'" in line for line in output)
    assert any("Manage Collections" in line for line in output)
    assert sum("Which client?" in label for label in prompts) == 3
    assert "reimu" not in welcome(tmp_path).casefold()
    for topic in ("scrobble", "download", "export", "collections", "update", "privacy"):
        result = await execute(parser().parse_args(["--data-dir", str(tmp_path), "help", topic]))
        assert len(result["message"]) > 40


async def test_detected_lazer_default_and_saved_stable_preference(tmp_path, monkeypatch):
    stable = tmp_path / "Stable"
    (stable / "Songs").mkdir(parents=True)
    lazer = tmp_path / "lazer"
    (lazer / "files").mkdir(parents=True)
    monkeypatch.setattr(commands, "discover", lambda: [Destination("stable", str(stable)), Destination("lazer", str(lazer))])
    store = Store(tmp_path / "state")
    try:
        assert commands.configure(store).client == "lazer"
        commands.configure(store, "stable", stable)
        assert commands.configure(store).client == "stable"
        assert len(store.preference("libraries")) == 2
    finally:
        store.close()


async def test_filter_invalid_count_reasks_same_field_without_losing_prior_values(tmp_path):
    prompts, output = [], []
    # Default values for first five fields; count is invalid, then unrestricted.
    values = ["2", "", "", "", "", "", "0", "100000000000000000000", "", "", "", "0"]
    answer = await scripted(values, prompts)
    await menu(tmp_path, output=output.append, prompt=answer)
    saved = Store(tmp_path / "beatmap-downloads")
    try:
        assert saved.preference("filters")["limit"] == 100000000000000000000
    finally:
        saved.close()
    assert any("positive whole number" in line for line in output)
    assert sum("How many beatmapsets?" in item for item in prompts) == 2


async def test_collection_choice_is_remembered_and_can_be_changed(tmp_path, monkeypatch):
    stable = tmp_path / "Stable"
    (stable / "Songs").mkdir(parents=True)
    store = Store(tmp_path / "beatmap-downloads")
    try:
        commands.configure(store, "stable", stable)
    finally:
        store.close()
    monkeypatch.setattr(commands, "discover", lambda: [])
    called = []
    async def fake_execute(args):
        called.append(args)
        return {"message": "started"}
    monkeypatch.setattr(commands, "execute", fake_execute)
    monkeypatch.setattr(commands, "check_libraries", lambda *args: set())
    async def signed_in(*args):
        return None
    monkeypatch.setattr(commands, "ensure_signed_in", signed_in)
    prompts, output = [], []
    await menu(tmp_path, output=output.append, embedded=True,
               prompt=await scripted(["1", "yes", "existing", "Favorites"], prompts))
    assert called[-1].collection == "Favorites" and not called[-1].new_collection
    await menu(tmp_path, output=output.append, embedded=True,
               prompt=await scripted(["1", "", "new", "New maps"], prompts))
    assert called[-1].collection == "New maps" and called[-1].new_collection
    saved = Store(tmp_path / "beatmap-downloads")
    try:
        assert saved.preference("collection") == {"enabled": True, "kind": "new", "name": "New maps"}
    finally:
        saved.close()


async def test_start_keeps_terminal_open_for_sign_in_then_live_progress(tmp_path, monkeypatch):
    stable = tmp_path / "Stable"
    (stable / "Songs").mkdir(parents=True)
    store = Store(tmp_path / "beatmap-downloads")
    try:
        commands.configure(store, "stable", stable)
    finally:
        store.close()
    monkeypatch.setattr(commands, "discover", lambda: [])
    monkeypatch.setattr(commands, "shared_root", lambda: tmp_path / "shared")
    events = []

    class Browser:
        def __init__(self, directory):
            events.append("browser-open")
        async def __aenter__(self):
            return self
        async def __aexit__(self, *args):
            events.append("browser-close")
        async def connect(self):
            raise AuthRequired("Sign in")
        async def login(self, prompt):
            events.append("sign-in-prompt")
            await prompt("Sign in, then press Enter: ")

    from osu_scrobbler.downloads import browser
    monkeypatch.setattr(browser, "BrowserSource", Browser)
    async def fake_execute(args):
        events.append("worker-start")
        return {"message": "Batch started"}
    monkeypatch.setattr(commands, "execute", fake_execute)
    from osu_scrobbler import console
    async def fake_console(directory, output):
        events.append("live-view")
    monkeypatch.setattr(console, "show_console", fake_console)
    prompts, output = [], []
    await menu(tmp_path, output=output.append,
               prompt=await scripted(["1", "no", ""], prompts))
    assert events == ["browser-open", "sign-in-prompt", "browser-close", "worker-start", "live-view"]
    assert any("terminal will wait" in line for line in output)
    assert any("Sign in, then press Enter" in item for item in prompts)


async def test_bad_library_stays_in_menu_without_opening_browser(tmp_path, monkeypatch):
    stable = tmp_path / "Stable"
    (stable / "Songs").mkdir(parents=True)
    store = Store(tmp_path / "beatmap-downloads")
    try:
        commands.configure(store, "stable", stable)
    finally:
        store.close()
    monkeypatch.setattr(commands, "discover", lambda: [])
    def broken_scan(*args):
        raise DownloadError("Cannot check existing map/archive: broken.osz")
    monkeypatch.setattr(commands, "check_libraries", broken_scan)
    async def must_not_open(*args):
        pytest.fail("Browser must not open when library preflight fails")
    monkeypatch.setattr(commands, "ensure_signed_in", must_not_open)
    prompts, output = [], []
    await menu(tmp_path, output=output.append,
               prompt=await scripted(["1", "no", "0"], prompts))
    assert any("broken.osz" in line for line in output)
    assert prompts[-1].startswith("Maps >")


def test_count_warning_uses_shared_history_without_rejecting_large_batches(tmp_path):
    assert Filters(limit=10**50).validate().limit == 10**50
    with pytest.raises(ValueError, match="positive whole"):
        Filters(limit=0).validate()
    limiter = Limiter(tmp_path / "shared", clock=lambda: 10000)
    try:
        assert limiter.reserve("42", 101, "download") is None
        assert not count_warning(1, limiter)
        warning = count_warning(100, limiter)
        assert "1 archive requests" in warning and "Continuing" in warning
    finally:
        limiter.close()


def test_collection_states_are_separate_and_do_not_modify_client_database(tmp_path):
    stable = tmp_path / "Stable"
    (stable / "Songs").mkdir(parents=True)
    database = stable / "collections.db"
    database.write_bytes(b"existing collection data")
    archive = stable / "Songs" / "101.osz"
    archive.write_bytes(b"validated by the download engine")
    store = Store(tmp_path / "state")
    try:
        job = store.create(Filters(limit=1), Destination("stable", str(stable)),
                           {"kind": "existing", "name": "Favourites"})
        store.page(job, [{"id": 101}], None)
        store.complete(job, 101, archive)
        assert store.collection_members(job) == [{"set_id": 101, "state": "awaiting_import"}]
        with pytest.raises(DownloadError, match="import"):
            store.confirm_collection(job, 101)
        store.confirm_stable({101})
        assert store.collection_members(job)[0]["state"] == "manual_required"
        assert "Manage Collections" in collection_guidance(store, job)["message"]
        store.confirm_collection(job, 101)
        assert store.collection_members(job)[0]["state"] == "confirmed_by_user"
        assert database.read_bytes() == b"existing collection data"
    finally:
        store.close()


def test_lazer_collection_plan_survives_restart_and_waits_for_import(tmp_path):
    root = tmp_path / "lazer"
    (root / "files").mkdir(parents=True)
    realm = root / "client.realm"
    realm.write_bytes(b"unchanged")
    archive = tmp_path / "101.osz"
    archive.write_bytes(b"validated by the download engine")
    path = tmp_path / "state"
    store = Store(path)
    job = store.create(Filters(), Destination("lazer", str(root)), {"kind": "new", "name": "New maps"})
    store.page(job, [{"id": 101}], None)
    store.complete(job, 101, archive, state="downloaded")
    store.close()
    again = Store(path)
    try:
        assert again.job(job)["collection"] == {"name": "New maps", "kind": "new"}
        assert again.collection_members(job)[0]["state"] == "awaiting_import"
        with pytest.raises(DownloadError, match="import"):
            again.confirm_collection(job, 101)
        again.confirm_lazer(101)
        assert again.collection_members(job)[0]["state"] == "manual_required"
        assert realm.read_bytes() == b"unchanged"
    finally:
        again.close()


def test_old_checkpoint_schema_gains_collection_tables_without_losing_history(tmp_path):
    root = tmp_path / "old"
    root.mkdir()
    db = sqlite3.connect(root / "downloads.sqlite3")
    db.execute("CREATE TABLE preferences (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
    db.execute("INSERT INTO preferences VALUES ('filters', '{\"limit\": 25}')")
    db.commit()
    db.close()
    store = Store(root)
    try:
        assert store.preference("filters") == {"limit": 25}
        assert store.db.execute("SELECT name FROM sqlite_master WHERE name='collection_members'").fetchone()
    finally:
        store.close()


def test_progress_bar_uses_completed_sets_and_saved_job_state(tmp_path):
    assert progress_bar(0, 100, width=10) == "[··········] 0/100 sets  0%"
    assert progress_bar(25, 100, width=10) == "[██········] 25/100 sets  25%"
    assert progress_bar(10**50, 10**50, width=10).endswith("100%")
    root = tmp_path / "lazer"
    (root / "files").mkdir(parents=True)
    store = Store(tmp_path / "beatmap-downloads")
    try:
        job = store.create(Filters(limit=100), Destination("lazer", str(root)))
        store.update(job, state="running", count=25, message="Downloading beatmapset 123 (full media)")
    finally:
        store.close()
    notice = progress_notice(tmp_path, tick=0)
    assert "25/100 sets" in notice and "25%" in notice
    assert "Downloading beatmapset 123" in notice
    assert progress_notice(tmp_path, tick=1) != notice  # Live indicator while transfer size is unknown.
