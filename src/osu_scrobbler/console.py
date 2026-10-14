"""A detachable terminal client; closing it never stops the background watcher."""

from __future__ import annotations

import asyncio
import contextlib
import os
from pathlib import Path
import sys

from .config import WatchLock
from .display import render
from .ipc import send_command
from .welcome import welcome


class ActivityFeed:
    """Read recent messages over local IPC; never create a disk activity log."""

    def __init__(self, directory):
        self.directory = directory
        self.cursor = 0
        self.session = None

    async def read(self):
        try:
            response = await asyncio.wait_for(send_command(
                self.directory, {"command": "events", "after": self.cursor, "session": self.session},
                control_name="tray-control.json"), 1)
        except (RuntimeError, OSError, ValueError, asyncio.TimeoutError):
            return []
        self.cursor = response.get("cursor", 0)
        self.session = response.get("session")
        return response.get("events", [])


def focus_console():
    if sys.platform != "win32":
        return
    import ctypes
    from ctypes import wintypes
    kernel = ctypes.windll.kernel32
    user = ctypes.windll.user32
    kernel.GetConsoleWindow.restype = wintypes.HWND
    user.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
    user.SetForegroundWindow.argtypes = [wintypes.HWND]
    user.FindWindowW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR]
    user.FindWindowW.restype = wintypes.HWND
    # Windows Terminal may host the visible window while GetConsoleWindow only
    # identifies its hidden compatibility console.
    window = user.FindWindowW(None, "osu! scrobbler") or kernel.GetConsoleWindow()
    if window:
        user.ShowWindow(window, 9)  # SW_RESTORE
        user.SetForegroundWindow(window)


async def view_command(directory, key):
    if key == "a":
        reply = await send_command(directory, {"command": "config"})
        return await send_command(directory, {"command": "config", "action": "set", "key": "auto_export",
                                              "value": not reply["config"]["auto_export"]})
    command = {"f": "force", "i": "info", "v": "info", "e": "export", "s": "status"}.get(key)
    return await send_command(directory, {"command": command}) if command else None


async def show_console(directory: Path, *, output=print):
    directory = Path(directory)
    try:
        lock = WatchLock(directory, "console.lock")
        lock.__enter__()
    except RuntimeError:
        (directory / "console.focus").touch()
        return
    previous = None
    try:
        if sys.platform == "win32":
            import ctypes
            ctypes.windll.kernel32.SetConsoleTitleW("osu! scrobbler")
        output(welcome(directory, background=True))
        feed = ActivityFeed(directory)
        try:
            response = await asyncio.wait_for(send_command(directory, {"command": "status"}), 2)
            output(render(response))
        except (RuntimeError, OSError, ValueError, asyncio.TimeoutError):
            output("Waiting for osu! stable or lazer. The tray monitor starts watching when it opens.")
        for marker in ("console.focus", "console.exit"):
            with contextlib.suppress(FileNotFoundError):
                (directory / marker).unlink()
        interactive = sys.stdin is not None and sys.stdin.isatty()
        if interactive and os.name != "nt":
            import termios
            import tty
            previous = termios.tcgetattr(sys.stdin.fileno())
            tty.setcbreak(sys.stdin.fileno())
        while True:
            for line in await feed.read():
                output(line)
            exit_path = directory / "console.exit"
            if exit_path.exists():
                exit_path.unlink()
                return
            focus_path = directory / "console.focus"
            if focus_path.exists():
                focus_path.unlink()
                focus_console()
            key = ""
            if interactive:
                if os.name == "nt":
                    import msvcrt
                    if msvcrt.kbhit():
                        key = msvcrt.getwch().lower()
                else:
                    import select
                    if select.select([sys.stdin], [], [], 0)[0]:
                        key = sys.stdin.read(1).lower()
            if key == "q":
                return
            if key in "fivesa" and key:
                try:
                    response = await view_command(directory, key)
                    if response is not None:
                        output(render(response, full=key == "v"))
                except (RuntimeError, OSError, ValueError):
                    output("Watcher unavailable; waiting for osu! / tosu.")
            await asyncio.sleep(0.25)
    finally:
        if previous is not None:
            import termios
            termios.tcsetattr(sys.stdin.fileno(), termios.TCSADRAIN, previous)
        lock.__exit__(None, None, None)
