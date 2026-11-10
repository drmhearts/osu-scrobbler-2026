import asyncio
import io
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from osu_scrobbler.downloads.browser import BrowserSource
from osu_scrobbler.downloads.models import AuthRequired, DownloadError, Filters, RateLimited, TransientError
from test_downloads import osz


@pytest.mark.parametrize("status,error", [(401, AuthRequired), (403, AuthRequired), (429, RateLimited), (503, TransientError)])
async def test_browser_failures_never_expose_upstream_credentials(tmp_path, monkeypatch, status, error):
    source = BrowserSource(tmp_path)
    async def checked():
        return "42"
    async def response(*args):
        return {"status": status, "retry": "120", "text": "private-session-token"}
    source.page = SimpleNamespace(evaluate=response)
    monkeypatch.setattr(source, "check", checked)
    with pytest.raises(error) as result:
        await source._json("/beatmapsets/search")
    assert "private-session-token" not in str(result.value)
    if status in {429, 503}:
        assert result.value.seconds == 120


async def test_non_json_verification_does_not_become_empty_listing(tmp_path, monkeypatch):
    source = BrowserSource(tmp_path)
    async def checked():
        return "42"
    async def response(*args):
        return {"status": 200, "text": "<html>verification secret-cookie</html>"}
    source.page = SimpleNamespace(evaluate=response)
    monkeypatch.setattr(source, "check", checked)
    with pytest.raises(AuthRequired):
        await source.search(Filters(), None)


def test_http_429_preserves_retry_after_without_urls_or_tokens(tmp_path):
    source = BrowserSource(tmp_path)
    source._response(SimpleNamespace(url="https://osu.ppy.sh/beatmapsets/101/download", status=429,
                                     headers={"retry-after": "7200"}))
    assert source.failed.is_set() and source.error.seconds == 7200
    assert "http" not in str(source.error)


@pytest.mark.skipif(os.name != "nt", reason="Downloader supports installed Edge/Chrome on Windows")
async def test_native_browser_full_archive_and_429_transport_with_isolated_routed_site(tmp_path):
    # Real Chromium downloads and browser events, entirely mocked network.
    # No user profile, external login or live beatmap requests.
    playwright = pytest.importorskip("playwright.async_api")
    async with playwright.async_playwright() as driver:
        browser = None
        for channel in ("msedge", "chrome"):
            try:
                browser = await driver.chromium.launch(channel=channel, headless=True)
                break
            except Exception:
                continue
        if browser is None:
            pytest.skip("No installed Edge/Chrome is available for native transport fixture.")
        context = await browser.new_context(accept_downloads=True)
        requests = []
        rate = [False]
        html = '<html><body><script id="json-current-user" type="application/json">{"id":42}</script><a href="https://osu.ppy.sh/beatmapsets/101/download">Download full archive</a></body></html>'
        async def route(request):
            url = request.request.url
            requests.append(url)
            if url.endswith("/download"):
                if rate[0]:
                    await request.fulfill(status=429, headers={"Retry-After": "120"}, body="Rate limit exceeded")
                else:
                    await request.fulfill(status=200, body=osz(), headers={
                        "Content-Type": "application/x-osu-beatmap-archive",
                        "Content-Disposition": 'attachment; filename="101.osz"'})
            elif url.startswith("https://osu.ppy.sh/"):
                await request.fulfill(status=200, body=html, content_type="text/html")
            else:
                await request.abort()
        await context.route("**/*", route)
        source = BrowserSource(tmp_path)
        source.context = context
        source.page = await context.new_page()
        source.page.on("response", source._response)
        try:
            assert await source.connect() == "42"
            target = tmp_path / "full.part"
            progress = []
            await source.download(101, target, progress.append)
            assert target.read_bytes() == osz() and progress == [len(osz())]
            assert not any("noVideo" in url or "/api/v2/" in url for url in requests)
            rate[0] = True
            with pytest.raises(RateLimited) as error:
                await asyncio.wait_for(source.download(101, tmp_path / "limited.part", lambda _: None), 15)
            assert error.value.seconds == 120
            assert not (tmp_path / "limited.part").exists()
        finally:
            await context.close()
            await browser.close()
