"""Remove this app's startup and cached runtimes without touching user data."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import shutil
import sys


def remove_owned_launcher(directory: Path) -> None:
    """Delete only launchers whose contents still match our recorded install."""
    state_path = directory / "local-launcher-state.json"
    try:
        state = json.loads(state_path.read_text("utf-8"))
    except (OSError, ValueError):
        return
    if not isinstance(state, dict):
        return
    for name in ("START.cmd", "launch.ps1"):
        path = directory / name
        digest = state.get(name)
        if (isinstance(digest, str) and len(digest) == 64 and path.is_file()
                and not path.is_symlink() and hashlib.sha256(path.read_bytes()).hexdigest() == digest):
            path.unlink()
    state_path.unlink(missing_ok=True)


async def uninstall(directory: Path) -> dict:
    """Stop the monitor before removing only this profile's installed runtime."""
    from .tray import remove_startup
    from .updater import stop_running

    directory = Path(directory).resolve()
    if sys.platform != "win32":
        raise RuntimeError("UNINSTALL.cmd is for the Windows package.")
    await stop_running(directory)
    remove_startup(directory)
    updates = directory / "updates"
    if updates.is_symlink() or updates.resolve() != directory / "updates":
        raise RuntimeError("Update folder is redirected; remove it manually.")
    if updates.is_dir():
        if Path(sys.executable).resolve().is_relative_to(updates):
            raise RuntimeError("Uninstall from the original Windows download, not the cached runtime.")
        shutil.rmtree(updates)
    remove_owned_launcher(directory)
    (directory / "install-root.json").unlink(missing_ok=True)
    (directory / "launcher-state.json").unlink(missing_ok=True)
    return {"message": "Startup and installed runtime removed. Saved settings, history, credentials and downloaded maps remain."}
