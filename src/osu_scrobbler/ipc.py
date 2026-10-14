"""Authenticated local commands; only the watcher owns playback and delivery."""

import asyncio
import hmac
import json
import os
import secrets
from pathlib import Path
from typing import Awaitable, Callable

MAX_REQUEST = 16_384


class CommandServer:
    def __init__(self, directory: Path, port: int, handler: Callable[[dict], Awaitable[dict]], *, control_name="control.json"):
        self.directory = directory
        self.port = port
        self.handler = handler
        self.token = secrets.token_urlsafe(32)
        self.server = None
        self.control_name = control_name

    async def start(self):
        self.server = await asyncio.start_server(self._handle, "127.0.0.1", self.port, limit=MAX_REQUEST)
        self.port = self.server.sockets[0].getsockname()[1]
        self.directory.mkdir(parents=True, exist_ok=True)
        path = self.directory / self.control_name
        path.write_text(json.dumps({"port": self.port, "token": self.token}), "utf-8")
        if os.name != "nt":
            path.chmod(0o600)

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        try:
            line = await asyncio.wait_for(reader.readline(), timeout=5)
            request = json.loads(line)
            if not isinstance(request, dict) or not hmac.compare_digest(str(request.pop("token", "")), self.token):
                result = {"error": "Unauthorized local command"}
            else:
                result = await self.handler(request)
        except (ValueError, asyncio.TimeoutError):
            result = {"error": "Invalid local command"}
        except Exception:
            # Never expose arbitrary third-party exception text or credential-bearing URLs.
            result = {"error": "Command failed; inspect watcher status"}
        try:
            writer.write((json.dumps(result, ensure_ascii=False) + "\n").encode("utf-8"))
            await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()

    async def close(self):
        if self.server:
            self.server.close()
            await self.server.wait_closed()
        path = self.directory / self.control_name
        if path.exists():
            path.unlink()


async def send_command(directory: Path, request: dict, *, control_name="control.json") -> dict:
    path = directory / control_name
    if not path.exists():
        raise RuntimeError("No watcher is running. Start osu-life-hacks watch first.")
    control = json.loads(path.read_text("utf-8"))
    try:
        reader, writer = await asyncio.wait_for(asyncio.open_connection("127.0.0.1", control["port"], limit=4 * 1024 * 1024), 5)
    except OSError as exc:
        raise RuntimeError("Watcher is unavailable. Restart osu-life-hacks watch.") from exc
    try:
        request = {**request, "token": control["token"]}
        writer.write((json.dumps(request, ensure_ascii=False) + "\n").encode("utf-8"))
        await writer.drain()
        return json.loads(await asyncio.wait_for(reader.readline(), 60))
    finally:
        writer.close()
        await writer.wait_closed()
