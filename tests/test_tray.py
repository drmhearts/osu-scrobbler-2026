import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from osu_scrobbler.config import Config
from osu_scrobbler.tray import Supervisor, install_startup, remove_startup, startup_name, tray_command


class FakeRegistry:
    HKEY_CURRENT_USER = "user"
    REG_SZ = 1
    KEY_SET_VALUE = 2

    def __init__(self):
        self.values = {"Other app": "keep"}

    def CreateKey(self, hive, key):
        assert hive == self.HKEY_CURRENT_USER
        return self

    def OpenKey(self, hive, key, reserved, access):
        assert hive == self.HKEY_CURRENT_USER
        return self

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def SetValueEx(self, key, name, reserved, kind, value):
        self.values[name] = value

    def DeleteValue(self, key, name):
        if name not in self.values:
            raise FileNotFoundError(name)
        del self.values[name]


def test_startup_is_hidden_user_scoped_and_only_removes_its_own_value(tmp_path):
    registry = FakeRegistry()
    profile = tmp_path / "Data profile"
    command = install_startup(profile, registry=registry, executable=r"C:\Program Files\Python\pythonw.exe")
    assert command.startswith('"C:\\Program Files\\Python\\pythonw.exe"')
    assert "--install" not in command and command.endswith("tray")
    assert f'"{profile.resolve()}"' in command
    assert registry.values[startup_name(profile)] == command
    remove_startup(profile, registry=registry)
    remove_startup(profile, registry=registry)
    assert registry.values == {"Other app": "keep"}


def test_startup_profiles_and_dryrun_do_not_collide(tmp_path):
    assert startup_name(tmp_path / "a") != startup_name(tmp_path / "b")
    command = tray_command(tmp_path, dry_run=True, executable="pythonw.exe")
    assert command[-2:] == ["tray", "--dry-run"]


def test_default_profile_omits_long_data_path_from_windows_startup(tmp_path, monkeypatch):
    import osu_scrobbler.tray as tray_module
    monkeypatch.setattr(tray_module, "default_data_dir", lambda: tmp_path)
    command = tray_command(tmp_path, executable="pythonw.exe")
    assert command == ["pythonw.exe", "-m", "osu_scrobbler", "tray"]


def test_overlong_startup_command_does_not_write_registry(tmp_path):
    registry = FakeRegistry()
    with pytest.raises(RuntimeError, match="too long"):
        install_startup(tmp_path, registry=registry, executable="C:\\" + "x" * 261 + "\\pythonw.exe")
    assert registry.values == {"Other app": "keep"}


class FakeWatcher:
    def __init__(self, directory, config, **options):
        self.stop = asyncio.Event()
        self.starts = 0
        self.stops = 0
        self.options = options

    async def run(self):
        self.starts += 1
        await self.stop.wait()
        self.stops += 1


async def absent(directory):
    return False


@pytest.mark.parametrize("game", ["osu!.exe", "osu.exe", "osulazer.exe", "OSU!.EXE"])
async def test_game_launch_starts_one_watcher_and_close_stops_it(tmp_path, game):
    watchers = []
    def factory(*args, **kwargs):
        watcher = FakeWatcher(*args, **kwargs)
        watchers.append(watcher)
        return watcher
    supervisor = Supervisor(tmp_path, dry_run=True, watcher_factory=factory, probe=absent, output=lambda _: None)
    await supervisor.tick(set(), 0)
    assert not watchers
    await supervisor.tick({game}, 1)
    await asyncio.sleep(0)
    await supervisor.tick({game}, 2)
    assert len(watchers) == 1 and watchers[0].starts == 1
    assert watchers[0].options["keyboard"] is False
    assert watchers[0].options["dry_run"] is True
    await supervisor.tick(set(), 3)
    assert watchers[0].stops == 1
    assert supervisor.task is None and supervisor.status == "Waiting for osu!"
    await supervisor.tick({game}, 4)
    await asyncio.sleep(0)
    assert len(watchers) == 2
    await supervisor._stop_watcher()


async def test_manual_watcher_is_never_stopped_or_duplicated(tmp_path):
    async def present(directory):
        return True
    def forbidden(*args, **kwargs):
        raise AssertionError("Existing processes must be reused")
    supervisor = Supervisor(tmp_path, probe=present, watcher_factory=forbidden, output=lambda _: None)
    await supervisor.tick({"osu!.exe"}, 0)
    assert supervisor.status == "Using running watcher"
    await supervisor.tick(set(), 1)
    assert supervisor.watcher is None


async def test_supervisor_shutdown_waits_for_watcher_cleanup(tmp_path):
    supervisor = Supervisor(tmp_path, watcher_factory=FakeWatcher, probe=absent, output=lambda _: None)
    statuses = []
    def ready(status):
        statuses.append(status)
        supervisor.stop.set()
    await supervisor.run(get_processes=lambda: {"osu!.exe"}, on_status=ready)
    assert statuses == ["Watching osu!"]
    assert supervisor.task is None and supervisor.watcher is None


async def test_failed_watcher_is_closed_and_restart_is_backed_off(tmp_path):
    watchers = []
    class FailingWatcher(FakeWatcher):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.closes = 0
            self.storage = SimpleNamespace(close=lambda: setattr(self, "closes", self.closes + 1))
        async def run(self):
            raise RuntimeError("startup failed")
    def factory(*args, **kwargs):
        watcher = FailingWatcher(*args, **kwargs)
        watchers.append(watcher)
        return watcher
    supervisor = Supervisor(tmp_path, watcher_factory=factory, probe=absent, output=lambda _: None)
    await supervisor.tick({"osu!.exe"}, 0)
    await asyncio.sleep(0)
    await supervisor.tick({"osu!.exe"}, 1)
    assert watchers[0].closes == 1
    assert supervisor.status == "Restarting watcher"
    await supervisor.tick({"osu!.exe"}, 5)
    assert len(watchers) == 1
    await supervisor.tick({"osu!.exe"}, 11)
    assert len(watchers) == 2
    await supervisor._stop_watcher()


def test_tray_click_opens_one_terminal_then_focuses_it_and_exit_requests_hide(tmp_path, monkeypatch):
    import ctypes
    import osu_scrobbler.tray as tray_module
    calls = []
    focus = []
    child = SimpleNamespace(pid=123, poll=lambda: None)
    monkeypatch.setattr(tray_module, "python_executable", lambda: "python.exe")
    monkeypatch.setattr(tray_module.subprocess, "CREATE_NEW_CONSOLE", 16, raising=False)
    monkeypatch.setattr(tray_module.subprocess, "Popen", lambda command, **kwargs: calls.append((command, kwargs)) or child)
    monkeypatch.setattr(ctypes, "windll", SimpleNamespace(user32=SimpleNamespace(AllowSetForegroundWindow=focus.append)), raising=False)
    stops = []
    app = tray_module.TrayApp(tmp_path, SimpleNamespace(stop=lambda: stops.append(True)))
    try:
        app.open_console()
        app.open_console()
        assert len(calls) == 1
        assert calls[0][0][-1] == "console" and calls[0][1]["creationflags"] == 16
        assert focus == [123]
        assert (tmp_path / "console.focus").exists()
        app.quit()
        assert (tmp_path / "console.exit").exists()
        assert stops == [True]
    finally:
        app.activity.clear()


def test_process_probe_reads_only_names_and_discards_unrelated_applications(monkeypatch):
    import sys
    import osu_scrobbler.tray as tray_module
    seen = []
    def processes(fields):
        seen.append(fields)
        return [SimpleNamespace(info={"name": name}) for name in
                ("Browser.exe", "PrivateWork.exe", "OSU!.exe", "osuLazer.exe", None)]
    cleared = []
    processes.cache_clear = lambda: cleared.append(True)
    monkeypatch.setitem(sys.modules, "psutil", SimpleNamespace(process_iter=processes, NoSuchProcess=LookupError, AccessDenied=PermissionError))
    assert tray_module.process_names() == {"osu!.exe", "osulazer.exe"}
    assert seen == [["name"]]
    assert cleared == [True]
