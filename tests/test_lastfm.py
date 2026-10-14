import hashlib
import json
import asyncio

import httpx
import pytest

from osu_scrobbler.lastfm import (CredentialError, Credentials, CredentialStore,
                                  LastFmClient, LastFmError, api_signature)
from osu_scrobbler.models import Submission, Track


@pytest.fixture
def submission():
    return Submission("id", "attempt", Track("曲", "歌手", "アルバム", "歌手", 180), 1700000000, 1700000030)


def client_for(handler):
    return LastFmClient(Credentials("key", "secret", "session"), httpx.AsyncClient(transport=httpx.MockTransport(handler)))


def response(accepted="1", ignored="0", code="0"):
    return {"scrobbles": {"@attr": {"accepted": accepted, "ignored": ignored},
            "scrobble": {"ignoredMessage": {"code": code, "#text": ""}}}}


def test_signature_excludes_transport_and_uses_unencoded_unicode():
    params = {"artist": "歌手 + &", "method": "track.scrobble", "format": "json", "callback": "cb"}
    expected = hashlib.md5("artist歌手 + &methodtrack.scrobble秘密".encode()).hexdigest()
    assert api_signature(params, "秘密") == expected


@pytest.mark.asyncio
async def test_accepted_counts_and_exact_playback_start(submission):
    from urllib.parse import parse_qs
    def handler(request):
        assert request.method == "POST"
        payload = {k: v[0] for k, v in parse_qs(request.content.decode()).items()}
        assert payload["timestamp"] == "1700000000"
        assert payload["artist"] == "歌手"
        assert payload["sk"] == "session"
        assert payload["api_sig"] == api_signature(payload, "secret")
        return httpx.Response(200, json=response())
    client = client_for(handler)
    result = await client.submit(submission)
    assert (result.status, result.accepted, result.ignored) == ("accepted", 1, 0)
    await client.client.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("body,status", [(response("0", "1", "3"), "ignored"),
    (response("1", "0", "3"), "uncertain"), (response("0", "0", "0"), "uncertain"),
    ({"ok": True}, "uncertain"), (response("2", "0", "0"), "uncertain")])
async def test_http_success_alone_is_not_acceptance(submission, body, status):
    client = client_for(lambda _: httpx.Response(200, json=body))
    result = await client.submit(submission)
    assert result.status == status
    await client.client.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("code,status", [(8,"retry"),(11,"retry"),(16,"retry"),(29,"retry"),(9,"failed"),(13,"failed")])
async def test_api_error_classification_never_exposes_response_secrets(submission, code, status):
    client = client_for(lambda _: httpx.Response(200, json={"error": code, "message": "key secret session"}))
    result = await client.submit(submission)
    assert result.status == status
    assert "secret" not in result.message
    await client.client.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("exception,status", [(httpx.ConnectError, "retry"), (httpx.ConnectTimeout,"retry"),
    (httpx.PoolTimeout,"retry"), (httpx.ReadTimeout,"uncertain"), (httpx.WriteTimeout,"uncertain"),
    (httpx.ReadError,"uncertain"), (httpx.WriteError,"uncertain")])
async def test_network_failure_does_not_silently_retry(submission, exception, status):
    calls = 0
    def handler(request):
        nonlocal calls
        calls += 1
        raise exception("secret should never be displayed", request=request)
    client = client_for(handler)
    result = await client.submit(submission)
    assert result.status == status
    assert calls == 1
    assert "secret" not in result.message
    await client.client.aclose()


@pytest.mark.asyncio
async def test_authorization_flow_and_redacted_errors():
    calls = []
    def handler(request):
        params = dict(request.url.params)
        calls.append(params["method"])
        assert params["api_sig"] == api_signature(params, "secret")
        assert "sk" not in params
        if params["method"] == "auth.getToken":
            return httpx.Response(200, json={"token": "token"})
        assert params["token"] == "token"
        return httpx.Response(200, json={"session": {"key": "authorized-session"}})
    client = client_for(handler)
    assert await client.get_token() == "token"
    assert "api_key=key&token=token" in client.authorization_url("token")
    assert await client.get_session("token") == "authorized-session"
    assert calls == ["auth.getToken", "auth.getSession"]
    await client.client.aclose()


class FakeVault:
    def __init__(self):
        self.data = {}
    def get_password(self, service, username):
        return self.data.get((service, username))
    def set_password(self, service, username, value):
        self.data[(service, username)] = value
    def delete_password(self, service, username):
        del self.data[(service, username)]


def test_credentials_only_in_secure_vault():
    vault = FakeVault()
    store = CredentialStore(backend=vault)
    credentials = Credentials("key", "secret", "session")
    assert store.load() is None
    store.save(credentials)
    assert store.load() == credentials
    store.delete()
    assert store.load() is None


def test_plaintext_backend_rejected(monkeypatch):
    import osu_scrobbler.lastfm as module
    monkeypatch.setattr(module.sys, "platform", "linux")
    monkeypatch.setattr(module.keyring, "get_keyring", lambda: FakeVault())
    with pytest.raises(CredentialError, match="no plaintext fallback"):
        CredentialStore().save(Credentials("key", "secret"))


def test_corrupt_vault_error_does_not_print_content():
    vault = FakeVault()
    vault.set_password("osu-scrobbler", "credentials", "secret")
    with pytest.raises(CredentialError) as error:
        CredentialStore(backend=vault).load()
    assert "secret" not in str(error.value)


async def test_background_launches_reuse_saved_key_secret_and_session_without_prompts(tmp_path, unused_tcp_port, monkeypatch):
    import osu_scrobbler.watcher as watcher_module
    from osu_scrobbler.config import Config
    from osu_scrobbler.models import Phase, Telemetry
    from osu_scrobbler.tray import Supervisor
    from osu_scrobbler.watcher import Watcher

    vault = FakeVault()
    monkeypatch.setattr(CredentialStore, "_secure_backend", lambda _: vault)
    expected = Credentials("remembered-api-key", "remembered-api-secret", "remembered-session")
    CredentialStore().save(expected)
    clients = []
    watchers = []
    messages = []
    ready = asyncio.Event()

    def forbid_prompt(*args, **kwargs):
        pytest.fail("Remembered authorization must not prompt or open a browser")
    monkeypatch.setattr("getpass.getpass", forbid_prompt)
    monkeypatch.setattr("builtins.input", forbid_prompt)
    monkeypatch.setattr("webbrowser.open", forbid_prompt)

    class SavedClient:
        def __init__(self, credentials):
            self.credentials = credentials
            self.closed = False
            clients.append(self)
        async def aclose(self):
            self.closed = True
        async def submit(self, submission):
            pytest.fail("This startup test must not send any scrobbles")

    class IdleAdapter:
        def __init__(self, **options):
            pass
        async def stream(self):
            yield Telemetry(None, Phase.MENU)
            await asyncio.Event().wait()

    class NoLookup:
        async def aclose(self):
            pass

    def output(message):
        messages.append(message)
        if message == "Waiting for osu!...":
            ready.set()

    def factory(directory, config, **options):
        watcher = Watcher(directory, config, musicbrainz=NoLookup(), **options)
        watchers.append(watcher)
        return watcher

    async def absent(directory):
        return False

    monkeypatch.setattr(watcher_module, "LastFmClient", SavedClient)
    monkeypatch.setattr(watcher_module, "InternalAdapter", IdleAdapter)
    Config(ipc_port=unused_tcp_port).save(tmp_path)
    monitor = Supervisor(tmp_path, watcher_factory=factory, probe=absent, output=output)
    try:
        for number in range(2):
            ready.clear()
            await monitor.tick({"osu!.exe"}, number * 10)
            await asyncio.wait_for(ready.wait(), 2)
            assert watchers[-1].delivery_status == "Authorized"
            assert clients[-1].credentials == expected
            assert watchers[-1].enable_keyboard is False
            await monitor.tick(set(), number * 10 + 1)
            assert clients[-1].closed
        assert len(clients) == 2
        assert messages.count("Last.fm: saved authorization loaded automatically.") == 2
        assert "GitHub:" not in "\n".join(messages)  # Welcome belongs to the visible terminal.
        for secret in (expected.api_key, expected.api_secret, expected.session_key):
            assert secret not in "\n".join(messages)
            assert secret not in (tmp_path / "config.json").read_text()
        assert CredentialStore().load() == expected
    finally:
        await monitor._stop_watcher()
