"""Check the actual installed launcher, native tray and clean shutdown in CI."""

import asyncio
import ctypes
import os
from pathlib import Path
import subprocess
import sys

from osu_scrobbler.ipc import send_command
from osu_scrobbler.tray import wait_for_tray
from osu_scrobbler.updater import stop_running


async def check(directory):
    ctypes.windll.user32.GetShellWindow.restype = ctypes.c_void_p
    if not ctypes.windll.user32.GetShellWindow():
        print("::notice::Native notification-icon check unavailable: this Windows runner has no desktop shell. Runtime/install checks passed.")
        return
    (directory / "startup-diagnostics").touch()
    powershell = Path(os.environ["SystemRoot"]) / "System32/WindowsPowerShell/v1.0/powershell.exe"
    process = await asyncio.create_subprocess_exec(str(powershell), "-NoProfile", "-NonInteractive",
        "-WindowStyle", "Hidden", "-ExecutionPolicy", "Bypass", "-File", str(directory / "launch.ps1"),
        "-Background", "-SkipUpdateCheck", stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=subprocess.CREATE_NO_WINDOW)
    if await process.wait():
        raise RuntimeError("Installed launch script failed")
    try:
        health = await wait_for_tray(directory, timeout=35)
        assert health["ready"] is True and health["status"] in {"Waiting for osu!", "Watching osu!"}
        # A developer may have osu! open while building locally. Neither state
        # requires first-time authorization or Last.fm contact for this check.
        response = await send_command(directory, {"command": "quit"}, control_name="tray-control.json")
        assert "stopped" in response["message"]
    except Exception as error:
        diagnostic = directory / "startup-error.txt"
        if diagnostic.is_file():
            message = diagnostic.read_text("utf-8").replace("%", "%25").replace("\n", "%0A").replace("\r", "%0D")
            print("::error::Native startup error: " + message, flush=True)
        try:
            health = await send_command(directory, {"command": "health"}, control_name="tray-control.json")
            print("::error::Native monitor health: " + repr(health), flush=True)
        except (RuntimeError, OSError):
            print("Native monitor did not open its IPC server", flush=True)
        raise
    finally:
        # The smoke profile has no downloader. A real user may be downloading
        # elsewhere under the shared limiter while this local build runs.
        await stop_running(directory, worker_shared=directory / "smoke-shared")
    assert not (directory / "tray-control.json").exists()
    print("Installed launcher, native tray readiness and shutdown passed")


if __name__ == "__main__":
    try:
        asyncio.run(check(Path(sys.argv[1])))
    except Exception:
        import traceback
        message = traceback.format_exc().replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")
        print("::error::" + message)
        raise
