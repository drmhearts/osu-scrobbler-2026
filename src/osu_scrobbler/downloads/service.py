"""Worker orchestration. A separate process keeps downloads out of scrobble timing."""

import asyncio
from datetime import datetime, timezone
import os
from pathlib import Path
import subprocess

from ..config import WatchLock
from .archive import archive_ids, publish
from .library import scan_all, discover, imported_stable_ids, imported_stable_file
from .limiter import Limiter
from .models import AuthRequired, Destination, DownloadError, Filters, RateLimited, TransientError
from .store import Store


def shared_root():
    # Independent of portable-runtime/--data-dir profile resolution: all profiles
    # for this OS user must share the same outer budget and worker lock.
    if os.name == "nt":
        base = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData/Local"))
    else:
        base = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local/share"))
    return base / "osu-scrobbler" / "beatmap-download-shared"


def import_archive(destination, archive):
    """Use the explicitly chosen client, never the default .osz association."""
    if not destination.executable:
        return False
    executable = Path(destination.executable)
    if not executable.is_file():
        raise DownloadError("Chosen client executable is unavailable; archive is downloaded, not imported.")
    subprocess.Popen([str(executable), str(Path(archive).resolve())], stdin=subprocess.DEVNULL,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    return True  # Accepted for handoff, NOT proof the client imported it.


class Interrupted(DownloadError):
    pass


class Engine:
    def __init__(self, store, limiter, source, *, output=lambda _: None, importer=import_archive):
        self.store, self.limiter, self.source = store, limiter, source
        self.output, self.importer = output, importer

    def running(self, job_id):
        return self.store.job(job_id)["control"] == "run"

    async def operation(self, job_id, awaitable):
        """Pause/cancel aborts an in-flight transfer without accepting its archive."""
        task = asyncio.ensure_future(awaitable)
        try:
            while not task.done():
                if not self.running(job_id):
                    raise Interrupted()
                await asyncio.wait({task}, timeout=0.25)
            return await task
        finally:
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

    async def reserve(self, job_id, kind, set_id=None):
        account = self.store.job(job_id)["account"]
        while True:
            if not self.running(job_id):
                raise Interrupted()
            until = self.limiter.reserve(account, set_id, kind)
            if until is None:
                self.store.update(job_id, state="running", message="", progress=0)
                return
            label = datetime.fromtimestamp(until, timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
            self.store.update(job_id, state="waiting", message="Conservative pacing/cooldown until " + label)
            await asyncio.sleep(min(0.5, max(0.01, until - self.limiter.clock())))

    async def run(self, job_id):
        store = self.store
        job = store.job(job_id)
        filters = Filters(**job["filters"]).validate()
        destination = Destination(**job["destination"]).validate()
        staging = store.root / "staging"
        staging.mkdir(exist_ok=True)
        inbox = store.root / "lazer-inbox"
        libraries = [destination, *[Destination(**value) for value in store.preference("libraries", [])], *discover()]
        def check():
            if not self.running(job_id):
                raise Interrupted()
        try:
            # This is an isolated worker process; keep SQLite on its owning thread.
            store.update(job_id, state="scanning", message="Checking installed libraries and pending archives...")
            scan_all(libraries, cache=store, pending=(inbox,))
            store.update(job_id, state="running", message="Library checked", progress=0)
            if not self.running(job_id):
                raise Interrupted()
            await self.reserve(job_id, "connect")
            account = await self.operation(job_id, self.source.connect())
            prior = store.job(job_id)["account"]
            if prior and prior != str(account):
                raise AuthRequired("Use the job's original osu! account. Queue and shared cooldowns are retained.")
            store.update(job_id, account=str(account))
            pages = 0
            while self.running(job_id):
                job = store.job(job_id)
                if job["count"] >= filters.limit:
                    store.update(job_id, state="complete", message="Maximum number of beatmapsets reached.")
                    return
                item = store.next(job_id)
                if item is None:
                    if job["exhausted"]:
                        store.update(job_id, state="complete", message="Matching listing exhausted.")
                        return
                    if pages >= 200:
                        store.update(job_id, state="paused", control="pause", message="200 search pages checked. Resume to continue the saved cursor.")
                        return
                    await self.reserve(job_id, "search")
                    items, cursor = await self.operation(job_id, self.source.search(filters, job["cursor"]))
                    if cursor and cursor == job["cursor"]:
                        raise DownloadError("Listing cursor did not advance; queue preserved.")
                    candidates = []
                    for metadata in items:
                        if not isinstance(metadata, dict):
                            raise DownloadError("Invalid listing response; queue retained.")
                        if filters.genre != "any" and "genre" not in metadata and "genre_id" not in metadata:
                            await self.reserve(job_id, "metadata")
                            metadata = await self.operation(job_id, self.source.details(metadata["id"]))
                        if filters.matches(metadata):
                            candidates.append(metadata)
                    store.page(job_id, candidates, cursor)
                    pages += 1
                    continue
                set_id = item["set_id"]
                # Re-scan metadata stamps to catch imports and renamed archives during this job.
                existing = scan_all(libraries, cache=store, pending=(inbox,))
                target = (destination.songs if destination.client == "stable" else inbox) / f"{set_id}.osz"
                if item["state"] in {"downloading", "validated"} and target.is_file():
                    archive_ids(target, expected=set_id, full=True, check=check)
                    store.complete(job_id, set_id, target)
                    self.limiter.completed(set_id, target)
                    continue
                if item["state"] == "validated" and destination.client == "stable" and set_id in existing:
                    imported = imported_stable_file(destination, set_id)
                    if imported:
                        store.complete(job_id, set_id, imported, state="imported")
                        self.limiter.completed(set_id, imported)
                        continue
                if set_id in existing or set_id in store.known() or set_id in self.limiter.known():
                    store.transition(job_id, set_id, "skipped")
                    continue
                partial = staging / f"job-{job_id}-set-{set_id}.part"
                # Recover a finished transfer after a process crash, without another request.
                valid = False
                if partial.exists():
                    try:
                        archive_ids(partial, expected=set_id, full=True, check=check)
                        valid = True
                    except Interrupted:
                        raise
                    except (DownloadError, OSError):
                        partial.unlink()  # Only this job's own partial in its staging directory.
                if not valid:
                    await self.reserve(job_id, "download", set_id)
                    store.transition(job_id, set_id, "downloading", archive=partial)
                    store.update(job_id, message=f"Downloading beatmapset {set_id} (full media)")
                    await self.operation(job_id, self.source.download(set_id, partial,
                        lambda count: store.update(job_id, progress=count)))
                    archive_ids(partial, expected=set_id, full=True, check=check)
                if not self.running(job_id):
                    raise Interrupted()
                store.transition(job_id, set_id, "validated", archive=partial)
                placed = publish(partial, target, set_id, check=check)
                placement_state = "downloaded" if destination.client == "lazer" else "queued_for_import"
                if destination.client == "stable" and not placed.exists():
                    placed = imported_stable_file(destination, set_id)
                    if placed is None:
                        raise DownloadError("Client consumed the archive but import could not yet be confirmed. Resume later.")
                    placement_state = "imported"
                # Local receipt precedes cross-profile receipt; either prevents repeat requests.
                store.complete(job_id, set_id, placed, state=placement_state)
                self.limiter.completed(set_id, placed)
                partial.unlink(missing_ok=True)
                store.update(job_id, failures=0, message=f"Beatmapset {set_id} placed safely.")
                if destination.client == "lazer" and destination.executable:
                    try:
                        if self.importer(destination, placed):
                            store.transition(job_id, set_id, "queued_for_import", placed=placed)
                            with store.db:
                                store.db.execute("UPDATE history SET state='queued_for_import' WHERE set_id=?", (set_id,))
                    except (OSError, DownloadError):
                        store.update(job_id, message=f"Downloaded {set_id}; client handoff failed. Use maps → import saved archives.")
                if destination.client == "stable":
                    store.confirm_stable(imported_stable_ids(destination))
                self.output(f"Beatmapset {set_id}: " + ("queued for import" if destination.client == "stable" or destination.executable else "downloaded"))
            raise Interrupted()
        except Interrupted:
            control = store.job(job_id)["control"]
            store.update(job_id, state="cancelled" if control == "cancel" else "paused",
                         message="Progress and pending queue saved. Resume restarts only an incomplete transfer.")
        except asyncio.CancelledError:
            store.control(job_id, "pause")
            raise
        except RateLimited as error:
            self.limiter.cooldown(error.seconds)
            until = datetime.fromtimestamp(self.limiter.ready_at(store.job(job_id)["account"]), timezone.utc).isoformat()
            store.update(job_id, state="cooldown", message=str(error) + " Cooldown until " + until)
        except TransientError as error:
            failures = store.job(job_id)["failures"] + 1
            self.limiter.cooldown(max(error.seconds, min(3600, 60 * 2 ** min(failures - 1, 6))))
            until = datetime.fromtimestamp(self.limiter.ready_at(store.job(job_id)["account"]), timezone.utc).isoformat()
            store.update(job_id, failures=failures, state="cooldown", message=str(error) + " Cooldown until " + until)
        except AuthRequired as error:
            store.update(job_id, state="auth_required", message=str(error))
        except (DownloadError, OSError):
            store.update(job_id, state="error", message="Download or placement failed. Library unchanged; valid staging and queue retained. Check destination, sign-in and disk space.")


async def worker(directory, job_id, *, source_factory=None, shared=None):
    from .browser import BrowserSource
    root = Path(directory) / "beatmap-downloads"
    shared = Path(shared) if shared else shared_root()
    shared.mkdir(parents=True, exist_ok=True)
    lock = WatchLock(shared, "worker.lock")
    try:
        lock.__enter__()
    except RuntimeError:
        return False  # Never pause the already-running job from a competing worker.
    store, limiter = Store(root), Limiter(shared)
    try:
        # Gate browser creation too: a persistent browser may restore an old tab.
        # No browser/session requests should begin during a shared cooldown.
        while limiter.ready_at(store.job(job_id)["account"], "connect") > limiter.clock():
            control = store.job(job_id)["control"]
            if control != "run":
                store.update(job_id, state="cancelled" if control == "cancel" else "paused")
                return True
            until = datetime.fromtimestamp(limiter.ready_at(store.job(job_id)["account"], "connect"), timezone.utc).isoformat()
            store.update(job_id, state="waiting", message="Shared cooldown/pacing until " + until)
            await asyncio.sleep(0.25)
        if store.job(job_id)["control"] != "run":
            return True
        async with (source_factory or BrowserSource)(root) as source:
            await Engine(store, limiter, source).run(job_id)
    except AuthRequired as error:
        store.update(job_id, state="auth_required", message=str(error))
    except Exception:
        store.update(job_id, state="error", message="Cannot start the download browser/worker. Install Edge or Chrome, close this tool's old browser, then resume. Progress retained.")
    finally:
        limiter.close()
        store.close()
        lock.__exit__(None, None, None)
    return True


def worker_active(shared=None):
    root = Path(shared) if shared else shared_root()
    if not root.is_dir():
        return False
    lock = WatchLock(root, "worker.lock")
    try:
        lock.__enter__()
    except RuntimeError:
        return True
    lock.__exit__(None, None, None)
    return False


def launch_worker(directory, job_id):
    from ..tray import python_executable
    subprocess.Popen([python_executable(), "-m", "osu_scrobbler", "--data-dir", str(Path(directory).resolve()),
                      "maps", "worker", "--job", str(job_id)], stdin=subprocess.DEVNULL,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))


def pause_active(directory):
    """Tray Exit/update pauses transfers; hiding the console does not."""
    import sqlite3
    root = Path(directory) / "beatmap-downloads"
    if not (root / "downloads.sqlite3").exists():
        return
    try:
        store = Store(root)
        try:
            with store.db:
                store.db.execute("UPDATE jobs SET control='pause',state='paused',message='App stopped; resume the saved maps job.' WHERE state IN ('ready','scanning','running','waiting')")
        finally:
            store.close()
    except sqlite3.Error:
        pass  # An unreadable checkpoint must not block the monitor's Exit button.
