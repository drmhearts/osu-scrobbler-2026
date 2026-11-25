"""CLI/menu facade over one checkpoint and independent background worker."""

import asyncio
from dataclasses import replace
from dataclasses import fields
from datetime import datetime, timezone
from pathlib import Path

from ..config import WatchLock
from ..terminal import color
from .library import discover, scan_all, imported_stable_ids
from .limiter import Limiter
from .models import Destination, DownloadError, Filters, GENRES, MODES, STATUSES
from .service import import_archive, launch_worker, shared_root, worker, worker_active
from .store import Store

ACTIONS = ("menu", "start", "resume", "pause", "cancel", "status", "scan", "login",
           "configure", "import", "confirm", "collection", "collection-confirm", "worker")


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
    parser.add_argument("--limit", type=int, help="Positive whole number of successful beatmapsets, default 25")
    parser.add_argument("--min-length", type=float, help="Full difficulty duration in seconds")
    parser.add_argument("--max-length", type=float)
    parser.add_argument("--mapper", help="Exact host mapper username or numeric user ID")
    parser.add_argument("--foreground", action="store_true", help="Run worker in this terminal instead of background")
    parser.add_argument("--set-id", type=int, help="Explicitly confirm a downloaded lazer set was imported")
    parser.add_argument("--collection", help="Optional existing/new in-game collection name; assignment is a manual step")
    parser.add_argument("--new-collection", action="store_true", help="Mark --collection as a new collection to create in the game")
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
        if not choices:
            raise DownloadError("No osu! installation found. Use maps → destination, or maps configure --client lazer --root STORAGE_DIRECTORY.")
        destination = next((choice for choice in choices if choice.client == "lazer"), choices[0]).validate()
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
    text = "\n".join(job_summary(job))
    if job["collection"]:
        text += f"\nCollection '{job['collection']['name']}': {job['collection_pending']} sets need import/assignment in the game. Use maps collection."
    return {"message": text, "download_job": job, "receipts": store.receipts()}


def progress_bar(count, total, width=18):
    """Exact completed-set progress; no invented byte percentage."""
    filled = min(width, max(0, count * width // total))
    percent = min(100, max(0, count * 100 // total))
    return f"[{'█' * filled}{'·' * (width - filled)}] {count}/{total} sets  {percent}%"


def job_summary(job):
    state = job["state"].replace("_", " ").title()
    if state == "Running" and job["message"].startswith("Downloading"):
        state = "Downloading"
    tone = "92" if state == "Complete" else "93" if state in {"Waiting", "Cooldown", "Paused", "Auth Required"} else "91" if state in {"Error", "Cancelled"} else "96"
    lines = [f"Batch #{job['id']}  {color(state, tone)}  ·  {job['destination']['client']}",
             color(progress_bar(job["count"], job["filters"]["limit"]), "96")]
    detail = job["message"] or "Ready."
    lines.append(detail)
    if job["last_id"] or job["pending"]:
        lines.append(f"Last saved: {job['last_id'] or 'none'}  ·  {job['pending']} waiting in queue")
    return lines


def compact_progress(job, tick=None):
    state = job["state"]
    detail = job["message"] or state.replace("_", " ")
    if tick is not None and state in {"scanning", "running", "waiting"}:
        detail = "|/-\\"[tick % 4] + " " + detail
    if len(detail) > 34:
        detail = detail[:31].rstrip() + "…"
    return color(f"Maps  {progress_bar(job['count'], job['filters']['limit'], 14)}  ·  {detail}", "96")


def collection_plan(name, kind="existing"):
    name = str(name).strip()
    if not name or len(name) > 120 or any(ord(char) < 32 for char in name):
        raise DownloadError("Enter a collection name (1–120 printable characters).")
    if kind not in {"existing", "new"}:
        raise DownloadError("Type existing or new for the collection kind.")
    return {"name": name, "kind": kind}


def collection_guidance(store, job_id=None):
    job = store.job(job_id)
    plan = job["collection"]
    if not plan:
        return {"message": "This batch has no collection plan. Enable it when starting a new batch."}
    members = store.collection_members(job["id"])
    pending = [f"{item['set_id']} ({item['state'].replace('_', ' ')})" for item in members
               if item["state"] != "confirmed_by_user"]
    instructions = ("In lazer: after import, find each set in Song Select, open its options and add the entire set in Manage collections. "
                    "In Stable: refresh/import, select the set, open Beatmap Options (F3) → Manage Collections. ")
    shown = ", ".join(pending[:20]) + (f" … and {len(pending) - 20} more (use --json maps collection for all IDs)" if len(pending) > 20 else "")
    text = (f"Collection '{plan['name']}' ({plan['kind']}): {shown if pending else 'no pending sets'}. "
            + instructions + "Create the named collection there if new. The app never writes collection databases; "
            "assignment is unverified until you inspect it in the game.")
    return {"message": text, "collection": plan, "members": members}


def count_warning(limit, limiter):
    recent = limiter.recent_downloads()
    remaining = max(0, limiter.hourly - recent)
    if limit <= remaining:
        return ""
    return (f"Requested {limit} beatmapsets; {recent} archive requests were recorded in the past hour. "
            f"This app permits at most {limiter.hourly} archive requests per rolling hour and spaces them "
            f"at least {limiter.spacing} seconds apart. The batch may take hours. osu! can still limit the "
            "account independently; progress and cooldown are saved. Continuing.")


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
        if action == "collection":
            return collection_guidance(store, args.job)
        if action == "collection-confirm":
            if not args.set_id or args.set_id <= 0:
                raise DownloadError("Use --set-id with the set you added and checked in the game's collection UI.")
            job = store.job(args.job)
            store.confirm_collection(job["id"], args.set_id)
            return {"message": f"Recorded your in-game collection check for set {args.set_id}. The app cannot verify collection membership directly."}
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
            if args.new_collection and not args.collection:
                raise DownloadError("--new-collection requires --collection NAME.")
            plan = collection_plan(args.collection, "new" if args.new_collection else "existing") if args.collection else None
            limiter = Limiter(shared_root())
            try:
                warning = count_warning(filters.limit, limiter)
            finally:
                limiter.close()
            job_id = store.create(filters, destination, collection=plan)
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
                result = describe(check, job_id)
                if action == "start" and warning:
                    result["message"] = warning + "\n" + result["message"]
                return result
            finally:
                check.close()
        launch_worker(args.data_dir, job_id)
        return {"message": (warning + "\n" if action == "start" and warning else "") +
                f"Maps batch #{job_id} started in the background. m opens controls; maps status/pause/resume/cancel also work. Full sets may include other modes/difficulties."}
    finally:
        if store:
            store.close()


async def ask(text):
    return await asyncio.to_thread(input, text)


class MenuBack(Exception):
    """Leave the current form without changing its saved preferences."""


async def _reply(prompt, output, label, topic="download"):
    from ..help import help_text
    while True:
        value = (await prompt(label)).strip()
        if value.casefold() in {"back", "cancel"}:
            raise MenuBack
        if value.casefold() == "help" or value.casefold().startswith("help "):
            requested = value[5:].strip() if len(value) > 4 else topic
            output(help_text(requested))
            continue
        return value


async def _filters_menu(store, output, prompt):
    previous = store.preference("filters", Filters().to_dict())
    values = dict(previous)
    labels = {
        "mode": "Mode: osu (standard), taiko, catch or mania",
        "genre": "Genre: " + ", ".join(GENRES),
        "category": "Category: " + ", ".join(sorted(STATUSES)),
        "ranked_from": "Ranked from: YYYY-MM-DD UTC (inclusive)",
        "ranked_to": "Ranked through: YYYY-MM-DD UTC (inclusive)",
        "limit": "How many beatmapsets? Positive whole number; full sets may include other modes",
        "min_length": "Minimum full difficulty length in seconds, e.g. 90",
        "max_length": "Maximum full difficulty length in seconds, e.g. 240",
        "mapper": "Host mapper: exact username or numeric user ID",
    }

    async def edit(key):
        default = Filters()
        while True:
            value = values[key]
            reply = await _reply(prompt, output, f"{labels[key]} [current: {value if value is not None and value != '' else 'any'}]. Enter keeps, - clears, back cancels: ")
            if not reply:
                return
            if reply == "-":
                values[key] = getattr(default, key)
                return
            try:
                parsed = int(reply) if key == "limit" else (float(reply) if key in {"min_length", "max_length"} else reply.casefold() if key in {"mode", "genre", "category"} else reply)
                trial = dict(values)
                trial[key] = parsed
                # Validate single-field shape before committing, while allowing
                # interdependent ranges to be corrected at the end.
                if key == "limit" and (type(parsed) is not int or parsed <= 0):
                    raise ValueError("Enter a positive whole number of beatmapsets, e.g. 25.")
                if key in {"min_length", "max_length"} and parsed < 0:
                    raise ValueError("Length must be zero or greater, in seconds.")
                if key == "mode" and parsed not in MODES or key == "genre" and parsed not in GENRES or key == "category" and parsed not in STATUSES:
                    raise ValueError("Choose one of the listed options.")
                if key in {"ranked_from", "ranked_to"}:
                    Filters(**{**Filters().to_dict(), key: parsed}).validate()
                values[key] = parsed
                return
            except (ValueError, OverflowError) as error:
                output(str(error) if str(error) else "Enter the requested format; type help for details.")

    for key in labels:
        await edit(key)
    while True:
        try:
            filters = Filters(**values).validate()
            break
        except ValueError as error:
            output(str(error))
            key = (await _reply(prompt, output, "Which filter should change? Type mode, genre, category, ranked_from, ranked_to, limit, min_length, max_length or mapper; back cancels: ")).casefold().replace("-", "_")
            if key in labels:
                await edit(key)
            else:
                output("Type one of the listed filter names.")
    store.remember("filters", filters.to_dict())
    output("Filters saved. Count means beatmapsets; mode and length must match the same difficulty.")


async def _destination_menu(store, output, prompt):
    choices = sorted(discover(), key=lambda item: (item.client != "lazer", item.root.casefold()))
    saved = store.preference("destination")
    if saved:
        output(f"Current: {saved['client']} — {saved['root']}")
    for index, destination in enumerate(choices, 1):
        output(f"{index}: {destination.client} — {destination.root}")
    default = "current" if saved else ("1" if choices else "custom")
    while True:
        reply = (await _reply(prompt, output, f"Destination: type an installation number, custom, or current [default: {default}]; back cancels: ")).casefold() or default
        if reply == "current" and saved:
            try:
                destination = Destination(**saved).validate()
                break
            except (ValueError, OSError) as error:
                output(str(error) + " Choose a detected installation or type custom to correct it.")
                continue
        if reply.isdecimal() and 1 <= int(reply) <= len(choices):
            try:
                destination = choices[int(reply) - 1].validate()
                break
            except (ValueError, OSError) as error:
                output(str(error) + " Try another installation or custom.")
                continue
        if reply == "custom":
            client_default = saved["client"] if saved else "lazer"
            while True:
                client = (await _reply(prompt, output, f"Which client? Type lazer or stable [default: {client_default}]; back cancels: ")).casefold() or client_default
                if client in {"lazer", "stable"}:
                    break
                output("Type exactly 'lazer' or 'stable'. A folder path belongs in the next prompt.")
            root_default = saved["root"] if saved and saved["client"] == client else ""
            example = r"C:\Users\You\AppData\Roaming\osu" if client == "lazer" else r"C:\Games\osu!\Songs"
            root_label = "lazer storage directory containing files" if client == "lazer" else "Stable installation or Songs directory"
            root = await _reply(prompt, output, f"{root_label}. Enter a folder path, e.g. {example}" + (f" [default: {root_default}]" if root_default else "") + "; back cancels: ")
            root = (root or root_default).strip('"')
            exe_default = saved.get("executable", "") if saved and saved["client"] == client and saved["root"] == root else ""
            executable = await _reply(prompt, output, f"Client .exe path (optional; Enter {('keeps ' + exe_default) if exe_default else 'skips automatic lazer import'}), e.g. C:\\Games\\osu!\\osu!.exe; back cancels: ")
            executable = (executable or exe_default).strip('"')
            while True:
                try:
                    destination = Destination(client, root, executable).validate()
                    break
                except (ValueError, OSError) as error:
                    output(str(error))
                    field = (await _reply(prompt, output, "Correct which field? Type client, path or exe; back cancels: ")).casefold()
                    if field == "client":
                        client = (await _reply(prompt, output, "Which client? Type lazer or stable: ")).casefold()
                    elif field == "path":
                        root = (await _reply(prompt, output, "Enter the existing installation/storage folder path: ")).strip('"')
                    elif field == "exe":
                        executable = (await _reply(prompt, output, "Enter an existing .exe path, or Enter to skip: ")).strip('"')
                    else:
                        output("Type client, path or exe.")
            break
        output("Type a shown number, 'custom', or 'current'. Type help for destination advice.")
    configure(store, destination.client, destination.root, destination.executable)
    output(f"Destination saved: {destination.client} — {destination.root}. Existing files remain untouched.")


async def _collection_prompt(store, output, prompt):
    preference = store.preference("collection", {"enabled": False, "kind": "existing", "name": ""})
    default = "yes" if preference.get("enabled") else "no"
    while True:
        answer = (await _reply(prompt, output, f"Add downloaded maps to a collection? Type yes or no [default: {default}]; back cancels: ", "collections")).casefold() or default
        if answer in {"yes", "no"}:
            break
        output("Type yes or no.")
    if answer == "no":
        store.remember("collection", {**preference, "enabled": False})
        return None
    while True:
        kind = (await _reply(prompt, output, f"Collection: type existing or new [default: {preference.get('kind', 'existing')}]; back cancels: ", "collections")).casefold() or preference.get("kind", "existing")
        if kind in {"existing", "new"}:
            break
        output("Type existing or new.")
    while True:
        name = await _reply(prompt, output, f"Collection name in {kind} list [default: {preference.get('name') or 'none'}]; back cancels: ", "collections")
        try:
            plan = collection_plan(name or preference.get("name", ""), kind)
            break
        except DownloadError as error:
            output(str(error))
    store.remember("collection", {"enabled": True, **plan})
    output("Collection plan saved. Add imported sets in the game's collection UI; this tool will list them and never claim unverified assignment.")
    return plan


async def menu(directory, *, output=print, prompt=ask):
    """Compact normal terminal menu; no extra server or GUI framework."""
    from ..cli import parser
    while True:
        store = Store(Path(directory) / "beatmap-downloads")
        try:
            output("\n" + color("BEATMAP DOWNLOADS", "95"))
            output(color("─" * 55, "90"))
            output(describe(store)["message"])
            output("")
            output(color("Download", "95") + "   1 Start recent   2 Filters     3 Pause")
            output("           4 Resume         5 Cancel")
            output(color("Setup", "95") + "      6 Sign in        7 Destination")
            output(color("Library", "95") + "    8 Import saved   9 Scan        c Collection")
            output(color("Other", "95") + "      h Help          0 Back")
            output(color("Count = full beatmapsets; other modes may be included.", "90"))
            choice = (await prompt("Maps > Choose a key, or type help [topic]: ")).strip().lower()
            if choice in {"0", "q", ""}:
                return
            if choice in {"h", "help"} or choice.startswith("help "):
                from ..help import help_text
                output(help_text(choice[5:].strip() if choice.startswith("help ") else "maps"))
                continue
            if choice == "2":
                await _filters_menu(store, output, prompt)
                continue
            if choice == "7":
                await _destination_menu(store, output, prompt)
                continue
            if choice == "c":
                output(collection_guidance(store)["message"])
                continue
            if choice == "1":
                plan = await _collection_prompt(store, output, prompt)
            else:
                plan = None
        except MenuBack:
            output("Back to maps. Saved settings and progress are unchanged.")
            continue
        except (ValueError, IndexError, DownloadError, OSError) as error:
            output(str(error) if str(error) else "Setting not accepted; saved preferences and library were kept.")
            continue
        finally:
            store.close()
        action = {"1": "start", "3": "pause", "4": "resume", "5": "cancel", "6": "login", "8": "import", "9": "scan"}.get(choice)
        if action:
            try:
                argv = ["--data-dir", str(directory), "maps", action]
                if action == "start" and plan:
                    argv += ["--collection", plan["name"]]
                    if plan["kind"] == "new":
                        argv.append("--new-collection")
                args = parser().parse_args(argv)
                result = await execute(args)
                output(result["message"])
                if action in {"start", "resume"}:
                    return  # Main terminal's live feed reports subsequent progress.
            except (DownloadError, OSError, RuntimeError, ValueError) as error:
                output(str(error) if isinstance(error, DownloadError) else "Maps action unavailable. Progress retained.")
        else:
            output("Type a shown command, h for help, or 0 to go back.")


def progress_notice(directory, *, tick=None):
    import sqlite3
    path = Path(directory) / "beatmap-downloads"
    if not (path / "downloads.sqlite3").is_file():
        return None
    try:
        store = Store(path)
    except sqlite3.Error:
        return "Maps checkpoint unavailable; progress is retained. Inspect maps status before resuming."
    try:
        try:
            return compact_progress(store.job(), tick=tick)
        except DownloadError:
            return None
    finally:
        store.close()
