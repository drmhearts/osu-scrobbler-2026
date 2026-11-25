"""Firefox transport against an isolated local website; no osu! requests."""

import asyncio
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import threading
import time

import pytest

from osu_scrobbler.downloads.models import Filters, RateLimited
from test_downloads import osz


@pytest.mark.skipif(os.name != "nt", reason="Firefox downloader runs on Windows")
async def test_firefox_native_session_listing_download_and_limit(tmp_path, monkeypatch):
    pytest.importorskip("selenium")
    from osu_scrobbler.downloads import browser, firefox_source
    executable = browser.default_browser_path()
    if not executable or executable.name.casefold() != "firefox.exe":
        pytest.skip("Installed Firefox is not the Windows default browser")
    archive = osz()
    rate = [False]

    class Site(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path == "/beatmapsets/101/download":
                if rate[0]:
                    self.send_response(429)
                    self.send_header("Retry-After", "120")
                    self.end_headers()
                    self.wfile.write(b"Rate limit exceeded")
                else:
                    self.send_response(200)
                    self.send_header("Content-Type", "application/x-osu-beatmap-archive")
                    self.send_header("Content-Disposition", 'attachment; filename="101.osz"')
                    self.send_header("Content-Length", str(len(archive)))
                    self.end_headers()
                    self.wfile.write(archive)
                return
            if self.path.startswith("/beatmapsets/search"):
                content = json.dumps({"beatmapsets": [{"id": 101}], "cursor_string": None}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(content)))
                self.end_headers()
                self.wfile.write(content)
                return
            content = (f'<html><body><script id="json-current-user" type="application/json">{{"id":42}}</script>'
                       f'<a href="/beatmapsets/101/download">full archive</a></body></html>').encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(content)))
            self.end_headers()
            self.wfile.write(content)

        def log_message(self, *_):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Site)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setattr(firefox_source, "ORIGIN", f"http://127.0.0.1:{server.server_port}")
    try:
        async with firefox_source.FirefoxSource(tmp_path, executable, headless=True) as source:
            assert await source.connect() == "42"
            items, cursor = await source.search(Filters(), None)
            assert items == [{"id": 101}] and cursor is None
            target = tmp_path / "101.part"
            progress = []
            await asyncio.wait_for(source.download(101, target, progress.append), 30)
            assert target.read_bytes() == archive and progress == [len(archive)]
            await asyncio.to_thread(source.driver.add_cookie, {"name": "fixture_session", "value": "remembered",
                                                               "expiry": int(time.time()) + 86400})
            rate[0] = True
            with pytest.raises(RateLimited) as error:
                await asyncio.wait_for(source.download(101, tmp_path / "limited.part", lambda _: None), 30)
            assert error.value.seconds == 120
        rate[0] = False
        async with firefox_source.FirefoxSource(tmp_path, executable, headless=True) as resumed:
            assert await resumed.connect() == "42"
            cookie = await asyncio.to_thread(resumed.driver.get_cookie, "fixture_session")
            assert cookie["value"] == "remembered"
    finally:
        server.shutdown()
        server.server_close()
