"""Terminal commands and secure browser-based Last.fm authorization."""

import argparse
import asyncio
import getpass
import json
import sys
import webbrowser
from dataclasses import asdict
from pathlib import Path

from .config import Config, default_data_dir
from .display import render
from .ipc import send_command
from .lastfm import CredentialStore, Credentials, LastFmClient


def parser():
    result = argparse.ArgumentParser(description="Read-only tosu telemetry → MusicBrainz → Last.fm")
    result.add_argument("--data-dir", type=Path, default=default_data_dir(), help="Private configuration/history directory")
    result.add_argument("--json", action="store_true", help="Show structured output instead of short terminal messages")
    result.add_argument("--skip-update-check", action="store_true", help=argparse.SUPPRESS)
    commands = result.add_subparsers(dest="command", required=True)
    auth = commands.add_parser("auth", help="Authorize Last.fm through a browser and save credentials in the OS vault")
    auth.add_argument("--session", action="store_true", help="Securely enter an already authorized session key")
    watch = commands.add_parser("watch", help="Start the sole gameplay watcher and local command server")
    watch.add_argument("--dry-run", action="store_true", help="Preview submissions; never write to Last.fm")
    watch.add_argument("--fixture", type=Path, help="Simulated telemetry replay; requires --dry-run and makes no MusicBrainz requests")
    commands.add_parser("status", help="Show watcher selection, timing, candidates and delivery status")
    info = commands.add_parser("info", help="Show track information")
    info.add_argument("--full", action="store_true", help="Include recording/release details, identifiers and source links")
    commands.add_parser("force", help="Submit selected metadata immediately (custom listening behavior)")
    export = commands.add_parser("export", help="Export accepted history to UTF-8 text")
    export.add_argument("destination", nargs="?", help="Optional .txt destination")
    config = commands.add_parser("config", help="Non-secret settings and persistent metadata corrections")
    actions = config.add_subparsers(dest="action")
    actions.add_parser("show")
    setting = actions.add_parser("set")
    setting.add_argument("key", choices=list(asdict(Config())))
    setting.add_argument("value", help="JSON boolean/integer, or string")
    choose = actions.add_parser("choose", help="Persist a numbered MusicBrainz candidate for the selected track")
    choose.add_argument("index", type=int)
    correct = actions.add_parser("correct", help="Persist selected-track metadata")
    correct.add_argument("--title", required=True)
    correct.add_argument("--artist", required=True)
    for field in ("album", "album-artist", "recording-id", "release-id"):
        correct.add_argument("--" + field)
    correct.add_argument("--duration", type=float, help="Full recording duration in seconds")
    actions.add_parser("clear-correction")
    queue = commands.add_parser("queue", help="Inspect history or explicitly retry a delivery")
    queue_actions = queue.add_subparsers(dest="queue_action")
    queue_actions.add_parser("list")
    retry = queue_actions.add_parser("retry")
    retry.add_argument("id")
    retry.add_argument("--accept-duplicate-risk", action="store_true", help="Allow uncertain requests to be retried; Last.fm may already have accepted them")
    commands.add_parser("quit", help="Stop the watcher")
    tray = commands.add_parser("tray", help="Run the Windows tray monitor; starts watching when osu! opens")
    startup = tray.add_mutually_exclusive_group()
    startup.add_argument("--install", action="store_true", help="Enable tray monitoring at sign-in and launch it now")
    startup.add_argument("--uninstall", action="store_true", help="Remove this profile's sign-in startup and stop its tray monitor")
    tray.add_argument("--dry-run", action="store_true", help="Preview only; never send Last.fm scrobbles")
    commands.add_parser("console", help="Open a terminal view of the running background watcher")
    commands.add_parser("launch", help="First-time setup, then quiet tray launches")
    setup = commands.add_parser("setup", help="Connect Last.fm, choose background startup, and open the welcome terminal")
    setup.add_argument("--enable-startup", action="store_true", help="Enable Windows sign-in startup without asking again")
    setup.add_argument("--disable-startup", action="store_true", help="Disable Windows sign-in startup")
    setup.add_argument("--update", action="store_true", help="Update and restart the app")
    privacy = commands.add_parser("privacy", help="Explain local data and network requests")
    privacy.add_argument("--clear-cache", action="store_true", help="Remove lookup cache and older activity logs; keep history/corrections (stop watching first)")
    update = commands.add_parser("update", help="Check/install the latest published release without visiting GitHub")
    update_action = update.add_mutually_exclusive_group()
    update_action.add_argument("--check", action="store_true", help="Check only; don't install")
    update_action.add_argument("--rollback", action="store_true", help="Restore the previous installation and disable automatic checks (stop the app first)")
    return result


def _value(value):
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return value


async def authorize(directory: Path, existing_session=False):
    store = CredentialStore()
    old = store.load()  # Check native secure storage before asking for credentials.
    api_key = old.api_key if old else getpass.getpass("Last.fm application API key: ")
    api_secret = old.api_secret if old else getpass.getpass("Last.fm application API secret: ")
    client = LastFmClient(Credentials(api_key, api_secret))
    try:
        if existing_session:
            session = getpass.getpass("Authorized Last.fm session key: ")
        else:
            token = await client.get_token()
            if not webbrowser.open(client.authorization_url(token)):
                raise RuntimeError("Could not open the authorization browser. Use Windows, or auth --session with an already authorized key.")
            await asyncio.to_thread(input, "Authorize this application in the opened browser, then press Enter: ")
            session = await client.get_session(token)
        store.save(Credentials(api_key, api_secret, session))
    finally:
        await client.aclose()
    if (directory / "control.json").exists():
        try:
            await send_command(directory, {"command": "auth-reload"})
        except RuntimeError:
            pass
    return {"authorized": True, "storage": "OS credential vault"}


async def execute(args):
    directory = args.data_dir.expanduser().resolve()
    directory.mkdir(parents=True, exist_ok=True)
    command = args.command
    if command == "launch":
        from .setup import launch
        await launch(directory)
        return None
    if command == "setup":
        from .setup import first_run
        from .updater import startup, stop_running
        if args.disable_startup:
            from .tray import remove_startup
            remove_startup(directory)
            await stop_running(directory)
            return {"message": "Startup: off."}
        if args.update:
            await stop_running(directory)
            from .updater import update
            await update(directory, output=print)
        if await startup(directory, command="setup", skip=args.skip_update_check,
                         enable_startup=args.enable_startup, output=print):
            return None
        await first_run(directory, enable_startup=args.enable_startup, check_updates=False)
        return None
    if command == "update":
        import httpx
        from .updater import update, UpdateError
        try:
            return await update(directory, check_only=args.check, rollback=args.rollback)
        except (httpx.HTTPError, OSError, ValueError, UpdateError, asyncio.TimeoutError):
            return {"error": "Update unavailable or app still running. Stop the tray/watch and try again; the installed app is kept."}
    if command == "privacy":
        from .privacy import clear_cache, describe
        return clear_cache(directory) if args.clear_cache else describe(directory)
    if command == "tray":
        from .tray import run_tray, install_startup, remove_startup, launch_tray
        if args.uninstall:
            remove_startup(directory)
            try:
                await asyncio.wait_for(send_command(directory, {"command": "quit"}, control_name="tray-control.json"), 3)
            except (RuntimeError, OSError, ValueError, asyncio.TimeoutError):
                return {"message": "Automatic startup removed. If an older tray icon remains, use Exit to stop it."}
            return {"message": "Automatic startup removed and background monitor stopped."}
        if args.install:
            install_startup(directory, dry_run=args.dry_run)
            launch_tray(directory, dry_run=args.dry_run)
            return {"message": "Enabled. The tray icon waits for osu! and starts at Windows sign-in."}
        from .updater import startup
        if await startup(directory, command="tray", skip=args.skip_update_check, check=not args.dry_run, dry_run=args.dry_run):
            return None
        # Native tray loop runs on the main thread; the watcher uses a worker thread.
        run_tray(directory, dry_run=args.dry_run)
        return None
    if command == "console":
        from .console import show_console
        await show_console(directory)
        return None
    if command == "auth":
        return await authorize(directory, args.session)
    if command == "watch":
        if args.fixture and not args.dry_run:
            raise ValueError("--fixture requires --dry-run")
        from .updater import startup
        if await startup(
                directory, command="watch", json_output=args.json, skip=args.skip_update_check,
                check=not args.dry_run and not args.fixture, dry_run=args.dry_run, fixture=args.fixture,
                output=lambda message: print(render(message, json_output=args.json)) if sys.stdout is not None else None):
            return None
        from .watcher import Watcher
        await Watcher(directory, Config.load(directory), dry_run=args.dry_run, fixture=bool(args.fixture), json_output=args.json).run(args.fixture)
        return None
    request = {"command": command}
    if command == "export":
        request["destination"] = args.destination
    elif command == "queue" and args.queue_action == "retry":
        request.update(command="retry", id=args.id, accept_duplicate_risk=args.accept_duplicate_risk)
    elif command == "config":
        request["action"] = args.action or "show"
        if args.action == "set":
            request.update(key=args.key, value=_value(args.value))
        elif args.action == "choose":
            request["index"] = args.index
        elif args.action == "correct":
            request["track"] = {key: value for key, value in vars(args).items()
                                if key in {"title", "artist", "album", "album_artist", "recording_id", "release_id", "duration"} and value is not None}
        if not (directory / "control.json").exists() and request["action"] in {"show", "set"}:
            config = Config.load(directory)
            if request["action"] == "set":
                config = Config(**{**asdict(config), args.key: request["value"]})
                config.save(directory)
                return {"config": asdict(config), "saved": args.key, "value": getattr(config, args.key), "restart_required": False}
            return {"config": asdict(config)}
    try:
        return await send_command(directory, request)
    except RuntimeError:
        if command == "export" or (command == "queue" and args.queue_action in {None, "list"}):
            from .storage import Storage
            storage = Storage(directory / "history.db")
            try:
                if command == "queue":
                    return {"history": storage.history()}
                config = Config.load(directory)
                destination = Path(args.destination or config.export_path or directory / "scrobbles.txt").expanduser()
                return {"exported": storage.export(destination), "destination": str(destination)}
            finally:
                storage.close()
        raise


def main(argv=None):
    args = parser().parse_args(argv)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    try:
        response = asyncio.run(execute(args))
        if response is not None and sys.stdout is not None:
            print(render(response, full=getattr(args, "full", False), json_output=args.json))
            if "error" in response:
                return 1
        return 0
    except KeyboardInterrupt:
        return 130
    except (ValueError, RuntimeError, OSError) as error:
        # These are local validation / safe domain errors; third-party network failures are caught in adapters.
        if sys.stderr is not None:
            print(str(error), file=sys.stderr)
        # Background launches never open error dialogs. An intentional setup
        # window reports errors normally; START remains available to retry.
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
