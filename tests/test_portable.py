import hashlib
import io
import json
from pathlib import Path
import stat
from zipfile import ZipFile, ZipInfo

import httpx
import pytest

from osu_scrobbler.portable import PREFIX, install_local, install_launcher, verify_archive
from osu_scrobbler.updater import Release, Updater, UpdateError, release_from_response


def runtime(directory, number="1.0.0"):
    directory.mkdir(parents=True)
    files = {"python.exe": b"fixture python", "pythonw.exe": b"fixture hidden python",
             "python3.dll": b"fixture dll", "Lib/site-packages/osu_scrobbler/__init__.py": f'__version__="{number}"'.encode()}
    for name, content in files.items():
        path = directory / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
    data = {"name": "osu-scrobbler", "schema": 1, "version": number,
            "files": {name: hashlib.sha256(content).hexdigest() for name, content in files.items()}}
    (directory / "portable.json").write_text(json.dumps(data))
    return data


def bundle(directory, number="1.0.0", extra=None):
    runtime(directory, number)
    data = io.BytesIO()
    with ZipFile(data, "w") as archive:
        for path in directory.rglob("*"):
            if path.is_file():
                archive.writestr(PREFIX + path.relative_to(directory).as_posix(), path.read_bytes())
        if extra:
            archive.writestr(*extra)
    content = data.getvalue()
    return content, Release(number, "osu-scrobbler-windows.zip", "", hashlib.sha256(content).hexdigest(), len(content))


class Runner:
    def __init__(self, fails=False):
        self.calls = []
        self.fails = fails
    async def __call__(self, arguments, **kwargs):
        self.calls.append(arguments)
        assert Path(arguments[0]).is_file()
        assert arguments[1] == "-c"  # The portable install never runs venv or pip.
        if self.fails:
            raise UpdateError("Runtime could not start")


async def test_local_install_survives_removed_download_and_keeps_user_data(tmp_path):
    import shutil
    source = tmp_path / "An arbitrary folder_日本語/runtime"
    runtime(source)
    profile = tmp_path / "User data with spaces"
    profile.mkdir()
    (profile / "history.db").write_bytes(b"private history")
    (profile / "threshold.json").write_bytes(b'{"mode":"seconds","seconds":42}')
    runner = Runner()
    updater = Updater(profile, runner=runner)
    first = await install_local(profile, source, updater=updater)
    assert updater.current() == first
    assert first["environment"].endswith("/runtime")
    assert len(runner.calls) == 1
    assert await install_local(profile, source, updater=updater) == first
    assert len(runner.calls) == 1
    shutil.rmtree(source.parent)
    assert updater.current() == first
    assert (profile / "START.cmd").is_file() and (profile / "launch.ps1").is_file()
    assert (profile / "history.db").read_bytes() == b"private history"
    assert json.loads((profile / "threshold.json").read_text())["seconds"] == 42


async def test_portable_updates_keep_previous_runtime_and_never_require_system_python(tmp_path):
    profile = tmp_path / "profile"
    runner = Runner()
    updater = Updater(profile, runner=runner)
    content, release = bundle(tmp_path / "first")
    first = await updater.install(release, content)
    assert updater.current() == first
    content, release = bundle(tmp_path / "second", "1.0.1")
    second = await updater.install(release, content)
    assert updater.current() == second
    assert updater.current("previous.json") == first
    assert updater.rollback() == "1.0.0"
    assert updater.current() == first
    assert len(runner.calls) == 2


async def test_failed_or_corrupt_install_keeps_pointer_and_deletes_only_staged_copy(tmp_path):
    updater = Updater(tmp_path / "profile", runner=Runner())
    content, release = bundle(tmp_path / "first")
    previous = await updater.install(release, content)
    updater.runner = Runner(fails=True)
    content, release = bundle(tmp_path / "second", "1.0.1")
    with pytest.raises(UpdateError, match="could not start"):
        await updater.install(release, content)
    assert updater.current() == previous
    assert len(list((updater.root / "versions").iterdir())) == 1
    with pytest.raises(UpdateError, match="checksum"):
        await updater.install(release, content[:-1] + b"x")
    assert updater.current() == previous


@pytest.mark.parametrize("path", ["../outside", "C:/outside", "folder\\evil", "folder/../outside", "CON.txt", "folder./file", "x//file", "x/./file"])
def test_windows_archive_rejects_unsafe_and_ambiguous_paths(tmp_path, path):
    content, release = bundle(tmp_path / "runtime", extra=(path, b"bad"))
    with pytest.raises(UpdateError, match="unsafe path"):
        verify_archive(content, release)


def test_windows_archive_checks_each_file_and_rejects_symlinks(tmp_path):
    link = ZipInfo("folder/link")
    link.external_attr = (stat.S_IFLNK | 0o777) << 16
    content, release = bundle(tmp_path / "first", extra=(link, b"../outside"))
    with pytest.raises(UpdateError, match="unsafe path"):
        verify_archive(content, release)
    source = tmp_path / "second"
    runtime(source)
    (source / "python.exe").write_bytes(b"corrupt")
    content = io.BytesIO()
    with ZipFile(content, "w") as archive:
        for path in source.rglob("*"):
            if path.is_file():
                archive.writestr(PREFIX + path.relative_to(source).as_posix(), path.read_bytes())
    data = content.getvalue()
    release = Release("1.0.0", "osu-scrobbler-windows.zip", "", hashlib.sha256(data).hexdigest(), len(data))
    with pytest.raises(UpdateError, match="file checksum"):
        verify_archive(data, release)


def test_windows_release_selection_prefers_complete_runtime(tmp_path, monkeypatch):
    import osu_scrobbler.updater as module
    monkeypatch.setattr(module.sys, "platform", "win32")
    content, release = bundle(tmp_path / "runtime")
    url = "https://github.com/drmhearts/osu-scrobbler-2026/releases/download/v1.0.0/osu-scrobbler-windows.zip"
    data = {"draft": False, "prerelease": False, "tag_name": "v1.0.0", "assets": [
        {"name": release.filename, "digest": "sha256:" + release.digest, "size": len(content), "browser_download_url": url}]}
    selected = release_from_response(data)
    assert selected.filename == "osu-scrobbler-windows.zip" and selected.url == url


async def test_public_release_fallback_selects_windows_bundle_with_same_checksums(tmp_path, monkeypatch):
    import osu_scrobbler.updater as module
    monkeypatch.setattr(module.sys, "platform", "win32")
    content, release = bundle(tmp_path / "runtime")
    def respond(request):
        if str(request.url) == module.RELEASE_API:
            return httpx.Response(403)
        if str(request.url).endswith("releases/latest"):
            return httpx.Response(302, headers={"Location": "https://github.com/drmhearts/osu-scrobbler-2026/releases/tag/v1.0.0"})
        if str(request.url).endswith("SHA256SUMS.txt"):
            return httpx.Response(200, text=f"{release.digest}  {release.filename}\n")
        return httpx.Response(200, content=content)
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        updater = Updater(tmp_path, client=client)
        selected = await updater.latest("0.3.0")
        assert selected.filename == "osu-scrobbler-windows.zip"
        assert await updater.download(selected) == content


def test_local_owned_launcher_preserves_user_edits(tmp_path):
    install_launcher(tmp_path)
    (tmp_path / "START.cmd").write_text("custom start")
    install_launcher(tmp_path)
    assert (tmp_path / "START.cmd").read_text() == "custom start"
    assert (tmp_path / "launch.ps1").is_file()
