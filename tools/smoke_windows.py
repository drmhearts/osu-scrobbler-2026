"""Check the actual installed launcher, native tray and clean shutdown in CI."""

import asyncio
import os
from pathlib import Path
import subprocess
import sys

from osu_scrobbler.ipc import send_command
from osu_scrobbler.tray import wait_for_tray
from osu_scrobbler.updater import stop_running


async def check(directory):
    powershell = Path(os.environ["SystemRoot"]) / "System32/WindowsPowerShell/v1.0/powershell.exe"
    process = await asyncio.create_subprocess_exec(str(powershell), "-NoProfile", "-NonInteractive",
        "-WindowStyle", "Hidden", "-ExecutionPolicy", "Bypass", "-File", str(directory / "launch.ps1"),
        "-Background", "-SkipUpdateCheck", stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=subprocess.CREATE_NO_WINDOW)
    if await process.wait():
        raise RuntimeError("Installed launch script failed")
    try:
        health = await wait_for_tray(directory, timeout=20)
        assert health["ready"] is True and health["status"] == "Waiting for osu!"
        # No first-time authorization, osu!, tosu, or Last.fm contact is needed
        # to verify the actual Windows notification icon and local IPC.
        response = await send_command(directory, {"command": "quit"}, control_name="tray-control.json")
        assert "stopped" in response["message"]
    finally:
        await stop_running(directory)
    assert not (directory / "tray-control.json").exists()
    print("Installed launcher, native tray readiness and shutdown passed")


if __name__ == "__main__":
    asyncio.run(check(Path(sys.argv[1])))
