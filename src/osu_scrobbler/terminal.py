"""Trusted terminal styling and a small timer prompt."""

import asyncio
import os
import sys
from dataclasses import asdict

from .threshold import Threshold, parse_threshold
_vt_enabled = None


def color(value, code="95", *, enabled=None):
    if enabled is None:
        enabled = sys.stdout is not None and sys.stdout.isatty() and "NO_COLOR" not in os.environ and _vt_enabled is not False
    return f"\033[{code}m{value}\033[0m" if enabled else value


def enable_colors():
    global _vt_enabled
    if sys.platform == "win32":
        import ctypes
        from ctypes import wintypes
        kernel = ctypes.windll.kernel32
        kernel.GetStdHandle.restype = wintypes.HANDLE
        kernel.GetConsoleMode.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        kernel.SetConsoleMode.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        handle = kernel.GetStdHandle(-11)
        mode = wintypes.DWORD()
        if kernel.GetConsoleMode(handle, ctypes.byref(mode)):
            _vt_enabled = bool(kernel.SetConsoleMode(handle, mode.value | 4))
        else:
            _vt_enabled = False


def style_message(value):
    for prefix, code in (("Scrobbled", "92"), ("Selected", "96"), ("Timer:", "95"),
                         ("Startup:", "95"), ("Disconnected", "93"), ("Delivery failed", "91")):
        if value.startswith(prefix):
            return color(value, code)
    return value


async def threshold_prompt(directory, *, sender=None, reader=None, output=print):
    from .ipc import send_command
    reader = reader or input
    sender = sender or send_command
    while True:
        try:
            value = await asyncio.to_thread(reader, "Timer: h half / f full / seconds [Enter cancels]: ")
            setting = parse_threshold(value)
            if setting is None:
                return None
        except (EOFError, KeyboardInterrupt):
            return None
        except ValueError as error:
            output(str(error))
            continue
        try:
            return await sender(directory, {"command": "threshold", **asdict(setting)})
        except (OSError, RuntimeError, ValueError):
            setting.save(directory)
            return {"threshold": setting.label}
