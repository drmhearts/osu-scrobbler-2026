"""Durable local delivery queue, history and idempotent Unicode exports."""
from __future__ import annotations

from collections import Counter
from contextlib import contextmanager
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sqlite3
import time
from typing import Iterator

from .models import Submission, Track

STATUSES = {"queued", "retry", "sending", "accepted", "ignored", "uncertain", "failed"}


@contextmanager
def _export_lock(destination: Path) -> Iterator[None]:
    """Serialize append+ledger operations across all exporting processes."""
    lock_path = destination.with_name(destination.name + ".lock")
    with lock_path.open("a+b") as stream:
        if os.name == "nt":
            import msvcrt
            stream.seek(0, os.SEEK_END)
            if stream.tell() == 0:
                stream.write(b"\0")
                stream.flush()
            stream.seek(0)
            msvcrt.locking(stream.fileno(), msvcrt.LK_LOCK, 1)
            try:
                yield
            finally:
                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def _field(value: str) -> str:
    # Normalize all line breaks, including Unicode paragraph/line separators.
    return " ".join(str(value).replace("\t", " ").splitlines())


def _line(row: dict) -> str:
    # An aware UTC starting point avoids Windows mktime failures when a small
    # positive Unix timestamp falls in 1969 in the user's local timezone.
    timestamp = datetime.fromtimestamp(row["submitted_at"], timezone.utc).astimezone().isoformat(timespec="microseconds")
    return "\t".join((timestamp, _field(row["track"]["artist"]),
                      _field(row["track"]["title"]), _field(row["track"]["album"])))


class Storage:
    def __init__(self, path: Path | str, *, recover_inflight: bool = False) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.path, timeout=20)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA busy_timeout=20000")
        self.db.execute("PRAGMA secure_delete=ON")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS submissions (
                id TEXT PRIMARY KEY,
                attempt_id TEXT NOT NULL UNIQUE,
                identity TEXT NOT NULL,
                track TEXT NOT NULL,
                playback_start INTEGER NOT NULL,
                requested_at REAL NOT NULL,
                manual INTEGER NOT NULL,
                status TEXT NOT NULL DEFAULT 'queued',
                message TEXT NOT NULL DEFAULT '',
                submitted_at REAL
            );
            CREATE INDEX IF NOT EXISTS successful_identity
                ON submissions(identity, submitted_at) WHERE status = 'accepted';
            CREATE TABLE IF NOT EXISTS exports (
                destination TEXT NOT NULL,
                submission_id TEXT NOT NULL REFERENCES submissions(id),
                PRIMARY KEY(destination, submission_id)
            );
            CREATE TABLE IF NOT EXISTS runtime_state (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
        """)
        if self.db.execute("PRAGMA user_version").fetchone()[0] < 1:
            # Keep queue/history intact while removing full lookup-response copies
            # saved by earlier releases. This does not alter submitted fields.
            with self.db:
                for row in self.db.execute("SELECT id,track FROM submissions"):
                    track = Track.from_dict(json.loads(row["track"]))
                    self.db.execute("UPDATE submissions SET track=? WHERE id=?",
                                    (json.dumps(track.to_dict(include_details=False), ensure_ascii=False), row["id"]))
                self.db.execute("PRAGMA user_version=1")
        if recover_inflight:
            self.recover_inflight()

    def close(self) -> None:
        self.db.close()

    def recover_inflight(self) -> int:
        """Call after obtaining the exclusive watcher lock, before delivery.

        Another watcher must not recover requests that are currently in flight.
        A crash during a write cannot establish whether Last.fm accepted it.
        """
        with self.db:
            cursor = self.db.execute("UPDATE submissions SET status='uncertain', message=? WHERE status='sending'",
                                     ("Watcher stopped during delivery; review before explicitly retrying.",))
            return cursor.rowcount

    def enqueue(self, submission: Submission, *, state: dict | None = None) -> bool:
        with self.db:
            cursor = self.db.execute("""INSERT OR IGNORE INTO submissions
                (id,attempt_id,identity,track,playback_start,requested_at,manual,status)
                VALUES (?,?,?,?,?,?,?,'queued')""", (
                submission.id, submission.attempt_id, submission.track.identity,
                json.dumps(submission.track.to_dict(include_details=False), ensure_ascii=False),
                submission.playback_start, submission.requested_at, int(submission.manual)))
            inserted = cursor.rowcount == 1
            if inserted:
                for key, value in (state or {}).items():
                    self._set_state(key, value)
            return inserted

    def pending(self) -> list[Submission]:
        rows = self.db.execute("SELECT * FROM submissions WHERE status IN ('queued','retry') ORDER BY requested_at,id").fetchall()
        return [Submission(row["id"], row["attempt_id"], Track.from_dict(json.loads(row["track"])),
                           row["playback_start"], row["requested_at"], bool(row["manual"])) for row in rows]

    def mark(self, submission_id: str, status: str, message: str = "", submitted_at: float | None = None,
             *, state: dict | None = None) -> None:
        if status not in STATUSES:
            raise ValueError("Unknown delivery status.")
        if status == "accepted" and submitted_at is None:
            submitted_at = time.time()
        with self.db:
            cursor = self.db.execute("""UPDATE submissions SET status=?,message=?,
                submitted_at=CASE WHEN ?='accepted' THEN COALESCE(submitted_at,?) ELSE submitted_at END
                WHERE id=?""", (status, message, status, submitted_at, submission_id))
            if cursor.rowcount != 1:
                raise KeyError(submission_id)
            for key, value in (state or {}).items():
                self._set_state(key, value)

    def _set_state(self, key: str, value) -> None:
        self.db.execute("INSERT INTO runtime_state(key,value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                        (key, json.dumps(value, ensure_ascii=False)))

    def get_state(self, key: str, default=None):
        row = self.db.execute("SELECT value FROM runtime_state WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row is not None else default

    def set_state(self, key: str, value) -> None:
        with self.db:
            self._set_state(key, value)

    @staticmethod
    def _summary(row: sqlite3.Row) -> dict:
        result = dict(row)
        result["track"] = json.loads(result["track"])
        result["manual"] = bool(result["manual"])
        return result

    def history(self, limit: int | None = None) -> list[dict]:
        query = "SELECT * FROM submissions ORDER BY requested_at DESC,id DESC"
        args: tuple = ()
        if limit is not None:
            query += " LIMIT ?"
            args = (max(0, limit),)
        return [self._summary(row) for row in self.db.execute(query, args).fetchall()]

    def get(self, submission_id: str) -> dict | None:
        row = self.db.execute("SELECT * FROM submissions WHERE id=?", (submission_id,)).fetchone()
        return self._summary(row) if row is not None else None

    def last_success(self, identity: str) -> float | None:
        return self.db.execute("SELECT MAX(submitted_at) FROM submissions WHERE status='accepted' AND identity=?",
                               (identity,)).fetchone()[0]

    def retry(self, submission_id: str, include_uncertain: bool = False) -> bool:
        statuses = ("retry", "failed", "uncertain") if include_uncertain else ("retry", "failed")
        placeholders = ",".join("?" for _ in statuses)
        with self.db:
            cursor = self.db.execute(f"UPDATE submissions SET status='queued',message='' WHERE id=? AND status IN ({placeholders})",
                                     (submission_id, *statuses))
            return cursor.rowcount == 1

    def export(self, destination: Path | str) -> int:
        """Append accepted submissions exactly once per destination.

        The separate ledger is committed only after fsync. On interruption
        between append and ledger commit, existing full lines recover the
        ledger without duplicating rows. Failed exports never touch delivery.
        """
        destination = Path(destination).expanduser().resolve()
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination_key = str(destination)
        count = 0
        with _export_lock(destination):
            with self.db:
                rows = [self._summary(row) for row in self.db.execute(
                    "SELECT * FROM submissions WHERE status='accepted' AND submitted_at IS NOT NULL ORDER BY submitted_at,id").fetchall()]
                # Count instead of set: distinct legitimate events can have identical metadata/times.
                if destination.exists():
                    existing = Counter(destination.read_text(encoding="utf-8").splitlines())
                else:
                    existing = Counter()
                known = {row[0] for row in self.db.execute(
                    "SELECT submission_id FROM exports WHERE destination=?", (destination_key,)).fetchall()}
                pending: list[tuple[dict, str]] = []
                for row in rows:
                    line = _line(row)
                    if row["id"] in known:
                        # Ledger preserves identity; consume its existing line if present.
                        if existing[line] > 0:
                            existing[line] -= 1
                        continue
                    if existing[line] > 0:
                        existing[line] -= 1
                        self.db.execute("INSERT OR IGNORE INTO exports VALUES (?,?)", (destination_key, row["id"]))
                    else:
                        pending.append((row, line))
                if pending:
                    # Reject an incomplete last line rather than concatenate two events.
                    if destination.exists() and destination.stat().st_size:
                        with destination.open("rb") as binary:
                            binary.seek(-1, os.SEEK_END)
                            ends_with_newline = binary.read(1) == b"\n"
                        if not ends_with_newline:
                            raise OSError("Export destination has an incomplete final line; repair it before exporting.")
                    with destination.open("a", encoding="utf-8", newline="") as stream:
                        for row, line in pending:
                            stream.write(line + "\n")
                        stream.flush()
                        os.fsync(stream.fileno())
                    for row, _ in pending:
                        self.db.execute("INSERT OR IGNORE INTO exports VALUES (?,?)", (destination_key, row["id"]))
                    count = len(pending)
        return count
