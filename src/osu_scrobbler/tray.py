"""Windows notification icon, user startup, and one background watcher."""

from __future__ import annotations

import asyncio
import hashlib
import importlib.util
import os
from pathlib import Path
import subprocess
import sys
import threading
import time

from .activity import ActivityBuffer
from .config import Config, WatchLock, default_data_dir
from .ipc import CommandServer, send_command

GAME_PROCESSES = {"osu!.exe", "osu.exe", "osulazer.exe"}
RELEVANT_PROCESSES = GAME_PROCESSES | {"tosu.exe"}
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"


def require_windows():
    if sys.platform != "win32":
        raise RuntimeError("The tray launcher is available on Windows. Use watch on other systems.")


def check_dependencies():
    if any(importlib.util.find_spec(package) is None for package in ("pystray", "PIL", "psutil")):
        raise RuntimeError("Install the updated dependencies first: python -m pip install -e .")


def python_executable(*, hidden=False):
    executable = Path(sys.executable)
    desired = executable.with_name("pythonw.exe" if hidden else "python.exe")
    if not desired.is_file():
        raise RuntimeError("Use the project virtual environment; its python.exe and pythonw.exe are required.")
    return str(desired)


def tray_command(directory: Path, *, dry_run=False, executable=None, skip_update_check=False):
    command = [executable or python_executable(hidden=True), "-m", "osu_scrobbler"]
    if directory.resolve() != default_data_dir().resolve():
        command += ["--data-dir", str(directory.resolve())]
    if skip_update_check:
        command.append("--skip-update-check")
    command.append("tray")
    if dry_run:
        command.append("--dry-run")
    return command


def startup_name(directory):
    identity = str(Path(directory).resolve()).casefold().encode("utf-8")
    return "osu-scrobbler-" + hashlib.sha256(identity).hexdigest()[:12]


def install_startup(directory: Path, *, dry_run=False, registry=None, executable=None):
    """Register only our profile's HKCU Run value; no administrator rights."""
    if registry is None:
        require_windows()
        check_dependencies()
        import winreg as registry
    command = subprocess.list2cmdline(tray_command(directory, dry_run=dry_run, executable=executable))
    if len(command) > 260:
        raise RuntimeError("Windows startup command is too long; use a shorter project or data-directory path.")
    with registry.CreateKey(registry.HKEY_CURRENT_USER, RUN_KEY) as key:
        registry.SetValueEx(key, startup_name(directory), 0, registry.REG_SZ, command)
    return command


def remove_startup(directory: Path, *, registry=None):
    if registry is None:
        require_windows()
        import winreg as registry
    try:
        with registry.OpenKey(registry.HKEY_CURRENT_USER, RUN_KEY, 0, registry.KEY_SET_VALUE) as key:
            registry.DeleteValue(key, startup_name(directory))
    except FileNotFoundError:
        pass


def startup_installed(directory: Path):
    require_windows()
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
            value, _ = winreg.QueryValueEx(key, startup_name(directory))
            return isinstance(value, str) and bool(value)
    except FileNotFoundError:
        return False


def launch_tray(directory: Path, *, dry_run=False, skip_update_check=False):
    require_windows()
    return subprocess.Popen(tray_command(directory, dry_run=dry_run, skip_update_check=skip_update_check),
                            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                            creationflags=subprocess.CREATE_NO_WINDOW)


def process_names():
    import psutil
    names = set()
    try:
        for process in psutil.process_iter(["name"]):
            try:
                name = process.info.get("name")
                if name and name.casefold() in RELEVANT_PROCESSES:
                    names.add(name.casefold())
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
    finally:
        psutil.process_iter.cache_clear()  # Discard cached objects/names of unrelated processes.
    return names


def find_tosu(config: Config, *, home=None):
    if config.tosu_path:
        candidate = Path(config.tosu_path).expanduser()
        return candidate if candidate.is_file() else None
    home = Path(home) if home is not None else Path.home()
    for candidate in (home / "Desktop/tosu/tosu.exe", home / "Downloads/tosu/tosu.exe"):
        if candidate.is_file():
            return candidate
    return None


def start_tosu(path: Path):
    return subprocess.Popen([str(path.resolve()), "--update=false"], cwd=str(path.resolve().parent),
                            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))


async def watcher_available(directory):
    try:
        response = await asyncio.wait_for(send_command(directory, {"command": "status"}), 1)
        return "active_seconds" in response
    except (OSError, ValueError, RuntimeError, asyncio.TimeoutError):
        return False


async def tray_health(directory):
    return await asyncio.wait_for(send_command(
        directory, {"command": "health"}, control_name="tray-control.json"), 1)


async def wait_for_tray(directory, process=None, *, timeout=15):
    """A spawned process is not success until its icon and monitor are ready."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            response = await tray_health(directory)
            if response.get("ready") is True:
                return response
        except (RuntimeError, OSError, ValueError, asyncio.TimeoutError):
            pass
        code = process.poll() if process is not None and hasattr(process, "poll") else getattr(process, "returncode", None)
        if code is not None:
            break
        await asyncio.sleep(0.25)
    raise RuntimeError("Background tray did not become ready. Setup has stopped; restart START.cmd to retry.")


class Supervisor:
    """Lifecycle is independent of the native icon so it can be fixture-tested."""

    def __init__(self, directory, *, dry_run=False, output=print, watcher_factory=None,
                 probe=watcher_available, tosu_locator=None, tosu_starter=start_tosu):
        self.directory = Path(directory)
        self.dry_run = dry_run
        self.output = output
        self.watcher_factory = watcher_factory
        self.probe = probe
        self.tosu_locator = tosu_locator
        self.tosu_starter = tosu_starter
        self.watcher = None
        self.task = None
        self.tosu = None
        self.stop = asyncio.Event()
        self.status = "Waiting for osu!"
        self.retry_at = 0
        self.tosu_retry_at = 0
        self.last_notice = None

    def notice(self, message):
        if message != self.last_notice:
            self.output(message)
            self.last_notice = message

    async def _stop_watcher(self):
        if self.watcher is not None:
            self.watcher.stop.set()
        if self.task is not None:
            try:
                await asyncio.wait_for(asyncio.shield(self.task), 5)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                self.task.cancel()
                await asyncio.gather(self.task, return_exceptions=True)
            except Exception:
                pass
        self.task = None
        self.watcher = None

    async def _close_failed_watcher(self):
        # Startup can fail before Watcher.run reaches its service cleanup block
        # (for example, a foreground watcher wins the lock race).
        if self.watcher is None:
            return
        for name in ("musicbrainz", "lastfm"):
            resource = getattr(self.watcher, name, None)
            close = getattr(resource, "aclose", None)
            if close:
                try:
                    await close()
                except Exception:
                    pass
        storage = getattr(self.watcher, "storage", None)
        if storage:
            storage.close()

    def _stop_tosu(self):
        # Only the process launched by this supervisor belongs to us.
        if self.tosu is not None and self.tosu.poll() is None:
            self.tosu.terminate()
        self.tosu = None

    async def tick(self, names, now=None):
        now = time.monotonic() if now is None else now
        names = {name.casefold() for name in names}
        if not names & GAME_PROCESSES:
            await self._stop_watcher()
            self._stop_tosu()
            self.status = "Waiting for osu!"
            self.notice("Waiting for osu! stable or lazer.")
            return
        config = Config.load(self.directory)
        if self.tosu is not None and self.tosu.poll() is not None:
            self.tosu = None
        if "tosu.exe" not in names and self.tosu is None and now >= self.tosu_retry_at:
            if self.tosu_locator is None:
                from .tosu import ensure_tosu
                try:
                    path = await ensure_tosu(self.directory)
                except (OSError, ValueError, RuntimeError, asyncio.TimeoutError):
                    path = None
            else:
                path = self.tosu_locator(config)
            if path:
                try:
                    self.tosu = self.tosu_starter(path)
                except OSError:
                    self.notice("Could not start tosu; check config tosu_path.")
            else:
                self.notice("tosu not found; automatic setup will retry.")
            self.tosu_retry_at = now + 30
        if self.task is not None and self.task.done():
            try:
                self.task.result()
            except Exception:
                self.notice("Watcher stopped unexpectedly; retrying shortly.")
                await self._close_failed_watcher()
            self.task = None
            self.watcher = None
            self.retry_at = now + 10
        if self.task is not None:
            self.status = "Watching osu!"
            return
        if await self.probe(self.directory):
            self.status = "Using running watcher"
            return
        if now < self.retry_at:
            self.status = "Restarting watcher"
            return
        if self.watcher_factory is None:
            from .watcher import Watcher
            factory = Watcher
        else:
            factory = self.watcher_factory
        self.watcher = factory(self.directory, config, dry_run=self.dry_run, output=self.output, keyboard=False)
        self.task = asyncio.create_task(self.watcher.run())
        self.status = "Watching osu!"

    async def run(self, get_processes=process_names, on_status=None):
        try:
            while not self.stop.is_set():
                try:
                    await self.tick(get_processes())
                except Exception:
                    self.status = "Setup needs attention"
                    self.notice("Background setup failed; check settings and restart the tray launcher.")
                if on_status:
                    on_status(self.status)
                try:
                    await asyncio.wait_for(self.stop.wait(), 1)
                except asyncio.TimeoutError:
                    pass
        finally:
            await self._stop_watcher()
            self._stop_tosu()


class TrayApp:
    def __init__(self, directory, icon, *, dry_run=False):
        self.directory = Path(directory)
        self.icon = icon
        self.activity = ActivityBuffer()
        self.supervisor = Supervisor(self.directory, dry_run=dry_run, output=self.activity.append)
        self.loop = None
        self.thread = None
        self.console = None
        self.ready = threading.Event()
        self.start_error = None

    async def handle_command(self, request):
        if request.get("command") == "health":
            from . import __version__
            return {"ready": bool(getattr(self.icon, "visible", False)), "status": self.supervisor.status,
                    "version": __version__}
        if request.get("command") == "events":
            after = request.get("after", 0)
            if type(after) is not int or after < 0:
                return {"error": "Invalid activity cursor"}
            if request.get("session") != self.activity.session:
                after = 0
            return {**self.activity.read(after), "status": self.supervisor.status}
        if request.get("command") == "quit":
            self.quit()
            return {"message": "Background monitor stopped."}
        return {"error": "Unknown tray command"}

    def open_console(self, *args):
        if self.console is not None and self.console.poll() is None:
            import ctypes
            ctypes.windll.user32.AllowSetForegroundWindow(self.console.pid)
            # The console owns its HWND and offers a focus request via a marker.
            (self.directory / "console.focus").touch()
            return
        self.console = subprocess.Popen([python_executable(), "-m", "osu_scrobbler", "--data-dir", str(self.directory.resolve()), "console"],
                                        creationflags=subprocess.CREATE_NEW_CONSOLE)

    def quit(self, *args):
        # First-run setup can own the view without being this icon's child.
        (self.directory / "console.exit").touch()
        if self.loop is not None and not self.loop.is_closed():
            self.loop.call_soon_threadsafe(self.supervisor.stop.set)
        self.icon.stop()

    def run(self):
        def update_status(state):
            self.activity.expire()
            self.icon.title = f"osu! scrobbler — {state}"

        def background():
            async def monitor():
                self.loop = asyncio.get_running_loop()
                server = CommandServer(self.directory, 0, self.handle_command, control_name="tray-control.json")
                try:
                    await server.start()
                    self.ready.set()
                    if getattr(self, "replace_legacy", False):
                        asyncio.create_task(self._replace_legacy_view())
                    await self.supervisor.run(on_status=update_status)
                finally:
                    await server.close()
                    self.activity.clear()
            try:
                asyncio.run(monitor())
            except Exception as error:
                self.start_error = error
                self.ready.set()
        self.thread = threading.Thread(target=background, name="osu-scrobbler-monitor")
        self.thread.start()
        if not self.ready.wait(10) or self.start_error is not None:
            if self.loop is not None and not self.loop.is_closed():
                self.loop.call_soon_threadsafe(self.supervisor.stop.set)
            self.thread.join(timeout=2)
            kind = type(self.start_error).__name__ if self.start_error else "timeout"
            raise RuntimeError(f"Background monitor could not start ({kind}). Restart START.cmd; check local port/settings if it persists.") from self.start_error
        try:
            self.icon.run()
        finally:
            if self.loop is not None and not self.loop.is_closed():
                self.loop.call_soon_threadsafe(self.supervisor.stop.set)
            self.thread.join(timeout=10)

    async def _replace_legacy_view(self):
        # Older START launches can leave their source-version console open.
        await asyncio.sleep(3)
        try:
            with WatchLock(self.directory, "console.lock"):
                return  # Sign-in startup stays hidden.
        except RuntimeError:
            (self.directory / "console.exit").touch()
        for _ in range(20):
            await asyncio.sleep(0.25)
            try:
                with WatchLock(self.directory, "console.lock"):
                    pass
            except RuntimeError:
                continue
            self.open_console()
            return


def run_tray(directory, *, dry_run=False):
    require_windows()
    check_dependencies()
    if not dry_run and startup_installed(directory):
        install_startup(directory)  # Keep the sign-in target on the installed runtime.
    if (Path(sys.prefix) / "portable.json").is_file():
        from .portable import install_launcher
        install_launcher(Path(directory))
    import pystray
    from PIL import Image, ImageDraw
    try:
        lock = WatchLock(Path(directory), "tray.lock")
        lock.__enter__()
    except RuntimeError:
        return  # A sign-in/startup invocation must not spawn another watcher.
    try:
        image = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
        draw = ImageDraw.Draw(image)
        draw.ellipse((3, 3, 61, 61), fill=(235, 80, 140), outline="white", width=3)
        draw.ellipse((20, 20, 44, 44), fill="white")
        icon = pystray.Icon("osu-scrobbler", image, "osu! scrobbler — Waiting for osu!")
        app = TrayApp(directory, icon, dry_run=dry_run)
        from .bootstrap import repair_launcher
        app.replace_legacy = repair_launcher(directory)
        icon.menu = pystray.Menu(pystray.MenuItem("Show terminal", app.open_console, default=True),
                                pystray.MenuItem("Exit", app.quit))
        app.run()
    finally:
        lock.__exit__(None, None, None)
