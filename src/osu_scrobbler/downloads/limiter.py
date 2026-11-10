"""Persistent, shared budgets. These are conservative app policy, not server guarantees."""

from datetime import timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
import sqlite3
import time

from .models import DownloadError


def retry_seconds(value, now=None, default=3600):
    now = time.time() if now is None else now
    try:
        seconds = float(value)
        if seconds >= 0 and seconds != float("inf"):
            return max(1, seconds)
    except (ValueError, TypeError):
        pass
    try:
        when = parsedate_to_datetime(value)
        if when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
        return max(1, when.timestamp() - now)
    except (ValueError, TypeError, OverflowError):
        return default


class Limiter:
    spacing = 60
    hourly = 30
    request_spacing = 2

    def __init__(self, root, clock=time.time):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.clock = clock
        self.db = sqlite3.connect(self.root / "request-history.sqlite3", timeout=10)
        self.db.executescript("""
            PRAGMA journal_mode=WAL;
            PRAGMA synchronous=FULL;
            CREATE TABLE IF NOT EXISTS requests (account TEXT, set_id INTEGER, kind TEXT, at REAL);
            CREATE TABLE IF NOT EXISTS cooldown (id INTEGER PRIMARY KEY CHECK(id=1), until_at REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS receipts (set_id INTEGER PRIMARY KEY, path TEXT NOT NULL);
        """)

    def close(self):
        self.db.close()

    def ready_at(self, account, kind="download"):
        now = self.clock()
        blocked = self.db.execute("SELECT until_at FROM cooldown WHERE id=1").fetchone()
        result = max(now, blocked[0] if blocked else now)
        last_request = self.db.execute("SELECT MAX(at) FROM requests").fetchone()[0]
        if last_request is not None:
            result = max(result, last_request + self.request_spacing)
        if kind == "download":
            # All accounts share the outer budget too: switching cannot reset it.
            for predicate, params in (("", ()), (" AND account=?", (str(account),))):
                rows = [row[0] for row in self.db.execute(
                    "SELECT at FROM requests WHERE kind='download' AND at>?" + predicate + " ORDER BY at DESC",
                    (now - 3600, *params))]
                if rows:
                    result = max(result, rows[0] + self.spacing)
                if len(rows) >= self.hourly:
                    result = max(result, rows[self.hourly - 1] + 3600)
        return result

    def reserve(self, account, set_id=None, kind="download"):
        self.db.execute("BEGIN IMMEDIATE")
        try:
            ready = self.ready_at(account, kind)
            if ready > self.clock() + 0.001:
                self.db.rollback()
                return ready
            self.db.execute("INSERT INTO requests VALUES (?,?,?,?)", (str(account), set_id, kind, self.clock()))
            self.db.execute("DELETE FROM requests WHERE at<?", (self.clock() - 7 * 86400,))
            self.db.commit()
            return None
        except BaseException:
            self.db.rollback()
            raise

    def cooldown(self, seconds):
        until = self.clock() + max(1, seconds)
        with self.db:
            self.db.execute("INSERT INTO cooldown VALUES (1,?) ON CONFLICT(id) DO UPDATE SET until_at=MAX(until_at,excluded.until_at)", (until,))

    def known(self):
        return {row[0] for row in self.db.execute("SELECT set_id FROM receipts")}

    def completed(self, set_id, path):
        if not Path(path).is_file():
            raise DownloadError("No placed archive; shared receipt unchanged.")
        with self.db:
            self.db.execute("INSERT OR IGNORE INTO receipts VALUES (?,?)", (set_id, str(path)))
