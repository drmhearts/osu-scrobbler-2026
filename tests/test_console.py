import asyncio

from types import SimpleNamespace

from osu_scrobbler.activity import ActivityBuffer
from osu_scrobbler.console import ActivityFeed, show_console, view_command
from osu_scrobbler.ipc import CommandServer, send_command
from osu_scrobbler.tray import TrayApp


def test_activity_is_bounded_and_expires_without_creating_files(tmp_path):
    now = [0]
    buffer = ActivityBuffer(clock=lambda: now[0])
    for number in range(60):
        buffer.append(f"Song {number}")
    assert len(buffer.read()["events"]) == 50
    assert buffer.read()["events"][0] == "Song 10"
    now[0] = 300
    buffer.expire()
    assert not buffer.events
    buffer.append("x" * 5000)
    assert len(buffer.read()["events"][0]) == 4096
    buffer.clear()
    assert buffer.read()["events"] == []
    assert list(tmp_path.iterdir()) == []


async def test_activity_feed_uses_authenticated_local_ipc_and_handles_new_tray_session(tmp_path):
    app = TrayApp(tmp_path, SimpleNamespace(stop=lambda: None))
    server = CommandServer(tmp_path, 0, app.handle_command, control_name="tray-control.json")
    await server.start()
    try:
        feed = ActivityFeed(tmp_path)
        app.activity.append("Scrobbled: 夏川陽子 — 防人想歌")
        assert await feed.read() == ["Scrobbled: 夏川陽子 — 防人想歌"]
        assert await feed.read() == []
        app.activity = ActivityBuffer()
        app.activity.append("New tray session")
        assert await feed.read() == ["New tray session"]
        assert "error" in await send_command(tmp_path, {"command": "events", "after": -1}, control_name="tray-control.json")
        reader, writer = await asyncio.open_connection("127.0.0.1", server.port)
        writer.write(b'{"command":"events","token":"wrong"}\n')
        await writer.drain()
        assert b"Unauthorized" in await reader.readline()
        writer.close()
        await writer.wait_closed()
        assert not list(tmp_path.glob("*.log*"))
    finally:
        await server.close()
    assert await feed.read() == []
    assert not (tmp_path / "tray-control.json").exists()


async def test_console_controls_use_running_watcher_ipc_without_creating_another(tmp_path, unused_tcp_port):
    commands = []
    async def handler(request):
        commands.append(request)
        if request["command"] == "config" and "action" not in request:
            return {"config": {"auto_export": False}}
        return {"message": "ok"}
    server = CommandServer(tmp_path, unused_tcp_port, handler)
    await server.start()
    try:
        assert await view_command(tmp_path, "f") == {"message": "ok"}
        await view_command(tmp_path, "a")
        assert commands[0] == {"command": "force"}
        assert commands[-1] == {"command": "config", "action": "set", "key": "auto_export", "value": True}
        assert await view_command(tmp_path, "q") is None
    finally:
        await server.close()


async def test_hiding_terminal_leaves_watcher_running(tmp_path, unused_tcp_port):
    commands = []
    async def handler(request):
        commands.append(request)
        return {"active_seconds": 0, "phase": "menu"}
    server = CommandServer(tmp_path, unused_tcp_port, handler)
    await server.start()
    shown = asyncio.Event()
    def output(message):
        if "No selected track" in message:
            shown.set()
    task = asyncio.create_task(show_console(tmp_path, output=output))
    try:
        await asyncio.wait_for(shown.wait(), 1)
        (tmp_path / "console.exit").touch()
        await asyncio.wait_for(task, 1)
        assert commands == [{"command": "status"}]
        assert server.server.is_serving()
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await server.close()


async def test_export_and_auto_export_work_from_start_view_with_osu_closed(tmp_path):
    from osu_scrobbler.config import Config
    from osu_scrobbler.storage import Storage
    from osu_scrobbler.models import Submission, Track
    storage = Storage(tmp_path / 'history.db')
    storage.enqueue(Submission('export-id', 'attempt', Track('Song', 'Artist', 'Album', 'Artist'), 1700000000, 1700000000))
    storage.mark('export-id', 'accepted', submitted_at=1700000001)
    storage.close()
    assert (await view_command(tmp_path, 'e'))['exported'] == 1
    assert 'Song' in (tmp_path / 'scrobbles.txt').read_text('utf-8')
    assert (await view_command(tmp_path, 'a'))['value'] is True
    assert Config.load(tmp_path).auto_export is True
