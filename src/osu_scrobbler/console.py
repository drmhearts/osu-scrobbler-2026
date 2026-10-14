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
from .welcome import welcome, commands
from .terminal import enable_colors, style_message, threshold_prompt


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
    window = user.FindWindowW(None, "osu! life hacks") or kernel.GetConsoleWindow()
    if window:
        user.ShowWindow(window, 9)  # SW_RESTORE
        user.SetForegroundWindow(window)


async def view_command(directory, key):
    if key == "t":
        return await threshold_prompt(directory)
    if key == "b":
        from .tray import startup_installed, install_startup, remove_startup
        try:
            enabled = not startup_installed(directory)
            (install_startup if enabled else remove_startup)(directory)
        except (OSError, RuntimeError):
            return {"error": "Startup could not be changed."}
        return {"message": "Startup: " + ("on" if enabled else "off")}
    if key == "?":
        return {"message": commands(background=True)}
    if key == "r":
        from .cli import authorize
        import httpx
        try:
            return await authorize(directory)
        except (httpx.HTTPError, OSError, RuntimeError, ValueError):
            return {"error": "Last.fm connection failed. Try again."}
    if key == "x":
        await send_command(directory, {"command": "quit"}, control_name="tray-control.json")
        return None
    if key == "u":
        from .tray import python_executable
        import subprocess
        try:
            subprocess.Popen([python_executable(), "-m", "osu_scrobbler", "--data-dir", str(directory), "setup", "--update"],
                             creationflags=getattr(subprocess, "CREATE_NEW_CONSOLE", 0))
        except (OSError, RuntimeError):
            return {"error": "Update could not start."}
        return {"message": "Updating…"}
    if key == "a":
        try:
            reply = await send_command(directory, {"command": "config"})
            return await send_command(directory, {"command": "config", "action": "set", "key": "auto_export",
                                                  "value": not reply["config"]["auto_export"]})
        except (RuntimeError, OSError, ValueError):
            from .config import Config
            config = Config.load(directory)
            config.auto_export = not config.auto_export
            config.save(directory)
            return {"saved": "auto_export", "value": config.auto_export}
    if key == "e":
        try:
            return await send_command(directory, {"command": "export"})
        except (RuntimeError, OSError, ValueError):
            from .config import Config
            from .storage import Storage
            config = Config.load(directory)
            storage = Storage(directory / "history.db")
            try:
                destination = Path(config.export_path).expanduser() if config.export_path else directory / "scrobbles.txt"
                return {"exported": storage.export(destination), "destination": str(destination)}
            except (OSError, ValueError):
                return {"error": "Export failed. History is preserved."}
            finally:
                storage.close()
    command = {"f": "force", "i": "info", "v": "info", "e": "export", "s": "status"}.get(key)
    return await send_command(directory, {"command": command}) if command else None


async def background_status(directory):
    from .tray import tray_health
    try:
        response = await tray_health(directory)
        return "Background: " + str(response.get("status") or "Starting watcher")
    except (RuntimeError, OSError, ValueError, asyncio.TimeoutError):
        return "Background app is not running. Close this window and double-click START.cmd."


async def show_console(directory: Path, *, output=print):
    directory = Path(directory)
    emit = output
    output = lambda message: emit(style_message(message))
    try:
        lock = WatchLock(directory, "console.lock")
        lock.__enter__()
    except RuntimeError:
        (directory / "console.focus").touch()
        return
    previous = None
    try:
        enable_colors()
        if sys.platform == "win32":
            import ctypes
            ctypes.windll.kernel32.SetConsoleTitleW("osu! life hacks")
        output(welcome(directory, background=True))
        feed = ActivityFeed(directory)
        try:
            response = await asyncio.wait_for(send_command(directory, {"command": "status"}), 2)
            output(render(response))
        except (RuntimeError, OSError, ValueError, asyncio.TimeoutError):
            output(await background_status(directory))
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
            if key in "fivesatbu?xr" and key:
                try:
                    if key in {"t", "r"} and previous is not None:
                        termios.tcsetattr(sys.stdin.fileno(), termios.TCSADRAIN, previous)
                    try:
                        response = await view_command(directory, key)
                    finally:
                        if key in {"t", "r"} and previous is not None:
                            tty.setcbreak(sys.stdin.fileno())
                    if response is not None:
                        output(render(response, full=key == "v"))
                except (RuntimeError, OSError, ValueError):
                    output(await background_status(directory))
            await asyncio.sleep(0.25)
    finally:
        if previous is not None:
            import termios
            termios.tcsetattr(sys.stdin.fileno(), termios.TCSADRAIN, previous)
        lock.__exit__(None, None, None)
