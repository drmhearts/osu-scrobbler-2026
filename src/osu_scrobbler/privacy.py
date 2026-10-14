"""Local data explanation and user-requested removal of disposable data."""

from contextlib import closing, ExitStack
from pathlib import Path
import sqlite3

from .config import WatchLock


def describe(directory):
    return {"message": (
        "No analytics, advertising, installation tracking or crash uploads.\n"
        "Online: MusicBrainz searches, Last.fm scrobbles, and GitHub/package-index downloads for enabled updates.\n"
        f"Local: settings, music cache/corrections and scrobble queue/history in {directory}.\n"
        "Credentials: OS vault. Terminal activity: memory only, up to 5 minutes.\n"
        "privacy --clear-cache removes disposable cache and older activity logs; keeps history/corrections."
    )}


def clear_cache(directory):
    directory = Path(directory)
    with ExitStack() as stack:
        try:
            stack.enter_context(WatchLock(directory, "tray.lock"))
            stack.enter_context(WatchLock(directory))
        except RuntimeError as error:
            raise RuntimeError("Exit the tray monitor and quit manual watch before clearing the cache.") from error
        path = directory / "musicbrainz.db"
        if path.exists():
            with closing(sqlite3.connect(path)) as db, db:
                db.execute("PRAGMA secure_delete=ON")
                db.execute("DELETE FROM cache")
        for name in ("activity.log", "activity.log.1", "activity.log.2"):
            (directory / name).unlink(missing_ok=True)
    return {"message": "Cleared music lookup cache and older activity logs. History, queued scrobbles and corrections kept."}
