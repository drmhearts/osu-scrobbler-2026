from datetime import datetime, timedelta, timezone
import json

import pytest

from osu_scrobbler.models import Submission, Track
from osu_scrobbler.storage import Storage


def submission(number=1, timestamp=1700000030.125):
    return Submission(f"id-{number}", f"attempt-{number}",
        Track("曲\t名\n次", "歌手\r\n名", "アルバム\u2028次", "歌手", 180, recording_id="recording"),
        1700000000, timestamp)


def test_durable_queue_is_idempotent_by_id_and_attempt(tmp_path):
    path = tmp_path / "state.sqlite3"
    store = Storage(path)
    first = submission()
    assert store.enqueue(first)
    assert not store.enqueue(first)
    other_id = Submission("other", first.attempt_id, first.track, first.playback_start, first.requested_at)
    assert not store.enqueue(other_id)
    store.close()
    store = Storage(path)
    assert store.pending() == [first]
    store.close()


def test_queue_keeps_delivery_fields_without_full_metadata_and_migrates_old_records(tmp_path):
    path = tmp_path / "history.db"
    store = Storage(path)
    item = submission()
    item.track.details = {"source_path": "C:/Users/Private/music.mp3", "playerName": "Private", "score": 999}
    store.enqueue(item)
    saved = store.get(item.id)
    assert "details" not in saved["track"]
    assert store.pending()[0].track.title == item.track.title
    # Reproduce a record written by the previous release.
    store.db.execute("UPDATE submissions SET track=?", (json.dumps(item.track.to_dict()),))
    store.db.execute("PRAGMA user_version=0")
    store.db.commit()
    store.close()
    store = Storage(path)
    try:
        assert "details" not in store.get(item.id)["track"]
        pending = store.pending()[0]
        assert pending.id == item.id and pending.playback_start == item.playback_start
        assert pending.track.recording_id == "recording"
        assert pending.track.details == {}
        store.mark(item.id, "accepted", submitted_at=1700000030)
        assert store.export(tmp_path / "kept.txt") == 1
    finally:
        store.close()


def test_explicit_recovery_and_uncertain_retry(tmp_path):
    path = tmp_path / "state.sqlite3"
    store = Storage(path)
    item = submission()
    store.enqueue(item)
    store.mark(item.id, "sending")
    store.close()
    store = Storage(path)
    assert store.get(item.id)["status"] == "sending"
    assert store.recover_inflight() == 1
    assert store.pending() == []
    assert not store.retry(item.id)
    assert store.retry(item.id, include_uncertain=True)
    assert store.pending() == [item]
    store.close()


def test_success_history_only_successes_export_and_unicode(tmp_path):
    store = Storage(tmp_path / "state.sqlite3")
    for n in range(1, 4):
        store.enqueue(submission(n))
    store.mark("id-1", "accepted", submitted_at=1700000030.125)
    store.mark("id-2", "ignored")
    store.mark("id-3", "uncertain")
    assert store.last_success("recording") == 1700000030.125
    assert not store.retry("id-1", include_uncertain=True)
    export = tmp_path / "Unicode.txt"
    assert store.export(export) == 1
    assert store.export(export) == 0
    content = export.read_text(encoding="utf-8")
    fields = content.rstrip("\n").split("\t")
    assert len(fields) == 4
    assert fields[1:] == ["歌手 名", "曲 名 次", "アルバム 次"]
    assert datetime.fromisoformat(fields[0]).utcoffset() is not None
    assert len(content.splitlines()) == 1
    store.close()


def test_export_near_epoch_preserves_instant_when_local_date_is_1969(tmp_path, monkeypatch):
    import osu_scrobbler.storage as storage_module

    western_timezone = timezone(timedelta(hours=-5))

    class WindowsEpochDatetime(datetime):
        def astimezone(self, tz=None):
            if self.tzinfo is None:
                # Windows mktime rejects this naive local-time conversion.
                raise OSError(22, "Invalid argument")
            return super().astimezone(tz or western_timezone)

    monkeypatch.setattr(storage_module, "datetime", WindowsEpochDatetime)
    store = Storage(tmp_path / "state.sqlite3")
    try:
        store.enqueue(submission())
        store.mark("id-1", "accepted", submitted_at=300)
        target = tmp_path / "epoch.txt"
        assert store.export(target) == 1
        fields = target.read_text("utf-8").strip().split("\t")
        exported_time = datetime.fromisoformat(fields[0])
        assert exported_time.year == 1969
        assert exported_time.utcoffset() == timedelta(hours=-5)
        assert exported_time.timestamp() == 300
        assert store.export(target) == 0
        assert store.get("id-1")["status"] == "accepted"
        assert store.pending() == []
    finally:
        store.close()


def test_append_before_ledger_commit_recovers_without_duplicate(tmp_path):
    path = tmp_path / "state.sqlite3"
    export = tmp_path / "output.txt"
    store = Storage(path)
    item = submission()
    store.enqueue(item)
    store.mark(item.id, "accepted", submitted_at=1700000030.125)
    assert store.export(export) == 1
    # Simulates abrupt termination after fsync but before ledger commit.
    store.db.execute("DELETE FROM exports")
    store.db.commit()
    assert store.export(export) == 0
    assert len(export.read_text(encoding="utf-8").splitlines()) == 1
    store.close()


def test_distinct_identical_success_lines_are_not_merged(tmp_path):
    store = Storage(tmp_path / "state.sqlite3")
    export = tmp_path / "output.txt"
    for n in (1, 2):
        item = submission(n)
        store.enqueue(item)
        store.mark(item.id, "accepted", submitted_at=1700000030.125)
    assert store.export(export) == 2
    assert len(export.read_text(encoding="utf-8").splitlines()) == 2
    assert store.export(export) == 0
    store.close()


def test_export_failure_never_requeues_accepted_scrobble(tmp_path):
    store = Storage(tmp_path / "state.sqlite3")
    item = submission()
    store.enqueue(item)
    store.mark(item.id, "accepted", submitted_at=1700000030.125)
    destination = tmp_path / "directory"
    destination.mkdir()
    with pytest.raises(OSError):
        store.export(destination)
    assert store.get(item.id)["status"] == "accepted"
    assert store.pending() == []
    store.close()


def test_failed_and_retry_queue_separate_from_ignored(tmp_path):
    store = Storage(tmp_path / "state.sqlite3")
    for n in (1, 2, 3):
        store.enqueue(submission(n))
    store.mark("id-1", "retry")
    store.mark("id-2", "failed")
    store.mark("id-3", "ignored")
    assert [x.id for x in store.pending()] == ["id-1"]
    assert store.retry("id-2")
    assert not store.retry("id-3", include_uncertain=True)
    assert [x.id for x in store.pending()] == ["id-1", "id-2"]
    store.close()


def test_two_processes_export_without_duplicates(tmp_path):
    import subprocess
    import sys
    path = tmp_path / "state.sqlite3"
    destination = tmp_path / "output.txt"
    store = Storage(path)
    item = submission()
    store.enqueue(item)
    store.mark(item.id, "accepted", submitted_at=1700000030.125)
    script = "from osu_scrobbler.storage import Storage; import sys; s=Storage(sys.argv[1]); print(s.export(sys.argv[2])); s.close()"
    commands = [subprocess.Popen([sys.executable, "-c", script, str(path), str(destination)],
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) for _ in range(2)]
    results = [process.communicate(timeout=15) for process in commands]
    assert all(process.returncode == 0 for process in commands), results
    assert sorted(stdout.strip() for stdout, _ in results) == ["0", "1"]
    assert len(destination.read_text(encoding="utf-8").splitlines()) == 1
    store.close()


def test_runtime_state_persists_and_accepted_state_commits_atomically(tmp_path):
    path = tmp_path / "state.sqlite3"
    store = Storage(path)
    assert store.get_state("force_credits", []) == []
    store.set_state("settings", {"unicode": "曲", "enabled": True})
    store.enqueue(submission())
    credits = [{"aliases": ["recording", "歌手\0曲"], "after_serial": 1}]
    store.mark("id-1", "accepted", submitted_at=1700000030.125, state={"force_credits": credits})
    store.close()
    store = Storage(path)
    assert store.get("id-1")["status"] == "accepted"
    assert store.get_state("force_credits") == credits
    assert store.get_state("settings") == {"unicode": "曲", "enabled": True}
    # Invalid JSON state must roll back the corresponding acceptance update.
    store.enqueue(submission(2))
    with pytest.raises(TypeError):
        store.mark("id-2", "accepted", state={"invalid": object()})
    assert store.get("id-2")["status"] == "queued"
    store.close()
