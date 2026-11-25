"""Firefox transport using the user's installed Firefox and an isolated profile.

geckodriver is managed by Selenium; no browser binary or ordinary Firefox
profile is downloaded or read. The profile is reused for osu! website sign-in.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
import socket
import tempfile
from urllib.parse import urlencode, urlparse

from .limiter import retry_seconds
from .models import AuthRequired, DownloadError, RateLimited, TransientError

ORIGIN = "https://osu.ppy.sh"


class FirefoxSource:
    def __init__(self, directory, executable, *, headless=False):
        self.directory = Path(directory)
        self.executable = Path(executable)
        self.headless = headless
        self.driver = self.download_temp = None
        self.account = self.error = None
        self.failed = asyncio.Event()
        self.loop = None

    async def __aenter__(self):
        from selenium import webdriver
        from selenium.webdriver.firefox.service import Service

        if not self.executable.is_file():
            raise DownloadError("Your default Firefox installation is unavailable. Check Windows default apps and retry.")
        profile = self.directory / "browser-firefox"
        profile.mkdir(parents=True, exist_ok=True)
        downloads = self.directory / "browser-downloads"
        downloads.mkdir(parents=True, exist_ok=True)
        self.download_temp = tempfile.TemporaryDirectory(prefix="firefox-", dir=downloads)
        self.loop = asyncio.get_running_loop()
        options = webdriver.FirefoxOptions()
        options.binary_location = str(self.executable)
        if self.headless:
            options.add_argument("-headless")
        # geckodriver's -profile argument keeps this one dedicated profile in
        # place; FirefoxProfile/options.profile would copy it and lose sign-in.
        options.add_argument("-profile")
        options.add_argument(str(profile))
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        options.add_argument("--marionette-port")
        options.add_argument(str(port))
        options.enable_bidi = True
        options.set_preference("browser.download.folderList", 2)
        options.set_preference("browser.download.dir", self.download_temp.name)
        options.set_preference("browser.download.useDownloadDir", True)
        options.set_preference("browser.helperApps.neverAsk.saveToDisk",
                               "application/x-osu-beatmap-archive,application/octet-stream,application/zip")
        options.set_preference("browser.download.alwaysOpenPanel", False)
        try:
            self.driver = await asyncio.to_thread(webdriver.Firefox, options=options, service=Service(log_output="NUL"))
            await asyncio.to_thread(self.driver.set_page_load_timeout, 45)
            await asyncio.to_thread(self.driver.set_script_timeout, 45)
            self.driver.network.add_event_handler("response_started", self._response)
            return self
        except asyncio.CancelledError:
            await self.__aexit__(None, None, None)
            raise
        except Exception:
            await self.__aexit__(None, None, None)
            raise DownloadError("Firefox could not start. Close this tool's Firefox window and retry; Selenium may need its geckodriver update.") from None

    def _response(self, event):
        """Observe status/Retry-After without retaining URLs, headers or cookies."""
        raw = event if isinstance(event, dict) else getattr(event, "__dict__", {})
        request = raw.get("request") or {}
        if urlparse(request.get("url", "")).hostname != urlparse(ORIGIN).hostname:
            return
        response = raw.get("response") or {}
        status = response.get("status")
        headers = {}
        for item in response.get("headers") or []:
            if isinstance(item, dict) and isinstance(item.get("name"), str):
                value = item.get("value")
                headers[item["name"].casefold()] = value.get("value") if isinstance(value, dict) else value
        if status == 429:
            self.error = RateLimited(retry_seconds(headers.get("retry-after")))
        elif isinstance(status, int) and status >= 500:
            self.error = TransientError(retry_seconds(headers.get("retry-after"), default=60))
        if self.error:
            self.loop.call_soon_threadsafe(self.failed.set)

    async def _page(self, path):
        try:
            await asyncio.to_thread(self.driver.get, ORIGIN + path)
        except Exception:
            if self.error:
                raise self.error
            raise TransientError() from None
        await self.check()

    async def check(self, *, require_login=True):
        if self.error:
            raise self.error
        try:
            url = await asyncio.to_thread(lambda: self.driver.current_url)
            if urlparse(url).hostname != urlparse(ORIGIN).hostname:
                raise AuthRequired("Complete osu! sign-in/verification in Firefox.")
            text = await asyncio.to_thread(self.driver.execute_script,
                                           "return (document.body?.innerText || '').slice(0,10000).toLowerCase()")
            if any(s in text for s in ("verify you are human", "just a moment...", "captcha")):
                raise AuthRequired("Complete browser verification yourself using maps → sign in, then resume.")
            if "rate limit exceeded" in text or "too many requests" in text:
                raise RateLimited()
            if require_login:
                user = await asyncio.to_thread(self.driver.execute_script, """try {
                    const u=JSON.parse(document.querySelector('#json-current-user')?.textContent || '{}');
                    return Number.isInteger(u.id) && u.id > 0 ? String(u.id) : null;
                } catch { return null; }""")
                if not user:
                    raise AuthRequired("osu! sign-in expired. Use maps → sign in, then resume; progress is retained.")
                if self.account and self.account != user:
                    raise AuthRequired("The signed-in account changed. Resume with the job's original account; cooldowns are retained.")
                self.account = user
            return self.account
        except DownloadError:
            raise
        except Exception:
            raise TransientError() from None

    async def login(self, prompt):
        try:
            await asyncio.to_thread(self.driver.get, ORIGIN + "/home")
        except Exception:
            raise DownloadError("Could not open osu! sign-in in Firefox.") from None
        while True:
            answer = (await prompt("Sign in and complete verification in Firefox. Press Enter to continue, or type help: ")).strip().casefold()
            if answer in {"help", "help download"}:
                from ..help import help_text
                print(help_text("download"))
            elif not answer:
                break
            else:
                print("Press Enter after signing in, or type help for instructions.")
        await self._page("/home")
        return self.account

    async def connect(self):
        await self._page("/home")
        return self.account

    async def _json(self, path):
        await self.check()
        try:
            result = await asyncio.to_thread(self.driver.execute_async_script, """
                const path=arguments[0], done=arguments[arguments.length-1];
                fetch(path, {headers:{Accept:'application/json','X-Requested-With':'XMLHttpRequest'}})
                  .then(async r => done({status:r.status, retry:r.headers.get('Retry-After'), text:await r.text()}))
                  .catch(() => done({status:0}));
            """, path)
        except Exception:
            if self.error:
                raise self.error
            raise TransientError() from None
        status = result.get("status") if isinstance(result, dict) else None
        if status == 429:
            raise RateLimited(retry_seconds(result.get("retry")))
        if status in {401, 403}:
            raise AuthRequired("osu! requires sign-in or browser verification. Use maps → sign in; queue retained.")
        if isinstance(status, int) and status >= 500:
            raise TransientError(retry_seconds(result.get("retry"), default=60))
        if status != 200:
            raise DownloadError("osu! listing is unavailable; queue preserved.")
        try:
            value = json.loads(result["text"])
            if not isinstance(value, dict):
                raise ValueError
            return value
        except (ValueError, KeyError, TypeError):
            raise AuthRequired("osu! returned a verification page or changed response. Check Firefox; no bypass is attempted.") from None

    async def search(self, filters, cursor):
        result = await self._json("/beatmapsets/search?" + urlencode(filters.query(cursor)))
        if not isinstance(result.get("beatmapsets"), list):
            raise DownloadError("osu! search format changed. Checkpoint retained.")
        return result["beatmapsets"], result.get("cursor_string")

    async def details(self, set_id):
        result = await self._json(f"/beatmapsets/{set_id}")
        if result.get("id") != set_id:
            raise DownloadError("osu! returned a different beatmapset; nothing downloaded.")
        return result

    async def download(self, set_id, target, progress):
        from selenium.webdriver.common.by import By

        await self._page(f"/beatmapsets/{set_id}")
        href = f"{ORIGIN}/beatmapsets/{set_id}/download"
        selector = f'a[href="{href}"], a[href="/beatmapsets/{set_id}/download"]'
        before = set(Path(self.download_temp.name).iterdir())
        try:
            link = await asyncio.to_thread(self.driver.find_element, By.CSS_SELECTOR, selector)
            await asyncio.to_thread(link.click)
        except Exception:
            if self.error:
                raise self.error
            raise DownloadError("Full archive link is unavailable in Firefox; queue retained.") from None
        deadline = asyncio.get_running_loop().time() + 300
        previous = None
        while asyncio.get_running_loop().time() < deadline:
            if self.error:
                raise self.error
            files = [p for p in Path(self.download_temp.name).iterdir() if p not in before]
            partial = any(p.suffix == ".part" for p in files)
            complete = [p for p in files if p.is_file() and p.suffix.casefold() == ".osz"]
            if complete and not partial:
                archive = max(complete, key=lambda p: p.stat().st_mtime_ns)
                size = archive.stat().st_size
                if size > 0 and (archive, size) == previous:
                    archive.replace(target)
                    progress(size)
                    return
                previous = (archive, size)
            else:
                previous = None
            await asyncio.sleep(0.5)
        raise DownloadError("Firefox download did not complete. Sign-in or website layout may have changed; queue retained.")

    async def __aexit__(self, *args):
        if self.driver:
            try:
                await asyncio.to_thread(self.driver.quit)
            except Exception:
                pass
        if self.download_temp:
            self.download_temp.cleanup()
