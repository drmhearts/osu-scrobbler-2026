from types import SimpleNamespace

import pytest

from osu_scrobbler.console import background_status
from osu_scrobbler.ipc import CommandServer
from osu_scrobbler.tray import TrayApp, wait_for_tray


async def test_readiness_requires_visible_icon_and_real_local_monitor(tmp_path):
    icon = SimpleNamespace(visible=True)
    app = TrayApp(tmp_path, icon)
    app.supervisor.status = "Watching osu!"
    server = CommandServer(tmp_path, 0, app.handle_command, control_name="tray-control.json")
    await server.start()
    try:
        assert (await wait_for_tray(tmp_path))["ready"] is True
        assert await background_status(tmp_path) == "Background: Watching osu!"
        icon.visible = False
        with pytest.raises(RuntimeError, match="did not become ready"):
            await wait_for_tray(tmp_path, SimpleNamespace(poll=lambda: 1))
    finally:
        await server.close()
    assert "not running" in await background_status(tmp_path)


def test_monitor_start_failure_reaches_launcher_without_showing_icon(tmp_path, monkeypatch):
    import osu_scrobbler.tray as module
    class Server:
        def __init__(self, *args, **kwargs):
            pass
        async def start(self):
            raise OSError("port unavailable")
        async def close(self):
            pass
    monkeypatch.setattr(module, "CommandServer", Server)
    app = TrayApp(tmp_path, SimpleNamespace(run=lambda: pytest.fail("Failed monitor must not show icon")))
    with pytest.raises(RuntimeError, match="Background monitor could not start.*OSError"):
        app.run()
    assert not app.thread.is_alive()


async def test_failed_updated_tray_is_terminated_and_reported(tmp_path, monkeypatch):
    import osu_scrobbler.updater as updater
    import osu_scrobbler.tray as tray
    events = []
    class Process:
        returncode = None
        def terminate(self):
            events.append("terminate")
        async def wait(self):
            self.returncode = 1
            events.append("wait")
            return 1
    async def spawn(*args, **kwargs):
        return Process()
    async def failed(*args, **kwargs):
        raise RuntimeError("no icon")
    monkeypatch.setattr(updater.asyncio, "create_subprocess_exec", spawn)
    monkeypatch.setattr(tray, "wait_for_tray", failed)
    with pytest.raises(updater.UpdateError, match="tray failed to start"):
        await updater.handoff(tmp_path / "env", tmp_path, "tray")
    assert events == ["terminate", "wait"]
