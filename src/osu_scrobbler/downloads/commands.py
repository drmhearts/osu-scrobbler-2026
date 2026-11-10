"""CLI/menu facade over one checkpoint and independent background worker."""

import asyncio
from dataclasses import replace
from dataclasses import fields
from datetime import datetime, timezone
from pathlib import Path

from ..config import WatchLock
from .library import discover, scan_all, imported_stable_ids
from .limiter import Limiter
from .models import Destination, DownloadError, Filters, GENRES, MODES, STATUSES
from .service import import_archive, launch_worker, shared_root, worker, worker_active
from .store import Store

ACTIONS = ("menu", "start", "resume", "pause", "cancel", "status", "scan", "login",
           "configure", "import", "confirm", "worker")


def add_parser(commands):
    parser = commands.add_parser("maps", help="Bulk beatmapsets: configure, sign in, start, pause, resume or cancel")
    parser.add_argument("maps_action", nargs="?", default="menu", choices=ACTIONS)
    parser.add_argument("--job", type=int, help="Job ID; default is most recent")
    parser.add_argument("--client", choices=["stable", "lazer"])
    parser.add_argument("--root", type=Path, help="Stable installation/Songs, or lazer storage directory")
    parser.add_argument("--exe", type=Path, help="Chosen client's .exe for archive import")
    parser.add_argument("--mode", choices=list(MODES), help="osu=standard; catch=fruits")
    parser.add_argument("--genre", choices=list(GENRES))
    parser.add_argument("--category", choices=sorted(STATUSES))
    parser.add_argument("--ranked-from", help="Inclusive YYYY-MM-DD UTC date")
    parser.add_argument("--ranked-to", help="Inclusive YYYY-MM-DD UTC date")
    parser.add_argument("--limit", type=int, help="Successful beatmapsets, default 25; remembered for subsequent jobs")
    parser.add_argument("--min-length", type=float, help="Full difficulty duration in seconds")
    parser.add_argument("--max-length", type=float)
    parser.add_argument("--mapper", help="Exact host mapper username or numeric user ID")
    parser.add_argument("--foreground", action="store_true", help="Run worker in this terminal instead of background")
    parser.add_argument("--set-id", type=int, help="Explicitly confirm a downloaded lazer set was imported")
    return parser


def configure(store, client=None, root=None, executable=None):
    saved = store.preference("destination")
    if root or client or executable:
        value = dict(saved or {})
        if (client and client != value.get("client")) or (root and str(Path(root).expanduser().resolve()) != value.get("root")):
            value["executable"] = ""  # Never carry a different client's executable into this destination.
        if client:
            value["client"] = client
        if root:
            value["root"] = str(Path(root).expanduser().resolve())
        if executable:
            value["executable"] = str(Path(executable).expanduser().resolve())
        if "client" not in value or "root" not in value:
            raise DownloadError("Choose both --client and --root, or use maps → destination.")
        destination = Destination(**value).validate()
    elif saved:
        destination = Destination(**saved).validate()
    else:
        choices = discover()
        if len(choices) != 1:
            raise DownloadError("Choose a destination using maps → destination, or maps configure --client ... --root ...")
        destination = choices[0].validate()
    libraries = store.preference("libraries", [])
    key = (destination.client, destination.root)
    libraries = [value for value in libraries if (value["client"], value["root"]) != key]
    libraries.append(destination.to_dict())
    store.remember("libraries", libraries)
    store.remember("destination", destination.to_dict())
    return destination


def describe(store, job_id=None):
    try:
        job = store.job(job_id)
    except DownloadError:
        return {"message": "Maps: no batch yet. Choose a destination, sign in once, then start recently ranked sets."}
    root = job["destination"]
    text = (f"Maps #{job['id']}: {job['state']} | {job['count']}/{job['filters']['limit']} sets | "
            f"{job['pending']} pending | {root['client']}\n"
            f"Last saved: {job['last_id'] or 'none'}. {job['message']}")
    return {"message": text, "download_job": job, "receipts": store.receipts()}


async def execute(args):
    action = args.maps_action
    if action == "menu":
        await menu(args.data_dir)
        return None
    if action == "worker":
        if not args.job:
            raise DownloadError("Worker requires --job.")
        await worker(args.data_dir, args.job)
        return None
    import sqlite3
    try:
        store = Store(Path(args.data_dir) / "beatmap-downloads")
    except sqlite3.Error:
        raise DownloadError("Download checkpoint cannot be read. Preserve beatmap-downloads; do not reset history to fix it.") from None
    try:
        if action == "configure":
            destination = configure(store, args.client, args.root, args.exe)
            return {"message": f"Destination remembered: {destination.client} — {destination.root}", "destination": destination.to_dict()}
        if action == "status":
            return describe(store, args.job)
        if action == "login":
            from .browser import BrowserSource
            with WatchLock(shared_root(), "worker.lock"):
                limiter = Limiter(shared_root())
                try:
                    until = limiter.ready_at("login", "connect")
                    if until > limiter.clock():
                        when = datetime.fromtimestamp(until, timezone.utc).isoformat()
                        raise DownloadError("Shared osu! cooldown/pacing is active until " + when + ". Sign in later; no request sent.")
                    limiter.reserve("login", kind="connect")
                    async with BrowserSource(store.root) as source:
                        async def prompt(text):
                            return await asyncio.to_thread(input, text)
                        await source.login(prompt)
                except Exception as error:
                    from .models import RateLimited
                    if isinstance(error, RateLimited):
                        limiter.cooldown(error.seconds)
                    if isinstance(error, DownloadError):
                        raise
                    raise DownloadError("Browser sign-in unavailable. Use installed Edge/Chrome and complete verification yourself; no bypass is attempted.") from None
                finally:
                    limiter.close()
            return {"message": "osu! sign-in remembered in the dedicated browser's Windows-protected profile."}
        if action == "scan":
            destination = configure(store, args.client, args.root, args.exe)
            libraries = [Destination(**value) for value in store.preference("libraries", [])] + discover()
            ids = scan_all(libraries, cache=store, pending=(store.root / "lazer-inbox",))
            if destination.client == "stable":
                store.confirm_stable(imported_stable_ids(destination))
            return {"message": f"Found {len(ids)} existing/pending set IDs across configured/detected clients. No library files changed.", "set_ids": sorted(ids)}
        if action == "confirm":
            if not args.set_id or args.set_id <= 0:
                raise DownloadError("Use --set-id with the lazer set you have verified in the client.")
            job = store.job(args.job)
            if job["destination"]["client"] != "lazer":
                raise DownloadError("Stable imports are detected by rescanning .osu metadata.")
            if not any(r["set_id"] == args.set_id and r["job"] == job["id"] for r in store.receipts()):
                raise DownloadError("That set does not belong to the selected lazer job.")
            store.confirm_lazer(args.set_id)
            return {"message": f"Recorded your import confirmation for lazer beatmapset {args.set_id}."}
        if action == "import":
            job = store.job(args.job)
            destination = Destination(**job["destination"]).validate()
            saved = configure(store, args.client, args.root, args.exe) if (args.client or args.root or args.exe) else store.preference("destination")
            saved = saved.to_dict() if isinstance(saved, Destination) else saved
            if saved and saved["client"] == destination.client and Path(saved["root"]).resolve() == Path(destination.root).resolve():
                destination = replace(destination, executable=saved.get("executable", destination.executable))
            if destination.client == "stable":
                store.confirm_stable(imported_stable_ids(destination))
                return {"message": "Stable archives are queued in Songs. Open Stable/refresh its map list; run maps scan to verify imports."}
            queued = 0
            for receipt in store.receipts():
                if receipt["job"] == job["id"] and receipt["state"] in {"downloaded", "queued_for_import"}:
                    if not import_archive(destination, receipt["path"]):
                        raise DownloadError("Set lazer's executable using maps configure --exe ... before importing.")
                    store.transition(job["id"], receipt["set_id"], "queued_for_import", placed=receipt["path"])
                    with store.db:
                        store.db.execute("UPDATE history SET state='queued_for_import' WHERE set_id=?", (receipt["set_id"],))
                    queued += 1
            return {"message": f"Handed {queued} archives to lazer. Import is queued; confirm in the client before maps confirm."}
        if action in {"pause", "cancel"}:
            job = store.job(args.job)
            store.control(job["id"], action)
            return describe(store, job["id"])
        if action == "start":
            if worker_active():
                raise DownloadError("A maps job/sign-in is already active. Pause/cancel it first; no second downloader started.")
            destination = configure(store, args.client, args.root, args.exe)
            values = store.preference("filters", {})
            values.update({field.name: getattr(args, field.name) for field in fields(Filters) if getattr(args, field.name, None) is not None})
            filters = Filters(**values).validate()
            job_id = store.create(filters, destination)
        else:
            job = store.job(args.job)
            job_id = job["id"]
            if job["count"] >= job["filters"]["limit"] or (job["exhausted"] and not job["pending"]):
                return {"message": "This batch is complete. Choose start for a new recent batch; history is retained."}
            if worker_active():
                raise DownloadError("A maps worker is already active. Check status; resume after it exits.")
            store.control(job_id, "run")
        if args.foreground:
            store.close()
            store = None
            await worker(args.data_dir, job_id)
            check = Store(Path(args.data_dir) / "beatmap-downloads")
            try:
                return describe(check, job_id)
            finally:
                check.close()
        launch_worker(args.data_dir, job_id)
        return {"message": f"Maps batch #{job_id} started in the background. m opens controls; maps status/pause/resume/cancel also work. Full sets may include other modes/difficulties."}
    finally:
        if store:
            store.close()


async def ask(text):
    return await asyncio.to_thread(input, text)


async def menu(directory, *, output=print, prompt=ask):
    """Compact normal terminal menu; no extra server or GUI framework."""
    from ..cli import parser
    while True:
        store = Store(Path(directory) / "beatmap-downloads")
        try:
            output("\nBeatmap downloads — full archives, including available media")
            output(describe(store)["message"])
            output("1 start recent  2 filters  3 pause  4 resume  5 cancel")
            output("6 sign in  7 destination  8 import saved  9 scan  0 back")
            output("Full sets may include other modes/difficulties. Limits count sets.")
            choice = (await prompt("Maps > ")).strip().lower()
            if choice in {"0", "q", ""}:
                return
            if choice == "2":
                previous = store.preference("filters", Filters().to_dict())
                labels = {"mode": "Mode: osu, taiko, catch, mania", "genre": "Genre: " + ", ".join(GENRES),
                          "category": "Category: " + ", ".join(sorted(STATUSES)),
                          "ranked_from": "Ranked from YYYY-MM-DD UTC (inclusive)",
                          "ranked_to": "Ranked through YYYY-MM-DD UTC (inclusive)",
                          "limit": "Maximum successful beatmapsets",
                          "min_length": "Minimum full difficulty length (seconds)",
                          "max_length": "Maximum full difficulty length (seconds)",
                          "mapper": "Host mapper: exact username or user ID"}
                values = dict(previous)
                for key, label in labels.items():
                    reply = (await prompt(f"{label} [{previous[key] if previous[key] is not None else 'any'}] (Enter keeps, - clears): ")).strip()
                    if reply == "-":
                        values[key] = None if key in {"min_length", "max_length"} else ("" if key in {"mapper", "ranked_from", "ranked_to"} else getattr(Filters(), key))
                    elif reply:
                        values[key] = int(reply) if key == "limit" else (float(reply) if key in {"min_length", "max_length"} else reply)
                filters = Filters(**values).validate()
                store.remember("filters", filters.to_dict())
                output("Filters remembered. Mode and length must match the same difficulty; missing ranked dates are excluded.")
                continue
            if choice == "7":
                choices = discover()
                for index, destination in enumerate(choices, 1):
                    output(f"{index}: {destination.client} — {destination.root}")
                reply = (await prompt("Installation number, or Enter to enter a custom path: ")).strip()
                if reply:
                    destination = choices[int(reply) - 1]
                    configure(store, destination.client, destination.root, destination.executable)
                else:
                    client = (await prompt("Client [stable/lazer]: ")).strip().lower()
                    root = (await prompt("Installation/Songs (stable), or storage directory (lazer): ")).strip().strip('"')
                    executable = (await prompt("Client .exe (Enter leaves archives downloaded for later import): ")).strip().strip('"')
                    configure(store, client, root, executable)
                output("Destination remembered. Existing library files will never be replaced.")
                continue
        except (ValueError, IndexError, DownloadError, OSError):
            output("Setting not accepted. Previous preferences and library preserved.")
            continue
        finally:
            store.close()
        action = {"1": "start", "3": "pause", "4": "resume", "5": "cancel", "6": "login", "8": "import", "9": "scan"}.get(choice)
        if action:
            try:
                args = parser().parse_args(["--data-dir", str(directory), "maps", action])
                result = await execute(args)
                output(result["message"])
                if action in {"start", "resume"}:
                    return  # Main terminal's live feed reports subsequent progress.
            except (DownloadError, OSError, RuntimeError, ValueError) as error:
                output(str(error) if isinstance(error, DownloadError) else "Maps action unavailable. Progress retained.")


def progress_notice(directory):
    import sqlite3
    path = Path(directory) / "beatmap-downloads"
    if not (path / "downloads.sqlite3").is_file():
        return None
    try:
        store = Store(path)
    except sqlite3.Error:
        return "Maps checkpoint unavailable; progress is retained. Inspect maps status before resuming."
    try:
        return describe(store)["message"]
    finally:
        store.close()
