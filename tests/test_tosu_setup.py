import hashlib
import io
from types import SimpleNamespace
from zipfile import ZipFile

import httpx
import pytest

from osu_scrobbler.config import Config
from osu_scrobbler.tosu import ensure_tosu, prepare_settings
from osu_scrobbler.updater import UpdateError


def official_fixture(monkeypatch, payload=b"fixture executable", filename="tosu.exe"):
    import osu_scrobbler.tosu as module
    stream = io.BytesIO()
    with ZipFile(stream, "w") as archive:
        archive.writestr(filename, payload)
    content = stream.getvalue()
    monkeypatch.setattr(module, "TOSU_SIZE", len(content))
    monkeypatch.setattr(module, "TOSU_SHA256", hashlib.sha256(content).hexdigest())
    return content


async def test_tosu_installs_once_from_official_release_and_never_opens_browser(tmp_path, monkeypatch):
    import osu_scrobbler.tray as tray
    content = official_fixture(monkeypatch)
    requests = []
    def respond(request):
        requests.append(request)
        assert request.url.host == "github.com" and "/tosuapp/tosu/releases/download/" in str(request.url)
        return httpx.Response(200, content=content)
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        path = await ensure_tosu(tmp_path, client=client)
        assert path.read_bytes() == b"fixture executable"
        settings = (path.parent / "tosu.env").read_text()
        for entry in ("OPEN_DASHBOARD_ON_STARTUP=false", "ENABLE_INGAME_OVERLAY=false", "SERVER_IP=127.0.0.1", "ENABLE_KEY_OVERLAY=false", "CALCULATE_PP=false"):
            assert entry in settings
        assert (path.parent / "LICENSE").exists()
        assert await ensure_tosu(tmp_path, client=client) == path
        assert len(requests) == 1
    calls = []
    monkeypatch.setattr(tray.subprocess, "CREATE_NO_WINDOW", 0x08000000, raising=False)
    monkeypatch.setattr(tray.subprocess, "Popen", lambda args, **kwargs: calls.append((args, kwargs)))
    tray.start_tosu(path)
    assert calls[0][1]["creationflags"] == 0x08000000
    assert "--update=false" in calls[0][0]


async def test_corrupt_download_and_unsafe_archive_never_install_or_start_tosu(tmp_path, monkeypatch):
    content = official_fixture(monkeypatch)
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(200, content=content[:-1] + b"x"))) as client:
        with pytest.raises(UpdateError, match="checksum"):
            await ensure_tosu(tmp_path, client=client)
    content = official_fixture(monkeypatch, filename="../tosu.exe")
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(200, content=content))) as client:
        with pytest.raises(UpdateError, match="contents"):
            await ensure_tosu(tmp_path, client=client)
    assert not list(tmp_path.rglob("tosu.exe"))
    assert not (tmp_path / "tosu.exe").exists()


def test_managed_settings_respect_local_endpoint_and_never_modify_external_tosu(tmp_path):
    exe = tmp_path / "managed/tosu.exe"
    exe.parent.mkdir()
    exe.write_bytes(b"fixture")
    unrelated = tmp_path / "external/tosu.env"
    unrelated.parent.mkdir()
    unrelated.write_text("OPEN_DASHBOARD_ON_STARTUP=true")
    prepare_settings(exe, Config(tosu_url="ws://127.0.0.1:24051/websocket/v2"))
    assert "SERVER_PORT=24051" in (exe.parent / "tosu.env").read_text()
    assert unrelated.read_text() == "OPEN_DASHBOARD_ON_STARTUP=true"


async def test_configured_tosu_in_any_folder_gets_a_quiet_copy_without_changing_original(tmp_path):
    source = tmp_path / "Another drive with spaces_日本語/tosu.exe"
    source.parent.mkdir()
    source.write_bytes(b"chosen executable")
    settings = source.with_name("tosu.env")
    settings.write_text("OPEN_DASHBOARD_ON_STARTUP=true")
    profile = tmp_path / "profile"
    Config(tosu_path=str(source)).save(profile)
    path = await ensure_tosu(profile)
    assert path != source and path.read_bytes() == source.read_bytes()
    assert "OPEN_DASHBOARD_ON_STARTUP=false" in path.with_name("tosu.env").read_text()
    assert settings.read_text() == "OPEN_DASHBOARD_ON_STARTUP=true"
