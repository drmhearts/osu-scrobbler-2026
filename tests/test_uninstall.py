"""Uninstall only app-owned runtime/startup files using isolated profiles."""

import hashlib
import json

import pytest

from osu_scrobbler import uninstall as module
from osu_scrobbler.cli import parser


@pytest.mark.asyncio
async def test_uninstall_keeps_user_data_and_map_library(tmp_path, monkeypatch):
    profile = tmp_path / "profile"
    updates = profile / "updates/versions/0.9.3-fixture/runtime"
    updates.mkdir(parents=True)
    (updates / "python.exe").write_bytes(b"cached runtime")
    (profile / "history.db").write_bytes(b"history")
    (profile / "download-state.json").write_text("pending maps")
    library = tmp_path / "osu!/Songs"
    library.mkdir(parents=True)
    (library / "map.osz").write_bytes(b"map")
    launcher = profile / "START.cmd"
    launcher.write_bytes(b"owned launcher")
    (profile / "launch.ps1").write_bytes(b"user-edited launcher")
    (profile / "local-launcher-state.json").write_text(json.dumps({
        "START.cmd": hashlib.sha256(launcher.read_bytes()).hexdigest(),
        "launch.ps1": hashlib.sha256(b"original launcher").hexdigest(),
    }))
    calls = []

    async def stop(directory):
        calls.append(("stop", directory))

    monkeypatch.setattr(module.sys, "platform", "win32")
    monkeypatch.setattr("osu_scrobbler.updater.stop_running", stop)
    monkeypatch.setattr("osu_scrobbler.tray.remove_startup", lambda directory: calls.append(("startup", directory)))
    response = await module.uninstall(profile)
    assert "removed" in response["message"]
    assert [name for name, _ in calls] == ["stop", "startup"]
    assert not (profile / "updates").exists()
    assert not launcher.exists()
    assert (profile / "launch.ps1").read_bytes() == b"user-edited launcher"
    assert (profile / "history.db").read_bytes() == b"history"
    assert (profile / "download-state.json").read_text() == "pending maps"
    assert (library / "map.osz").read_bytes() == b"map"
    assert parser().parse_args(["uninstall"]).command == "uninstall"


@pytest.mark.asyncio
async def test_uninstall_never_removes_cache_if_stop_fails(tmp_path, monkeypatch):
    profile = tmp_path / "profile"
    (profile / "updates").mkdir(parents=True)

    async def stop(_):
        raise RuntimeError("still running")

    monkeypatch.setattr(module.sys, "platform", "win32")
    monkeypatch.setattr("osu_scrobbler.updater.stop_running", stop)
    monkeypatch.setattr("osu_scrobbler.tray.remove_startup", lambda _: pytest.fail("startup changed"))
    with pytest.raises(RuntimeError, match="still running"):
        await module.uninstall(profile)
    assert (profile / "updates").exists()
