import asyncio
import hashlib
import io
import json
from pathlib import Path
import stat
import sys
from types import SimpleNamespace
from zipfile import ZipFile, ZipInfo

import httpx
import pytest

from osu_scrobbler import __version__
from osu_scrobbler.config import Config, WatchLock
from osu_scrobbler.updater import (Release, Updater, UpdateError, RELEASE_API, environment_python,
                                   release_from_response, startup, update, verify_wheel)


def wheel(number="1.0.0", extra=None):
    content = io.BytesIO()
    with ZipFile(content, "w") as archive:
        archive.writestr("osu_scrobbler/__init__.py", f'__version__ = "{number}"\n')
        archive.writestr(f"osu_scrobbler-{number}.dist-info/METADATA",
                         f"Metadata-Version: 2.1\nName: osu-scrobbler\nVersion: {number}\n")
        if extra:
            archive.writestr(*extra)
    return content.getvalue()


def published(content, number="1.0.0"):
    filename = f"osu_scrobbler-{number}-py3-none-any.whl"
    return {"draft": False, "prerelease": False, "tag_name": "v" + number,
            "assets": [{"name": filename, "size": len(content), "digest": "sha256:" + hashlib.sha256(content).hexdigest(),
                        "browser_download_url": f"https://github.com/drmhearts/osu-scrobbler-2026/releases/download/v{number}/{filename}"}]}


class Installer:
    def __init__(self, fail=None):
        self.commands = []
        self.fail = fail
    async def __call__(self, command, **options):
        self.commands.append(list(map(str, command)))
        if len(self.commands) == self.fail:
            raise UpdateError("Installation failed")
        if command[1:3] == ["-m", "venv"]:
            environment = Path(command[3])
            executable = environment_python(environment)
            executable.parent.mkdir(parents=True)
            executable.write_text("fixture interpreter")
            if sys.platform == "win32":
                environment_python(environment, hidden=True).write_text("fixture interpreter")


def network(content, data=None, *, redirect=None):
    requests = []
    def handler(request):
        requests.append(request)
        if str(request.url) == RELEASE_API:
            return httpx.Response(200, json=data if data is not None else published(content))
        if request.url.host == "github.com" and redirect:
            return httpx.Response(302, headers={"Location": redirect})
        return httpx.Response(200, content=content)
    return httpx.AsyncClient(transport=httpx.MockTransport(handler)), requests


async def test_installs_verified_release_separately_without_touching_user_data(tmp_path):
    content = wheel()
    client, requests = network(content, redirect="https://release-assets.githubusercontent.com/github-production-release-asset/fixture?token=download")
    runner = Installer()
    messages = []
    updater = Updater(tmp_path, client=client, runner=runner, output=messages.append)
    Config().save(tmp_path)
    untouched = {}
    for filename in ("history.db", "musicbrainz.db", "scrobbles.txt", "credentials-fixture"):
        value = b"Private data must stay unchanged"
        (tmp_path / filename).write_bytes(value)
        untouched[filename] = value
    untouched["config.json"] = (tmp_path / "config.json").read_bytes()
    try:
        reply = await update(tmp_path, updater=updater)
        assert "Updated to 1.0.0" in reply["message"]
        current = updater.current()
        assert current["version"] == "1.0.0"
        assert (updater.root / current["environment"]).is_dir()
        assert json.loads((updater.root / "previous.json").read_text()) == {"bootstrap": True}
        assert len(runner.commands) == 3
        assert "--only-binary=:all:" in runner.commands[1]
        assert all((tmp_path / name).read_bytes() == value for name, value in untouched.items())
        assert [request.url.host for request in requests] == ["api.github.com", "github.com", "release-assets.githubusercontent.com"]
        assert all("Authorization" not in request.headers and not request.content for request in requests)
        assert "Private" not in "\n".join(messages)
        assert not list(updater.root.rglob("*.whl"))
    finally:
        await client.aclose()


@pytest.mark.parametrize("failure", [1, 2, 3])
async def test_failed_environment_pip_or_smoke_preserves_previous_release(tmp_path, failure):
    updater = Updater(tmp_path, runner=Installer())
    content = wheel()
    await updater.install(release_from_response(published(content)), content)
    before = (updater.root / "current.json").read_bytes()
    new_content = wheel("1.0.1")
    updater.runner = Installer(fail=failure)
    with pytest.raises(UpdateError):
        await updater.install(release_from_response(published(new_content, "1.0.1")), new_content)
    assert (updater.root / "current.json").read_bytes() == before
    assert updater.current()["version"] == "1.0.0"
    assert len(list((updater.root / "versions").iterdir())) == 1


async def test_checks_only_newer_stable_versions_and_never_installs_when_check_only(tmp_path):
    content = wheel()
    client, requests = network(content)
    updater = Updater(tmp_path, client=client, runner=Installer())
    try:
        response = await update(tmp_path, updater=updater, check_only=True)
        assert "available: 1.0.0" in response["message"]
        assert not updater.current()
        assert len(requests) == 1
        assert await updater.latest("1.0.0") is None
        assert await updater.latest("2.0.0") is None
        assert not updater.runner.commands
    finally:
        await client.aclose()


@pytest.mark.parametrize("change", ["digest", "size", "url", "name", "assets", "tag", "prerelease", "draft"])
def test_rejects_untrusted_or_unverifiable_release_metadata(change):
    data = published(wheel())
    if change == "digest":
        data["assets"][0]["digest"] = None
    elif change == "size":
        data["assets"][0]["size"] = 99 * 1024 * 1024
    elif change == "url":
        data["assets"][0]["browser_download_url"] = "https://example.com/malware.whl"
    elif change == "name":
        data["assets"][0]["name"] = "other.whl"
    elif change == "assets":
        data["assets"] = None
    elif change == "tag":
        data["tag_name"] = "v1.0.0/../../data"
    else:
        data[change] = True
    with pytest.raises(UpdateError):
        release_from_response(data)


@pytest.mark.parametrize("path", ["../credentials", "/absolute", "C:/absolute", "folder\\..\\credentials"])
def test_rejects_wheels_with_paths_outside_the_isolated_environment(path):
    content = wheel(extra=(path, "payload"))
    with pytest.raises(UpdateError, match="unsafe path"):
        verify_wheel(content, release_from_response(published(content)))


def test_rejects_symlink_wrong_identity_and_checksum_mismatch():
    link = ZipInfo("link")
    link.create_system = 3
    link.external_attr = (stat.S_IFLNK | 0o777) << 16
    content = wheel(extra=(link, "../../credentials"))
    with pytest.raises(UpdateError, match="unsafe path"):
        verify_wheel(content, release_from_response(published(content)))
    content = wheel()
    release = release_from_response(published(content))
    with pytest.raises(UpdateError, match="checksum"):
        verify_wheel(content[:-1] + b"x", release)
    with pytest.raises(UpdateError):
        verify_wheel(wheel("1.0.1"), release_from_response(published(wheel("1.0.1"))))


async def test_offline_or_corrupt_update_does_not_prevent_startup(tmp_path, monkeypatch):
    import osu_scrobbler.updater as module
    async def offline(*args, **kwargs):
        raise httpx.ConnectError("credential-bearing proxy URL must not be shown")
    monkeypatch.setattr(module, "update", offline)
    messages = []
    assert not await startup(tmp_path, command="watch", output=messages.append)
    assert "continuing with the installed app" in messages[0]
    assert "proxy URL" not in messages[0]
    Config(auto_update=False).save(tmp_path)
    messages.clear()
    assert not await startup(tmp_path, command="watch", output=messages.append)
    assert messages == []


async def test_corrupt_cached_runtime_is_not_relaunched_after_successful_check(tmp_path, monkeypatch):
    import osu_scrobbler.updater as module
    content = wheel()
    updater = Updater(tmp_path, runner=Installer())
    await updater.install(release_from_response(published(content)), content)
    async def broken(*args, **kwargs):
        raise UpdateError("Broken runtime")
    async def latest(*args, **kwargs):
        return None
    async def forbidden(*args, **kwargs):
        pytest.fail("An invalid cached runtime must not be started")
    monkeypatch.setattr(updater, "smoke", broken)
    monkeypatch.setattr(updater, "latest", latest)
    monkeypatch.setattr(module, "Updater", lambda *args, **kwargs: updater)
    monkeypatch.setattr(module, "handoff", forbidden)
    assert not await startup(tmp_path, command="watch")


@pytest.mark.parametrize("redirect", ["https://example.com/update", "http://github.com/update", "file:///private"])
async def test_download_never_contacts_untrusted_redirects(tmp_path, redirect):
    content = wheel()
    client, requests = network(content, redirect=redirect)
    updater = Updater(tmp_path, client=client)
    try:
        with pytest.raises(UpdateError, match="unsupported host"):
            await updater.download(release_from_response(published(content)))
        assert len(requests) == 1 and requests[0].url.host == "github.com"
    finally:
        await client.aclose()


async def test_runtime_switch_uses_verified_python_and_only_skips_duplicate_check(tmp_path, monkeypatch):
    import osu_scrobbler.updater as module
    updater = Updater(tmp_path, runner=Installer())
    content = wheel()
    await updater.install(release_from_response(published(content)), content)
    current = updater.current()
    calls = []
    async def handoff(environment, directory, command, **options):
        calls.append((environment, directory, command, options))
        return True
    monkeypatch.setattr(module, "Updater", lambda *args, **kwargs: updater)
    monkeypatch.setattr(module, "handoff", handoff)
    async def checked(*args, **kwargs):
        return {"message": "Up to date"}
    monkeypatch.setattr(module, "update", checked)
    assert await startup(tmp_path, command="tray")
    assert calls == [(updater.root / current["environment"], tmp_path, "tray", {"json_output": False, "skip": True, "dry_run": False, "fixture": None})]
    calls.clear()
    assert not await startup(tmp_path, command="tray", skip=True)
    assert calls == []
    assert await startup(tmp_path, command="watch", check=False, dry_run=True, fixture=tmp_path / "demo.json")
    assert calls[0][3] == {"json_output": False, "skip": True, "dry_run": True, "fixture": tmp_path / "demo.json"}


async def test_failed_cached_tray_returns_to_original_runtime(tmp_path, monkeypatch):
    import osu_scrobbler.updater as module
    updater = Updater(tmp_path, runner=Installer())
    content = wheel()
    await updater.install(release_from_response(published(content)), content)
    monkeypatch.setattr(module, "Updater", lambda *args, **kwargs: updater)
    async def failed(*args, **kwargs):
        raise UpdateError("tray unavailable")
    monkeypatch.setattr(module, "handoff", failed)
    messages = []
    assert not await startup(tmp_path, command="tray", check=False, output=messages.append)
    assert any("original installation" in message for message in messages)


async def test_rollback_retains_both_valid_releases_and_disables_reinstall(tmp_path):
    updater = Updater(tmp_path, runner=Installer())
    Config().save(tmp_path)
    for number in ("1.0.0", "1.0.1"):
        content = wheel(number)
        await updater.install(release_from_response(published(content, number)), content)
    assert updater.current()["version"] == "1.0.1"
    reply = await update(tmp_path, updater=updater, rollback=True)
    assert "Restored 1.0.0" in reply["message"]
    assert updater.current()["version"] == "1.0.0"
    assert Config.load(tmp_path).auto_update is False
    assert len(list((updater.root / "versions").iterdir())) == 2


async def test_old_update_environments_are_pruned_but_user_files_are_not(tmp_path):
    updater = Updater(tmp_path, runner=Installer())
    for number in ("1.0.0", "1.0.1", "1.0.2"):
        content = wheel(number)
        await updater.install(release_from_response(published(content, number)), content)
    assert updater.current()["version"] == "1.0.2"
    assert updater.current("previous.json")["version"] == "1.0.1"
    assert len(list((updater.root / "versions").iterdir())) == 2
    user_folder = updater.root / "versions/my-notes"
    user_folder.mkdir()
    (user_folder / "notes.txt").write_text("Keep this")
    updater.prune()
    assert (user_folder / "notes.txt").read_text() == "Keep this"


async def test_updates_wait_for_running_watcher_and_reject_pointer_traversal(tmp_path):
    updater = Updater(tmp_path, runner=Installer())
    with WatchLock(tmp_path):
        with pytest.raises(UpdateError, match="already running"):
            await update(tmp_path, updater=updater)
    assert updater.runner.commands == []
    updater.root.mkdir(exist_ok=True)
    (updater.root / "current.json").write_text(json.dumps({"version": "1.0.0", "environment": "../other"}))
    assert updater.current() is None


async def test_handoff_keeps_profile_cleans_python_paths_and_waits_for_manual_watch(tmp_path, monkeypatch):
    import osu_scrobbler.updater as module
    calls = []
    waits = []
    async def wait():
        waits.append(True)
        return 0
    async def spawn(*arguments, **options):
        calls.append((arguments, options))
        return SimpleNamespace(wait=wait)
    monkeypatch.setenv("PYTHONPATH", "unverified-checkout")
    monkeypatch.setenv("PYTHONHOME", "unverified-home")
    monkeypatch.setattr(module.asyncio, "create_subprocess_exec", spawn)
    environment = tmp_path / "managed/venv"
    assert await module.handoff(environment, tmp_path, "watch", json_output=True, skip=True)
    arguments, options = calls[0]
    assert arguments[0] == str(environment_python(environment))
    assert "--data-dir" in arguments and str(tmp_path.resolve()) in arguments
    assert "--json" in arguments and "--skip-update-check" in arguments
    assert "PYTHONPATH" not in options["env"] and "PYTHONHOME" not in options["env"]
    assert waits == [True]
