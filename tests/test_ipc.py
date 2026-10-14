import asyncio
import json

from osu_scrobbler.ipc import CommandServer, send_command


async def test_second_process_command_uses_watcher_state(tmp_path, unused_tcp_port):
    received = []
    async def handler(request):
        received.append(request)
        return {"selected": "防人想歌", "attempt": "one-owner"}
    server = CommandServer(tmp_path, unused_tcp_port, handler)
    await server.start()
    try:
        answer = await send_command(tmp_path, {"command": "status"})
        assert answer == {"selected": "防人想歌", "attempt": "one-owner"}
        assert received == [{"command": "status"}]
    finally:
        await server.close()
    assert not (tmp_path / "control.json").exists()


async def test_local_commands_require_private_capability(tmp_path, unused_tcp_port):
    received = []
    async def handler(request):
        received.append(request)
        return {}
    server = CommandServer(tmp_path, unused_tcp_port, handler)
    await server.start()
    try:
        reader, writer = await asyncio.open_connection("127.0.0.1", unused_tcp_port)
        writer.write(b'{"command":"force","token":"wrong"}\n')
        await writer.drain()
        assert json.loads(await reader.readline()) == {"error": "Unauthorized local command"}
        writer.close()
        await writer.wait_closed()
        assert not received
    finally:
        await server.close()
