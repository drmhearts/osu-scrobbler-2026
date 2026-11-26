import json
from types import SimpleNamespace

import pytest

from osu_scrobbler.osu_api import OsuApiStore, preview_ranked, public_token


class Vault:
    def __init__(self):
        self.values = {}

    def get_password(self, service, username):
        return self.values.get((service, username))

    def set_password(self, service, username, value):
        self.values[(service, username)] = value


class Client:
    def __init__(self, *, token_status=200, search_status=200):
        self.token_status = token_status
        self.search_status = search_status
        self.calls = []

    async def post(self, url, *, data, headers):
        self.calls.append(("post", url, data, headers))
        return SimpleNamespace(status_code=self.token_status, json=lambda: {"access_token": "ephemeral-token"})

    async def get(self, url, *, params, headers):
        self.calls.append(("get", url, params, headers))
        return SimpleNamespace(status_code=self.search_status, json=lambda: {"beatmapsets": [
            {"id": 42, "artist": "Artist", "title": "Song"}]})


async def test_public_osu_api_credentials_stay_in_vault_and_preview_uses_public_scope(tmp_path):
    vault = Vault()
    store = OsuApiStore(backend=vault)
    assert store.load() is None
    store.save(123, "private-secret")
    assert store.load() == (123, "private-secret")
    assert not list(tmp_path.iterdir())
    client = Client()
    assert await preview_ranked(store=store, client=client) == [
        {"id": 42, "artist": "Artist", "title": "Song"}]
    assert client.calls[0][2] == {"client_id": 123, "client_secret": "private-secret",
                                  "grant_type": "client_credentials", "scope": "public"}
    assert client.calls[1][3]["Authorization"] == "Bearer ephemeral-token"


async def test_rejected_osu_api_secret_is_not_exposed():
    client = Client(token_status=401)
    with pytest.raises(RuntimeError, match="Check the client ID and secret") as error:
        await public_token(123, "private-secret", client=client)
    assert "private-secret" not in str(error.value)


def test_invalid_osu_api_vault_value_fails_without_exposing_secret():
    vault = Vault()
    store = OsuApiStore(backend=vault)
    vault.set_password(store.service, store.username,
                       json.dumps({"client_id": "123", "client_secret": "private-secret"}))
    with pytest.raises(RuntimeError, match="could not be read") as error:
        store.load()
    assert "private-secret" not in str(error.value)
