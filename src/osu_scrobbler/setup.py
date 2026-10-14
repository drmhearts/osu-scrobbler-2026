"""Interactive first-run setup followed by the detachable welcome terminal."""

import asyncio
import json
from pathlib import Path
import subprocess

from .console import show_console
from .lastfm import CredentialStore
from .tray import install_startup, launch_tray, require_windows, startup_installed, wait_for_tray, tray_health
from .updater import startup, stop_running
from . import __version__
from .welcome import welcome


def setup_complete(directory):
    try:
        state = json.loads((directory / "setup-state.json").read_text("utf-8"))
        return isinstance(state, dict) and state.get("complete") is True
    except (OSError, ValueError):
        return False


async def first_run(directory: Path, *, enable_startup=False, check_updates=True):
    require_windows()
    directory.mkdir(parents=True, exist_ok=True)
    credentials = CredentialStore().load()
    if not credentials or not credentials.session_key:
        from .cli import authorize
        print("Connect Last.fm once. Credentials stay in Windows Credential Manager.")
        await authorize(directory)
    registered = startup_installed(directory)
    if not setup_complete(directory) and not enable_startup and not registered:
        answer = await asyncio.to_thread(input, "Start automatically when you sign in to Windows? [y/N]: ")
        enable_startup = answer.strip().casefold() in {"y", "yes"}
    if enable_startup or registered:
        install_startup(directory)
    # Only remember the setup decision after successful authorization/startup.
    marker = directory / "setup-state.json"
    temporary = marker.with_suffix(".tmp")
    temporary.write_text(json.dumps({"complete": True}) + "\n", "utf-8")
    temporary.replace(marker)
    # Check/install before launching the hidden process, keeping progress and
    # errors visible and keeping long downloads outside the readiness timeout.
    if check_updates and await startup(directory, command="setup", enable_startup=enable_startup, output=print):
        return
    try:
        running = await tray_health(directory)
    except (RuntimeError, OSError, ValueError, asyncio.TimeoutError):
        running = None
    if running and running.get("version") != __version__:
        await stop_running(directory)
    process = launch_tray(directory, skip_update_check=True)
    await wait_for_tray(directory, process)
    # Clear installer output only on success; preserve error messages on failure.
    subprocess.run(["cmd.exe", "/d", "/c", "cls"], check=False)
    if enable_startup or registered:
        print(welcome(directory, background=True))
        print("Ready. Click the pink tray icon for commands.")
        await asyncio.sleep(2)
        return  # The one-time setup window closes; background scrobbling stays on.
    await show_console(directory)


async def launch(directory: Path, *, skip_update_check=False):
    """START opens setup only when needed; all later launches stay in the tray."""
    require_windows()
    credentials = CredentialStore().load()
    if not setup_complete(directory) or not credentials or not credentials.session_key:
        from .tray import python_executable
        process = await asyncio.create_subprocess_exec(python_executable(), "-m", "osu_scrobbler",
            "--data-dir", str(directory.resolve()), "setup", creationflags=subprocess.CREATE_NEW_CONSOLE)
        if await process.wait():
            raise RuntimeError("Setup did not finish. Reopen START.cmd to retry.")
        return
    # Updates stay hidden. Network failures keep the installed app.
    if await startup(directory, command="tray", skip=skip_update_check):
        return
    try:
        health = await tray_health(directory)
    except (RuntimeError, OSError, ValueError, asyncio.TimeoutError):
        health = None
    if health and health.get("version") == __version__ and health.get("ready"):
        return
    if health:
        await stop_running(directory)
    process = launch_tray(directory, skip_update_check=True)
    await wait_for_tray(directory, process)
