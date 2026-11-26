import asyncio
from pathlib import Path

import httpx
import pytest

from osu_scrobbler.downloads.firefox_extension import ExtensionSource, PairingStore, selected_source
from osu_scrobbler.downloads.models import AuthRequired, RateLimited


class Vault:
    def __init__(self):
        self.values = {}

    def get_password(self, service, username):
        return self.values.get((service, username))

    def set_password(self, service, username, value):
        self.values[(service, username)] = value


def test_pairing_code_is_persistent_and_rotatable_only_in_vault(tmp_path):
    store = PairingStore(backend=Vault())
    assert store.token() is None
    original = store.token(create=True)
    assert len(original) >= 24 and store.token() == original
    assert store.token(create=True, rotate=True) != original
    assert not list(tmp_path.iterdir())


async def test_bridge_requires_code_and_maps_browser_results(tmp_path):
    source = ExtensionSource(tmp_path, token="private-pairing-code", port=0)
    async with source:
        port = source.server.sockets[0].getsockname()[1]
        async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}", trust_env=False) as client:
            assert (await client.get("/next")).status_code == 401
            assert (await client.get("/next", headers={"Authorization": "Bearer wrong"})).status_code == 401
            headers = {"Authorization": "Bearer private-pairing-code"}
            pending = asyncio.create_task(source.connect())
            await asyncio.sleep(0)
            task = (await client.get("/next", headers=headers)).json()["task"]
            assert task["action"] == "connect"
            assert (await client.post("/result", headers=headers, json={"id": "wrong", "value": "42"})).status_code == 409
            assert (await client.post("/result", headers=headers, json={"id": task["id"], "value": "42"})).status_code == 200
            assert await pending == "42"
            assert (await client.get("/next", headers=headers)).json()["task"] is None

            pending = asyncio.create_task(source.details(42))
            await asyncio.sleep(0)
            task = (await client.get("/next", headers=headers)).json()["task"]
            assert task["id"] != "wrong"
            await client.post("/result", headers=headers, json={"id": task["id"], "error": "rate", "retry": "10"})
            with pytest.raises(RateLimited) as error:
                await pending
            assert error.value.seconds == 10


async def test_bridge_cancellation_and_recovery_does_not_reuse_task_id(tmp_path):
    source = ExtensionSource(tmp_path, token="private-pairing-code", port=0)
    async with source:
        port = source.server.sockets[0].getsockname()[1]
        headers = {"Authorization": "Bearer private-pairing-code"}
        async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}", trust_env=False) as client:
            pending = asyncio.create_task(source.connect())
            await asyncio.sleep(0)
            first = (await client.get("/next", headers=headers)).json()["task"]["id"]
            pending.cancel()
            with pytest.raises(asyncio.CancelledError):
                await pending
            status = (await client.get("/status?id=" + first, headers=headers)).json()
            assert status["cancel"] is True
            pending = asyncio.create_task(source.connect())
            await asyncio.sleep(0)
            second = (await client.get("/next", headers=headers)).json()["task"]["id"]
            assert second != first
            assert (await client.get("/status?id=" + first, headers=headers)).json()["cancel"] is True
            await client.post("/result", headers=headers, json={"id": second, "value": "42"})
            assert await pending == "42"


async def test_completed_firefox_archive_is_copied_to_staging_not_deleted(tmp_path):
    source_file = tmp_path / "Downloads" / "osu-life-hacks" / "42.osz"
    source_file.parent.mkdir(parents=True)
    source_file.write_bytes(b"archive bytes")
    source = ExtensionSource(tmp_path, token="private-pairing-code", port=0)
    async with source:
        port = source.server.sockets[0].getsockname()[1]
        headers = {"Authorization": "Bearer private-pairing-code"}
        async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}", trust_env=False) as client:
            target = tmp_path / "staging" / "42.part"
            sizes = []
            pending = asyncio.create_task(source.download(42, target, sizes.append))
            await asyncio.sleep(0)
            task = (await client.get("/next", headers=headers)).json()["task"]
            assert task["action"] == "download" and task["set_id"] == 42
            await client.post("/result", headers=headers, json={"id": task["id"],
                "value": {"filename": str(source_file), "bytes": source_file.stat().st_size}})
            await pending
            assert target.read_bytes() == source_file.read_bytes()
            assert source_file.is_file() and sizes[-1] == source_file.stat().st_size


def test_firefox_is_selected_without_automated_profile(tmp_path, monkeypatch):
    from osu_scrobbler.downloads import browser
    monkeypatch.setattr(browser, "default_browser_path", lambda: Path("C:/Program Files/Mozilla Firefox/firefox.exe"))
    assert isinstance(selected_source(tmp_path), ExtensionSource)
