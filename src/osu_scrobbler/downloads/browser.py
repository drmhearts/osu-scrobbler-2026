"""Chromium website source, with Firefox delegated to the regular-profile add-on.

No game tokens, passwords or browser verification are read/bypassed. Chrome and
Edge use a dedicated profile. Firefox uses its extension in the usual profile.
"""

import asyncio
import ctypes
import os
from pathlib import Path
from urllib.parse import urlencode, urlparse

from .limiter import retry_seconds
from .models import AuthRequired, DownloadError, RateLimited, TransientError

ORIGIN = "https://osu.ppy.sh"


def default_browser_path():
    """Ask Windows for the executable associated with HTTPS for this user."""
    if os.name != "nt":
        return None
    buffer = ctypes.create_unicode_buffer(32768)
    length = ctypes.c_ulong(len(buffer))
    # ASSOCSTR_EXECUTABLE=2. This resolves the user's HTTPS choice without
    # parsing UserChoice hashes or opening a tab in another browser.
    result = ctypes.windll.shlwapi.AssocQueryStringW(
        0, 2, "https", "open", buffer, ctypes.byref(length))
    path = Path(buffer.value) if result == 0 and buffer.value else None
    return path if path and path.is_file() else None


class BrowserSource:
    def __init__(self, directory):
        self.directory = Path(directory)
        self.context = self.page = self.playwright = None
        self.account = None
        self.error = None
        self.failed = asyncio.Event()
        self.extension = None

    async def __aenter__(self):
        if os.name != "nt":
            raise DownloadError("Browser downloads currently support Windows. Core/fixture commands remain portable.")
        # Refuse insecure credential backends, matching the existing app policy.
        from ..lastfm import CredentialStore
        CredentialStore()._secure_backend()
        executable = default_browser_path()
        name = executable.name.casefold() if executable else ""
        if name == "firefox.exe":
            from .firefox_extension import ExtensionSource
            self.extension = ExtensionSource(self.directory)
            await self.extension.__aenter__()
            return self.extension
        if name not in {"msedge.exe", "chrome.exe"}:
            raise DownloadError("Set Firefox, Chrome or Edge as your Windows default browser, then retry. No other browser was opened.")
        from playwright.async_api import async_playwright
        self.playwright = await async_playwright().start()
        profile = self.directory / "browser"
        profile.mkdir(parents=True, exist_ok=True)
        (self.directory / "browser-downloads").mkdir(parents=True, exist_ok=True)
        try:
            channel = "msedge" if name == "msedge.exe" else "chrome"
            try:
                self.context = await self.playwright.chromium.launch_persistent_context(
                    str(profile), channel=channel, executable_path=str(executable), headless=False,
                    accept_downloads=True,
                    ignore_default_args=["--password-store=basic", "--use-mock-keychain"],
                    no_viewport=True, downloads_path=str(self.directory / "browser-downloads"))
            except Exception:
                pass
            if not self.context:
                raise DownloadError("Close this tool's dedicated browser or repair your default browser, then retry.")
            self.context.set_default_timeout(15000)
            self.page = self.context.pages[0] if self.context.pages else await self.context.new_page()
            self.page.on("response", self._response)
            return self
        except BaseException:
            await self.__aexit__(None, None, None)
            raise

    def _response(self, response):
        url = urlparse(response.url)
        if url.hostname != "osu.ppy.sh":
            return  # No credentials or mirror URLs are retained.
        if response.status == 429:
            self.error = RateLimited(retry_seconds(response.headers.get("retry-after")))
        elif response.status >= 500:
            self.error = TransientError(retry_seconds(response.headers.get("retry-after"), default=60))
        if self.error:
            self.failed.set()

    async def _page(self, path):
        from playwright.async_api import Error
        try:
            await self.page.goto(ORIGIN + path, wait_until="domcontentloaded", timeout=45000)
        except Error:
            if self.error:
                raise self.error
            raise TransientError() from None
        await self.check()

    async def check(self, *, require_login=True):
        if self.error:
            raise self.error
        if urlparse(self.page.url).hostname != "osu.ppy.sh":
            raise AuthRequired("Complete osu! sign-in/verification in the dedicated browser.")
        text = (await self.page.locator("body").inner_text())[:10000].casefold()
        if any(s in text for s in ("verify you are human", "just a moment...", "captcha")):
            raise AuthRequired("Complete browser verification yourself using maps → sign in, then resume.")
        if "rate limit exceeded" in text or "too many requests" in text:
            raise RateLimited()
        if require_login:
            user = await self.page.evaluate("""() => {
                try { const u=JSON.parse(document.querySelector('#json-current-user')?.textContent || '{}');
                    return Number.isInteger(u.id) && u.id > 0 ? String(u.id) : null; } catch { return null; }
            }""")
            if not user:
                raise AuthRequired("osu! sign-in expired. Use maps → sign in, then resume; progress is retained.")
            if self.account and self.account != user:
                raise AuthRequired("The signed-in account changed. Resume with the job's original account; cooldowns are retained.")
            self.account = user
        return self.account

    async def login(self, prompt):
        try:
            await self.page.goto(ORIGIN + "/home", wait_until="domcontentloaded", timeout=45000)
        except Exception:
            raise DownloadError("Could not open osu! sign-in in the dedicated browser.") from None
        while True:
            answer = (await prompt("Sign in and complete verification in the open browser. Press Enter to continue, or type help: ")).strip().casefold()
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
        # Same-origin browser request; cookies remain exclusively in Chromium.
        result = await self.page.evaluate("""async path => {
            const r = await fetch(path, {headers:{Accept:'application/json','X-Requested-With':'XMLHttpRequest'}});
            return {status:r.status, retry:r.headers.get('Retry-After'), text:await r.text()};
        }""", path)
        status = result["status"]
        if status == 429:
            raise RateLimited(retry_seconds(result.get("retry")))
        if status in {401, 403}:
            raise AuthRequired("osu! requires sign-in or browser verification. Use maps → sign in; queue retained.")
        if status >= 500:
            raise TransientError(retry_seconds(result.get("retry"), default=60))
        if status != 200:
            raise DownloadError("osu! listing is unavailable; queue preserved.")
        import json
        try:
            value = json.loads(result["text"])
            if not isinstance(value, dict):
                raise ValueError()
            return value
        except (ValueError, KeyError):
            raise AuthRequired("osu! returned a verification page or changed response. Check the dedicated browser; no bypass is attempted.") from None

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
        await self._page(f"/beatmapsets/{set_id}")
        href = f"{ORIGIN}/beatmapsets/{set_id}/download"
        # Exact full-media link only. Never choose no-video, a mirror, or modify the URL.
        link = self.page.locator(f'a[href="{href}"], a[href="/beatmapsets/{set_id}/download"]').first
        async def transfer():
            try:
                async with self.page.expect_download(timeout=300000) as event:
                    await link.click()
                download = await event.value
                await download.save_as(str(target))
                failure = await download.failure()
                if failure:
                    raise DownloadError("Transfer interrupted; queue retained and partial archive will be revalidated.")
                progress(Path(target).stat().st_size)
            except DownloadError:
                raise
            except Exception:
                if self.error:
                    raise self.error
                await self.check()
                raise DownloadError("Download did not complete. Sign-in, access or website layout may have changed; queue retained.") from None
        task = asyncio.create_task(transfer())
        failure = asyncio.create_task(self.failed.wait())
        try:
            done, _ = await asyncio.wait({task, failure}, return_when=asyncio.FIRST_COMPLETED)
            if failure in done:
                raise self.error
            await task
        finally:
            for pending in (task, failure):
                if not pending.done():
                    pending.cancel()
            await asyncio.gather(task, failure, return_exceptions=True)

    async def __aexit__(self, *args):
        if self.extension:
            await self.extension.__aexit__(*args)
            return
        if self.context:
            try:
                await self.context.close()
            except Exception:
                pass
        if self.playwright:
            await self.playwright.stop()
