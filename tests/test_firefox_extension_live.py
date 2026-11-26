"""Optional real Firefox test: isolated site, temporary add-on and loopback bridge."""

import asyncio
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import threading
from zipfile import ZipFile

import pytest

from osu_scrobbler.downloads.firefox_extension import ExtensionSource
from osu_scrobbler.downloads.models import Filters
from test_downloads import osz


@pytest.mark.skipif(os.name != "nt", reason="Firefox extension integration runs on Windows")
async def test_regular_firefox_cookie_drives_listing_and_full_archive(tmp_path):
    pytest.importorskip("selenium")
    from selenium import webdriver
    from selenium.webdriver.firefox.service import Service
    from osu_scrobbler.downloads.browser import default_browser_path

    executable = default_browser_path()
    if not executable or executable.name.casefold() != "firefox.exe":
        pytest.skip("Firefox is not the installed default browser")
    archive = osz()
    requests = []

    class Site(BaseHTTPRequestHandler):
        def do_GET(self):
            requests.append((self.path, self.headers.get("Cookie", "")))
            if self.path == "/beatmapsets/101/download":
                content, mime = archive, "application/x-osu-beatmap-archive"
            elif self.path.startswith("/beatmapsets/search"):
                content, mime = json.dumps({"beatmapsets": [{"id": 101}], "cursor_string": None}).encode(), "application/json"
            elif self.path == "/beatmapsets/101":
                content, mime = b'{"id":101}', "application/json"
            else:
                user = '{"id":42}' if "session=ok" in self.headers.get("Cookie", "") else "{}"
                content = f'<html><script id="json-current-user" type="application/json">{user}</script></html>'.encode()
                mime = "text/html"
            self.send_response(200)
            self.send_header("Content-Type", mime)
            self.send_header("Content-Length", str(len(content)))
            if self.path.endswith("/download"):
                self.send_header("Content-Disposition", 'attachment; filename="101.osz"')
            self.end_headers()
            self.wfile.write(content)

        def log_message(self, *_):
            pass

    site = ThreadingHTTPServer(("127.0.0.1", 0), Site)
    thread = threading.Thread(target=site.serve_forever, daemon=True)
    thread.start()
    bridge = ExtensionSource(tmp_path, token="test-pairing-code-for-fixture", port=0, timeout=35)
    driver = None
    try:
        async with bridge:
            bridge_port = bridge.server.sockets[0].getsockname()[1]
            extension_dir = Path(__file__).resolve().parents[1] / "firefox-extension"
            manifest = json.loads((extension_dir / "manifest.json").read_text("utf-8"))
            addon = tmp_path / "fixture.xpi"
            with ZipFile(addon, "w") as archive_file:
                archive_file.writestr("manifest.json", json.dumps(manifest))
                script = (extension_dir / "background.js").read_text("utf-8")
                script = script.replace("http://127.0.0.1:27895", f"http://127.0.0.1:{bridge_port}")
                script = script.replace("https://osu.ppy.sh", f"http://127.0.0.1:{site.server_port}")
                script = script.replace("loop();", "browser.storage.local.set({pairing:'test-pairing-code-for-fixture'}).then(loop);")
                archive_file.writestr("background.js", script)
                archive_file.write(extension_dir / "popup.html", "popup.html")
                archive_file.write(extension_dir / "popup.js", "popup.js")
            options = webdriver.FirefoxOptions()
            options.binary_location = str(executable)
            options.add_argument("-headless")
            options.set_preference("browser.download.folderList", 2)
            options.set_preference("browser.download.dir", str(tmp_path / "Downloads"))
            options.set_preference("browser.download.useDownloadDir", True)
            driver = await asyncio.to_thread(webdriver.Firefox, options=options, service=Service(log_output="NUL"))
            await asyncio.to_thread(driver.get, f"http://127.0.0.1:{site.server_port}/home")
            await asyncio.to_thread(driver.add_cookie, {"name": "session", "value": "ok"})
            await asyncio.to_thread(driver.install_addon, str(addon), temporary=True)
            assert await asyncio.wait_for(bridge.connect(), 35) == "42"
            assert (await bridge.search(Filters(), None))[0] == [{"id": 101}]
            assert await bridge.details(101) == {"id": 101}
            target = tmp_path / "staging" / "101.part"
            await asyncio.wait_for(bridge.download(101, target, lambda _: None), 35)
            assert target.read_bytes() == archive
            assert any(path.endswith("/download") and "session=ok" in cookie for path, cookie in requests)
    finally:
        if driver:
            await asyncio.to_thread(driver.quit)
        site.shutdown()
        site.server_close()
