"""Firefox bridge: browser-session requests, never browser cookie access.

The add-on polls a loopback-only, bearer-protected server while a maps worker is
active. Only one task is offered at a time. Archive bytes remain in Firefox's
normal downloads directory until this module copies them into app staging.
"""

from __future__ import annotations

import asyncio
import hmac
import json
from pathlib import Path
import secrets
from urllib.parse import parse_qs, urlsplit

from ..lastfm import CredentialStore
from .limiter import retry_seconds
from .models import AuthRequired, DownloadError, RateLimited, TransientError

PORT = 27895
MAX_BODY = 8 * 1024 * 1024


class PairingStore(CredentialStore):
    def __init__(self, *, backend=None):
        super().__init__(service="osu-life-hacks-firefox-extension", username="pairing", backend=backend)

    def token(self, *, create=False, rotate=False):
        backend = self._secure_backend()
        token = None if rotate else backend.get_password(self.service, self.username)
        if token is None and create:
            token = secrets.token_urlsafe(24)
            backend.set_password(self.service, self.username, token)
        return token


class ExtensionSource:
    def __init__(self, directory, *, token=None, port=PORT, timeout=25):
        self.directory = Path(directory)
        self.token = token
        self.port = port
        self.timeout = timeout
        self.server = None
        self.pending = None
        self.result = None

    async def __aenter__(self):
        self.token = self.token or PairingStore().token()
        if not self.token:
            raise AuthRequired("Pair the Firefox extension with maps → p first; no browser was launched.")
        try:
            self.server = await asyncio.start_server(self._handle, "127.0.0.1", self.port)
        except OSError:
            raise DownloadError("Firefox extension bridge port is in use. Close the other maps job and resume.") from None
        return self

    async def __aexit__(self, *_):
        if self.server:
            self.server.close()
            await self.server.wait_closed()

    async def _handle(self, reader, writer):
        status, payload, origin = 400, {}, ""
        try:
            head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 5)
            if len(head) > 16384:
                raise ValueError
            lines = head.decode("ascii").split("\r\n")
            method, raw_path, version = lines[0].split(" ")
            parsed = urlsplit(raw_path)
            path = parsed.path
            if version != "HTTP/1.1" or path not in {"/next", "/result", "/status"}:
                raise ValueError
            headers = dict(line.split(":", 1) for line in lines[1:] if ":" in line)
            headers = {key.casefold(): value.strip() for key, value in headers.items()}
            origin = headers.get("origin", "")
            if method == "OPTIONS":
                status = 204 if origin.startswith("moz-extension://") else 403
            elif not hmac.compare_digest(headers.get("authorization", ""), "Bearer " + self.token):
                status = 401
            elif method == "GET" and path == "/next":
                status = 200
                payload = {"task": self.pending}
            elif method == "GET" and path == "/status":
                status = 200
                task_id = parse_qs(parsed.query).get("id", [None])[0]
                active = self.pending is not None and self.pending["id"] == task_id
                payload = {"active": active, "cancel": not active}
            elif method == "POST" and path == "/result":
                length = int(headers.get("content-length", "-1"))
                if length < 0 or length > MAX_BODY:
                    raise ValueError
                data = json.loads(await asyncio.wait_for(reader.readexactly(length), 15))
                if not isinstance(data, dict) or not self.pending or data.get("id") != self.pending["id"]:
                    status = 409
                else:
                    status = 200
                    self.pending = None
                    if self.result and not self.result.done():
                        self.result.set_result(data)
                    payload = {"accepted": True}
        except (ValueError, UnicodeError, asyncio.IncompleteReadError, asyncio.LimitOverrunError,
                asyncio.TimeoutError, json.JSONDecodeError):
            pass
        finally:
            body = json.dumps(payload).encode("utf-8")
            phrase = {200: "OK", 204: "No Content", 400: "Bad Request", 401: "Unauthorized",
                      403: "Forbidden", 409: "Conflict"}[status]
            headers = [f"HTTP/1.1 {status} {phrase}", "Content-Type: application/json",
                       "Cache-Control: no-store", "Connection: close", f"Content-Length: {len(body)}"]
            if origin.startswith("moz-extension://"):
                headers += ["Access-Control-Allow-Origin: " + origin,
                            "Access-Control-Allow-Headers: Authorization, Content-Type",
                            "Access-Control-Allow-Methods: GET, POST, OPTIONS"]
            writer.write(("\r\n".join(headers) + "\r\n\r\n").encode("ascii") + body)
            try:
                await writer.drain()
            except ConnectionError:
                pass
            writer.close()
            await writer.wait_closed()

    async def _request(self, action, **arguments):
        if self.pending:
            raise DownloadError("Firefox extension bridge is busy; resume the job after it settles.")
        self.pending = {"id": secrets.token_hex(12), "action": action, **arguments}
        self.result = asyncio.get_running_loop().create_future()
        try:
            data = await asyncio.wait_for(self.result, 660 if action == "download" else self.timeout)
        except asyncio.TimeoutError:
            raise AuthRequired("Firefox extension did not respond. Open Firefox, pair the add-on with maps → p, then resume.") from None
        finally:
            self.pending = None
            self.result = None
        if data.get("error"):
            kind = data["error"]
            if kind == "auth":
                raise AuthRequired("Sign in to osu! in your regular Firefox window, then resume. Queue retained.")
            if kind == "rate":
                raise RateLimited(retry_seconds(data.get("retry")))
            if kind == "transient":
                raise TransientError(retry_seconds(data.get("retry"), default=60))
            raise DownloadError("Firefox could not finish this osu! request. Check its Downloads panel and resume.")
        return data.get("value")

    async def connect(self):
        account = await self._request("connect")
        if not isinstance(account, str) or not account.isdecimal():
            raise AuthRequired("Sign in to osu! in your regular Firefox window, then resume.")
        return account

    async def login(self, prompt):
        import webbrowser
        webbrowser.open("https://osu.ppy.sh/home")
        await prompt("Sign in to osu! in your regular Firefox window, then press Enter: ")
        return await self.connect()

    async def search(self, filters, cursor):
        value = await self._request("search", query=filters.query(cursor))
        if not isinstance(value, dict) or not isinstance(value.get("beatmapsets"), list):
            raise DownloadError("osu! search format changed. Queue retained.")
        return value["beatmapsets"], value.get("cursor_string")

    async def details(self, set_id):
        value = await self._request("details", set_id=set_id)
        if not isinstance(value, dict) or value.get("id") != set_id:
            raise DownloadError("osu! returned a different beatmapset. Queue retained.")
        return value

    async def download(self, set_id, target, progress):
        value = await self._request("download", set_id=set_id)
        if not isinstance(value, dict) or not isinstance(value.get("filename"), str):
            raise DownloadError("Firefox did not report a completed archive. Queue retained.")
        source = Path(value["filename"])
        target = Path(target)
        if (not source.is_file() or source.suffix.casefold() != ".osz"
                or source.parent.name != "osu-life-hacks"
                or not (source.stem == str(set_id) or source.stem.startswith(f"{set_id} ("))):
            raise DownloadError("Firefox archive is missing. Check its Downloads panel, then resume.")
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            with source.open("rb") as src, target.open("xb") as dst:
                while chunk := src.read(1024 * 1024):
                    dst.write(chunk)
                    progress(dst.tell())
        except FileExistsError:
            raise DownloadError("A staged archive already exists. Resume to validate it.") from None
        except BaseException:
            target.unlink(missing_ok=True)
            raise

    async def forget(self, set_id):
        await self._request("forget", set_id=set_id)


def selected_source(directory):
    """Firefox uses the installed add-on; other browsers keep their old source."""
    from .browser import BrowserSource, default_browser_path
    path = default_browser_path()
    return ExtensionSource(directory) if path and path.name.casefold() == "firefox.exe" else BrowserSource(directory)
