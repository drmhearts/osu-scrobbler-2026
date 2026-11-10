"""Transactional checkpoint, queue ownership and immutable placement receipts."""

import json
from pathlib import Path
import sqlite3
import time

from .models import DownloadError


class Store:
    def __init__(self, directory):
        self.root = Path(directory)
        self.root.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.root / "downloads.sqlite3", timeout=10)
        self.db.row_factory = sqlite3.Row
        self.db.executescript("""
            PRAGMA journal_mode=WAL;
            PRAGMA synchronous=FULL;
            CREATE TABLE IF NOT EXISTS jobs (
                id INTEGER PRIMARY KEY, filters TEXT NOT NULL, destination TEXT NOT NULL,
                state TEXT NOT NULL DEFAULT 'ready', control TEXT NOT NULL DEFAULT 'run',
                cursor TEXT, exhausted INTEGER NOT NULL DEFAULT 0, count INTEGER NOT NULL DEFAULT 0,
                last_id INTEGER, message TEXT NOT NULL DEFAULT '', progress INTEGER NOT NULL DEFAULT 0,
                account TEXT, created REAL NOT NULL, failures INTEGER NOT NULL DEFAULT 0);
            CREATE TABLE IF NOT EXISTS queue (
                job INTEGER NOT NULL, set_id INTEGER NOT NULL, metadata TEXT NOT NULL,
                state TEXT NOT NULL DEFAULT 'pending', archive TEXT, placed TEXT,
                PRIMARY KEY(job,set_id));
            CREATE UNIQUE INDEX IF NOT EXISTS pending_owner ON queue(set_id)
                WHERE state IN ('pending','downloading','validated');
            CREATE TABLE IF NOT EXISTS history (
                set_id INTEGER PRIMARY KEY, job INTEGER NOT NULL, path TEXT NOT NULL,
                state TEXT NOT NULL, completed REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS file_cache (
                path TEXT PRIMARY KEY, size INTEGER, modified INTEGER, ids TEXT);
            CREATE TABLE IF NOT EXISTS preferences (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        """)
        if "failures" not in {row[1] for row in self.db.execute("PRAGMA table_info(jobs)")}:
            with self.db:
                self.db.execute("ALTER TABLE jobs ADD COLUMN failures INTEGER NOT NULL DEFAULT 0")

    def close(self):
        self.db.close()

    def preference(self, key, fallback=None):
        row = self.db.execute("SELECT value FROM preferences WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else fallback

    def remember(self, key, value):
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO preferences VALUES (?,?)", (key, json.dumps(value)))

    def create(self, filters, destination):
        filters.validate()
        destination.validate()
        with self.db:
            result = self.db.execute("INSERT INTO jobs(filters,destination,created) VALUES (?,?,?)",
                                     (json.dumps(filters.to_dict()), json.dumps(destination.to_dict()), time.time()))
            job_id = result.lastrowid
        self.remember("filters", filters.to_dict())
        self.remember("destination", destination.to_dict())
        return job_id

    def job(self, job_id=None):
        row = self.db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone() if job_id else self.db.execute(
            "SELECT * FROM jobs ORDER BY id DESC LIMIT 1").fetchone()
        if row is None:
            raise DownloadError("No download job yet. Start a batch from the maps menu.")
        result = dict(row)
        result["filters"] = json.loads(result["filters"])
        result["destination"] = json.loads(result["destination"])
        result["pending"] = self.db.execute("SELECT COUNT(*) FROM queue WHERE job=? AND state IN ('pending','downloading','validated')",
                                            (result["id"],)).fetchone()[0]
        return result

    def update(self, job_id, **values):
        allowed = {"state", "control", "cursor", "exhausted", "count", "last_id", "message", "progress", "account", "failures"}
        if not values or set(values) - allowed:
            raise ValueError("Invalid checkpoint fields.")
        with self.db:
            self.db.execute("UPDATE jobs SET " + ",".join(k + "=?" for k in values) + " WHERE id=?",
                            (*values.values(), job_id))

    def control(self, job_id, action):
        if action not in {"pause", "cancel", "run"}:
            raise ValueError("Invalid download control.")
        self.update(job_id, control=action)
        if action != "run":
            self.update(job_id, state="paused" if action == "pause" else "cancelled",
                        message="Current transfer stops safely; queue retained for resume.")

    def page(self, job_id, items, cursor):
        with self.db:
            for item in items:
                set_id = item.get("id")
                if type(set_id) is not int or set_id <= 0:
                    raise DownloadError("Invalid beatmapset ID in listing; checkpoint preserved.")
                if self.db.execute("SELECT 1 FROM history WHERE set_id=?", (set_id,)).fetchone():
                    continue
                self.db.execute("INSERT OR IGNORE INTO queue(job,set_id,metadata) VALUES (?,?,?)",
                                (job_id, set_id, json.dumps({key: item[key] for key in ("id", "artist", "title") if key in item}, ensure_ascii=False)))
            self.db.execute("UPDATE jobs SET cursor=?,exhausted=? WHERE id=?", (cursor, not bool(cursor), job_id))

    def next(self, job_id):
        row = self.db.execute("SELECT * FROM queue WHERE job=? AND state IN ('pending','downloading','validated') ORDER BY rowid LIMIT 1",
                              (job_id,)).fetchone()
        if not row:
            return None
        item = dict(row)
        item["metadata"] = json.loads(item["metadata"])
        return item

    def transition(self, job_id, set_id, state, *, archive=None, placed=None):
        with self.db:
            self.db.execute("UPDATE queue SET state=?,archive=COALESCE(?,archive),placed=COALESCE(?,placed) WHERE job=? AND set_id=?",
                            (state, str(archive) if archive else None, str(placed) if placed else None, job_id, set_id))

    def complete(self, job_id, set_id, path, state="queued_for_import"):
        # Called only after validated exclusive placement (or verified crash recovery).
        if not Path(path).is_file():
            raise DownloadError("Placement was not completed; download history unchanged.")
        with self.db:
            inserted = self.db.execute("INSERT OR IGNORE INTO history VALUES (?,?,?,?,?)",
                                      (set_id, job_id, str(path), state, time.time())).rowcount
            self.db.execute("UPDATE queue SET state=?,placed=? WHERE job=? AND set_id=?", (state, str(path), job_id, set_id))
            if inserted:
                self.db.execute("UPDATE jobs SET count=count+1,last_id=?,progress=0 WHERE id=?", (set_id, job_id))
                job = self.db.execute("SELECT count,filters FROM jobs WHERE id=?", (job_id,)).fetchone()
                if job[0] >= json.loads(job[1])["limit"]:
                    # Retain unused candidates, but release ownership when this
                    # finite batch is full so the next batch can download them.
                    self.db.execute("UPDATE queue SET state='deferred' WHERE job=? AND state='pending'", (job_id,))
                    self.db.execute("UPDATE jobs SET state='complete' WHERE id=?", (job_id,))

    def known(self):
        return {row[0] for row in self.db.execute("SELECT set_id FROM history")}

    def receipts(self):
        return [dict(row) for row in self.db.execute("SELECT * FROM history ORDER BY completed DESC")]

    def confirm_stable(self, ids):
        with self.db:
            for set_id in ids:
                self.db.execute("UPDATE history SET state='imported' WHERE set_id=?", (set_id,))
                self.db.execute("UPDATE queue SET state='imported' WHERE set_id=? AND state='queued_for_import'", (set_id,))

    def confirm_lazer(self, set_id):
        # Explicit user confirmation, not an inference from an import process's exit code.
        with self.db:
            if not self.db.execute("SELECT 1 FROM history WHERE set_id=?", (set_id,)).fetchone():
                raise DownloadError("No downloaded archive with that ID.")
            self.db.execute("UPDATE history SET state='imported_confirmed_by_user' WHERE set_id=?", (set_id,))
            self.db.execute("UPDATE queue SET state='imported_confirmed_by_user' WHERE set_id=?", (set_id,))

    def get(self, path, stamp):
        row = self.db.execute("SELECT ids FROM file_cache WHERE path=? AND size=? AND modified=?", (path, *stamp)).fetchone()
        return set(json.loads(row[0])) if row else None

    def put(self, path, stamp, ids):
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO file_cache VALUES (?,?,?,?)", (path, *stamp, json.dumps(sorted(ids))))
