"""Interactive first-run setup followed by the detachable welcome terminal."""

import asyncio
import json
from pathlib import Path
import subprocess

from .console import show_console
from .lastfm import CredentialStore
from .tray import install_startup, launch_tray, require_windows, startup_installed


def setup_complete(directory):
    try:
        state = json.loads((directory / "setup-state.json").read_text("utf-8"))
        return isinstance(state, dict) and state.get("complete") is True
    except (OSError, ValueError):
        return False


async def first_run(directory: Path, *, enable_startup=False):
    require_windows()
    directory.mkdir(parents=True, exist_ok=True)
    credentials = CredentialStore().load()
    if not credentials or not credentials.session_key:
        from .cli import authorize
        print("Connect Last.fm once. Credentials stay in Windows Credential Manager.")
        await authorize(directory)
    else:
        print("Last.fm authorization remembered.")
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
    launch_tray(directory)
    # Clear installer output only on success; preserve error messages on failure.
    subprocess.run(["cmd.exe", "/d", "/c", "cls"], check=False)
    await show_console(directory)
