"""Verified public releases, isolated installs, and a stable startup launcher."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from email.parser import Parser
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import time
from urllib.parse import urlsplit
from zipfile import ZipFile, BadZipFile

import httpx

from . import __version__
from .config import Config, WatchLock

REPOSITORY = "drmhearts/osu-scrobbler-2026"
RELEASE_API = f"https://api.github.com/repos/{REPOSITORY}/releases/latest"
MAX_WHEEL = 8 * 1024 * 1024
DOWNLOAD_HOSTS = {"github.com", "release-assets.githubusercontent.com", "objects.githubusercontent.com"}


class UpdateError(RuntimeError):
    """Messages contain no raw network URLs, credentials or subprocess output."""


def version(value):
    if not isinstance(value, str) or not re.fullmatch(r"(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)", value):
        raise UpdateError("Unsupported release version; keeping the installed app.")
    return tuple(int(part) for part in value.split("."))


@dataclass(frozen=True)
class Release:
    version: str
    filename: str
    url: str
    digest: str
    size: int


def release_from_response(data):
    if not isinstance(data, dict) or data.get("draft") is not False or data.get("prerelease") is not False:
        raise UpdateError("No supported stable release; keeping the installed app.")
    tag = data.get("tag_name", "")
    if not isinstance(tag, str) or not tag.startswith("v"):
        raise UpdateError("Invalid release tag; keeping the installed app.")
    number = tag[1:]
    version(number)
    listed_assets = data.get("assets")
    if not isinstance(listed_assets, list):
        raise UpdateError("Invalid release asset list.")
    portable = "osu-life-hacks-windows.zip"
    filename = (portable if sys.platform == "win32" and any(
        isinstance(asset, dict) and asset.get("name") == portable for asset in listed_assets)
        else f"osu_life_hacks-{number}-py3-none-any.whl")
    assets = [asset for asset in listed_assets if isinstance(asset, dict) and asset.get("name") == filename]
    if len(assets) != 1:
        raise UpdateError("Release has no matching update package; keeping the installed app.")
    asset = assets[0]
    expected = f"https://github.com/{REPOSITORY}/releases/download/{tag}/{filename}"
    digest = asset.get("digest", "")
    size = asset.get("size")
    if (asset.get("browser_download_url") != expected or not isinstance(digest, str)
            or not re.fullmatch(r"sha256:[a-f0-9]{64}", digest)
            or type(size) is not int or not 0 < size <= package_limit(filename)):
        raise UpdateError("Release package verification data is invalid; keeping the installed app.")
    return Release(number, filename, expected, digest.removeprefix("sha256:"), size)


def package_limit(filename):
    from .portable import MAX_BUNDLE
    return MAX_BUNDLE if filename == "osu-life-hacks-windows.zip" else MAX_WHEEL


def verify_package(content, release):
    if release.filename == "osu-life-hacks-windows.zip":
        from .portable import verify_archive
        verify_archive(content, release)
    else:
        verify_wheel(content, release)


def verify_wheel(content, release):
    if len(content) > MAX_WHEEL or (release.size and len(content) != release.size) or hashlib.sha256(content).hexdigest() != release.digest:
        raise UpdateError("Update checksum did not match; keeping the installed app.")
    try:
        with ZipFile(io.BytesIO(content)) as wheel:
            entries = wheel.infolist()
            names = [entry.filename for entry in entries]
            if len(entries) > 1000 or sum(entry.file_size for entry in entries) > 32 * 1024 * 1024:
                raise UpdateError("Update package is too large; keeping the installed app.")
            if len(set(name.casefold() for name in names)) != len(names):
                raise UpdateError("Update package contains duplicate paths.")
            for entry in entries:
                path = PurePosixPath(entry.filename)
                if (path.is_absolute() or ".." in path.parts or "\\" in entry.orig_filename or "\x00" in entry.orig_filename
                        or ":" in entry.filename or stat.S_ISLNK(entry.external_attr >> 16)):
                    raise UpdateError("Update package contains an unsafe path.")
            metadata_name = f"osu_life_hacks-{release.version}.dist-info/METADATA"
            metadata_entry = wheel.getinfo(metadata_name)
            if metadata_entry.file_size > 64 * 1024 or "osu_scrobbler/__init__.py" not in names:
                raise UpdateError("Update package metadata is invalid.")
            metadata = Parser().parsestr(wheel.read(metadata_name).decode("utf-8"))
            if metadata.get("Name") != "osu-life-hacks" or metadata.get("Version") != release.version:
                raise UpdateError("Update package identity does not match the release.")
    except (BadZipFile, KeyError, UnicodeError, OSError) as error:
        raise UpdateError("Update package is damaged; keeping the installed app.") from error


def environment_python(environment, *, hidden=False):
    if environment.name == "runtime":
        return environment / ("pythonw.exe" if hidden else "python.exe")
    if sys.platform == "win32":
        return environment / "Scripts" / ("pythonw.exe" if hidden else "python.exe")
    return environment / "bin/python"


async def run_process(command, *, timeout=300):
    # No shell, no prompts, and no saved pip logs / arbitrary error text.
    environment = os.environ.copy()
    environment.pop("PYTHONPATH", None)
    environment.pop("PYTHONHOME", None)
    process = await asyncio.create_subprocess_exec(*map(str, command), env=environment, stdin=subprocess.DEVNULL,
                                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                                 **({"creationflags": subprocess.CREATE_NO_WINDOW} if sys.platform == "win32" else {}))
    try:
        result = await asyncio.wait_for(process.wait(), timeout)
    except (asyncio.TimeoutError, asyncio.CancelledError):
        if process.returncode is None:
            process.kill()
        await process.wait()
        raise
    if result:
        raise UpdateError("Update installation failed; keeping the installed app.")


def _write_json(path, value):
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(value) + "\n", "utf-8")
    temporary.replace(path)


class Updater:
    def __init__(self, directory, *, client=None, runner=run_process, output=lambda _: None):
        self.root = Path(directory) / "updates"
        self.client = client
        self.runner = runner
        self.output = output

    def current(self, filename="current.json"):
        try:
            data = json.loads((self.root / filename).read_text("utf-8"))
            version(data["version"])
            relative = data["environment"]
            if not isinstance(relative, str) or not re.fullmatch(r"versions/[0-9.]+-[a-z0-9_\-]+/(?:venv|runtime)", relative):
                return None
            environment = self.root / relative
            if (environment.resolve() != self.root.resolve() / relative or not environment_python(environment).is_file()
                    or (sys.platform == "win32" and not environment_python(environment, hidden=True).is_file())
                    or not (environment.parent / "ready.json").is_file()):
                return None
            marker = json.loads((environment.parent / "ready.json").read_text("utf-8"))
            if marker != data:
                return None
            return data
        except (OSError, ValueError, TypeError, KeyError, UpdateError):
            return None

    async def latest(self, installed=__version__):
        if self.client is None:
            async with httpx.AsyncClient(timeout=httpx.Timeout(5, connect=3), follow_redirects=False,
                                         headers={"User-Agent": f"osu-life-hacks/{__version__}", "Accept": "application/vnd.github+json"}) as client:
                self.client = client
                try:
                    return await self.latest(installed)
                finally:
                    self.client = None
        try:
            return await self._api_latest(installed)
        except (httpx.HTTPError, ValueError, UpdateError):
            return await self._public_latest(installed)

    async def _api_latest(self, installed):
        async with self.client.stream("GET", RELEASE_API) as response:
            if response.status_code == 404:
                return None  # A repository with no release is a normal first install.
            response.raise_for_status()
            content = bytearray()
            async for chunk in response.aiter_bytes():
                content.extend(chunk)
                if len(content) > 256 * 1024:
                    raise UpdateError("Release information is too large.")
            data = json.loads(content)
        if not isinstance(data, dict):
            raise UpdateError("Invalid release information.")
        tag = data.get("tag_name", "")
        if isinstance(tag, str) and tag.startswith("v") and version(tag[1:]) <= version(installed):
            return None
        return release_from_response(data)

    async def _public_latest(self, installed):
        # Public release downloads work even when the unauthenticated API is limited.
        response = await self.client.get(f"https://github.com/{REPOSITORY}/releases/latest")
        location = response.headers.get("location", "")
        if location.startswith("/"):
            location = "https://github.com" + location
        prefix = f"https://github.com/{REPOSITORY}/releases/tag/v"
        if response.status_code not in {301, 302, 303, 307, 308} or not location.startswith(prefix):
            raise UpdateError("Latest release is unavailable.")
        number = location[len(prefix):]
        if version(number) <= version(installed):
            return None
        base = f"https://github.com/{REPOSITORY}/releases/download/v{number}/"
        checksums = await self._download_bytes(base + "SHA256SUMS.txt", 8192)
        rows = [line.split() for line in checksums.decode("utf-8").splitlines()]
        portable = "osu-life-hacks-windows.zip"
        filename = (portable if sys.platform == "win32" and any(len(row) == 2 and row[1] == portable for row in rows)
                    else f"osu_life_hacks-{number}-py3-none-any.whl")
        hashes = [row[0] for row in rows if len(row) == 2 and row[1] == filename]
        if len(hashes) != 1 or not re.fullmatch(r"[a-f0-9]{64}", hashes[0]):
            raise UpdateError("Release checksums are invalid.")
        return Release(number, filename, base + filename, hashes[0], 0)

    async def _download_bytes(self, url, limit):
        for _ in range(5):
            parsed = urlsplit(url)
            if parsed.scheme != "https" or parsed.hostname not in DOWNLOAD_HOSTS or parsed.username or parsed.password or parsed.port not in {None, 443}:
                raise UpdateError("Update download redirected to an unsupported host.")
            async with self.client.stream("GET", url, headers={"Accept-Encoding": "identity"}) as response:
                if response.status_code in {301, 302, 303, 307, 308}:
                    url = response.headers.get("location", "")
                    continue
                response.raise_for_status()
                content = bytearray()
                async for chunk in response.aiter_bytes():
                    content.extend(chunk)
                    if len(content) > limit:
                        raise UpdateError("Update download is too large.")
                return bytes(content)
        raise UpdateError("Too many update redirects.")

    async def download(self, release):
        if self.client is None:
            async with httpx.AsyncClient(timeout=httpx.Timeout(20, connect=5), follow_redirects=False,
                                         headers={"User-Agent": f"osu-life-hacks/{__version__}", "Accept-Encoding": "identity"}) as client:
                self.client = client
                try:
                    return await self.download(release)
                finally:
                    self.client = None
        url = release.url
        for _ in range(5):
            parsed = urlsplit(url)
            if parsed.scheme != "https" or parsed.hostname not in DOWNLOAD_HOSTS or parsed.username or parsed.password or parsed.port not in {None, 443}:
                raise UpdateError("Update download redirected to an unsupported host.")
            async with self.client.stream("GET", url, headers={"Accept-Encoding": "identity"}) as response:
                if response.status_code in {301, 302, 303, 307, 308}:
                    url = response.headers.get("location", "")
                    continue
                response.raise_for_status()
                content = bytearray()
                async for chunk in response.aiter_bytes():
                    content.extend(chunk)
                    if (release.size and len(content) > release.size) or len(content) > package_limit(release.filename):
                        raise UpdateError("Update download exceeded its expected size.")
                verify_package(content, release)
                return bytes(content)
        raise UpdateError("Too many update download redirects.")

    async def install(self, release, content):
        verify_package(content, release)
        versions = self.root / "versions"
        versions.mkdir(parents=True, exist_ok=True)
        staged = Path(tempfile.mkdtemp(prefix=release.version + "-", dir=versions))
        portable = release.filename == "osu-life-hacks-windows.zip"
        environment = staged / ("runtime" if portable else "venv")
        activated = False
        try:
            self.output(f"Installing osu! life hacks {release.version}…")
            if portable:
                from .portable import PREFIX
                environment.mkdir()
                with ZipFile(io.BytesIO(content)) as archive:
                    for entry in archive.infolist():
                        if entry.filename.startswith(PREFIX) and not entry.is_dir():
                            target = environment / entry.filename[len(PREFIX):]
                            target.parent.mkdir(parents=True, exist_ok=True)
                            target.write_bytes(archive.read(entry))
            else:
                package = staged / release.filename
                package.write_bytes(content)
                await self.runner([sys.executable, "-m", "venv", str(environment)])
                executable = environment_python(environment)
                await self.runner([executable, "-m", "pip", "--disable-pip-version-check", "install", "--no-input",
                                   "--no-cache-dir", "--only-binary=:all:", str(package)])
                package.unlink()
            await self.smoke(environment, release.version)
            data = self.activate(environment, release.version, release.digest)
            activated = True
            self.prune()
            self.output(f"Updated to {release.version}. Settings and Last.fm authorization kept.")
            return data
        except BaseException:
            if not activated:
                shutil.rmtree(staged, ignore_errors=True)
            raise

    def activate(self, environment, number, digest):
        data = {"version": number, "environment": environment.relative_to(self.root).as_posix(), "digest": digest}
        _write_json(environment.parent / "ready.json", data)
        previous = self.current()
        _write_json(self.root / "previous.json", previous or {"bootstrap": True})
        _write_json(self.root / "current.json", data)
        return data

    async def install_directory(self, runtime, manifest, digest):
        versions = self.root / "versions"
        versions.mkdir(parents=True, exist_ok=True)
        staged = Path(tempfile.mkdtemp(prefix=manifest["version"] + "-", dir=versions))
        environment = staged / "runtime"
        activated = False
        try:
            environment.mkdir()
            for name in [*manifest["files"], "portable.json"]:
                target = environment / name
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(runtime / name, target)
            # Re-check the copy, including cross-volume installs and paths with spaces.
            from .portable import verify_directory, install_launcher
            verify_directory(environment, manifest["version"])
            await self.smoke(environment, manifest["version"])
            data = self.activate(environment, manifest["version"], digest)
            activated = True
            install_launcher(self.root.parent)
            self.prune()
            return data
        finally:
            if not activated:
                shutil.rmtree(staged, ignore_errors=True)

    async def smoke(self, environment, number):
        script = ("from osu_scrobbler import __version__; "
                  "from osu_scrobbler import cli, watcher, tray, console; "
                  f"assert __version__ == {number!r}; "
                  "import httpx, keyring, unidecode, sys; "
                  "exec('if sys.platform == \"win32\":\\n import psutil, pystray, PIL')")
        await self.runner([environment_python(environment), "-c", script], timeout=20)

    def prune(self):
        """Keep current/previous runtimes and never delete an unowned directory."""
        protected = {Path(sys.prefix).resolve().parent}
        for filename in ("current.json", "previous.json"):
            pointer = self.current(filename)
            if pointer:
                protected.add((self.root / pointer["environment"]).resolve().parent)
        try:
            candidates = list((self.root / "versions").iterdir())
        except OSError:
            return
        for candidate in candidates:
            if candidate.is_symlink() or candidate.resolve() in protected or not candidate.is_dir():
                continue
            try:
                marker = json.loads((candidate / "ready.json").read_text("utf-8"))
                version(marker["version"])
                owned = marker.get("environment") in {candidate.relative_to(self.root).as_posix() + "/venv",
                                                       candidate.relative_to(self.root).as_posix() + "/runtime"}
                if owned:
                    shutil.rmtree(candidate, ignore_errors=True)
            except (OSError, ValueError, TypeError, KeyError, UpdateError):
                continue

    def rollback(self):
        previous = self.current("previous.json")
        if previous:
            _write_json(self.root / "current.json", previous)
            return previous["version"]
        try:
            bootstrap = json.loads((self.root / "previous.json").read_text("utf-8")).get("bootstrap") is True
        except (OSError, ValueError, AttributeError):
            bootstrap = False
        if bootstrap:
            (self.root / "current.json").unlink(missing_ok=True)
            return "original installation"
        raise UpdateError("No previous installation is available.")


async def stop_running(directory):
    from .ipc import send_command
    for control in ("tray-control.json", "control.json"):
        try:
            await asyncio.wait_for(send_command(directory, {"command": "quit"}, control_name=control), 2)
        except (OSError, RuntimeError, ValueError, asyncio.TimeoutError):
            pass
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        try:
            with WatchLock(directory, "tray.lock"), WatchLock(directory):
                return
        except RuntimeError:
            await asyncio.sleep(0.25)
    raise UpdateError("The app is still stopping. Try START.cmd again.")


async def update(directory, *, check_only=False, rollback=False, output=lambda _: None, updater=None, restart_running=False):
    updater = updater or Updater(directory, output=output)
    updater.root.mkdir(parents=True, exist_ok=True)
    if check_only:
        current = updater.current()
        installed = current["version"] if current else __version__
        release = await updater.latest(installed)
        return {"message": f"Update available: {release.version}." if release else f"Up to date: {installed}."}
    release = None
    if not restart_running:
        try:
            with WatchLock(directory, "tray.lock"), WatchLock(directory):
                pass
        except RuntimeError as error:
            raise UpdateError("An app is already running; stop it before updating.") from error
    if not rollback:
        current = updater.current()
        installed = current["version"] if current else __version__
        release = await updater.latest(installed)
        if not release:
            return {"message": f"Up to date: {installed}."}
        if restart_running:
            await stop_running(directory)
    try:
        with WatchLock(directory, "tray.lock"), WatchLock(directory), WatchLock(updater.root, "update.lock"):
            if rollback:
                restored = updater.rollback()
                config = Config.load(directory)
                config.auto_update = False
                config.save(directory)
                return {"message": f"Restored {restored}. Start the app again; automatic checks are now disabled."}
            current = updater.current()
            installed = current["version"] if current else __version__
            if version(release.version) <= version(installed):
                return {"message": f"Up to date: {installed}."}
            content = await updater.download(release)
            await updater.install(release, content)
            return {"message": f"Updated to {release.version}. Start the app to use it."}
    except RuntimeError as error:
        if isinstance(error, UpdateError):
            raise
        raise UpdateError("An app or update is already running; changes will wait until the next startup.") from error


async def startup(directory, *, command, json_output=False, skip=False, check=True,
                  dry_run=False, fixture=None, output=lambda _: None, enable_startup=False):
    """Return True when a newer isolated installation took over this invocation."""
    if skip:
        return False
    updater = Updater(directory, output=output)
    current = updater.current()
    broken = None
    environment = updater.root / current["environment"] if current else None
    if current and environment.resolve() != Path(sys.prefix).resolve():
        try:
            await updater.smoke(environment, current["version"])
        except (UpdateError, OSError, asyncio.TimeoutError):
            output("Saved update could not start; using the original installation.")
            broken = current
            current = None
        else:
            # Finish update work in this process. The child can then start its
            # icon immediately instead of disappearing into another update check.
            if check and Config.load(directory).auto_update:
                try:
                    await update(directory, updater=updater, output=output, restart_running=command in {"setup", "tray"})
                except (httpx.HTTPError, OSError, ValueError, UpdateError, asyncio.TimeoutError):
                    output("Update check/install unavailable; continuing with the installed app.")
                current = updater.current() or current
                environment = updater.root / current["environment"]
            try:
                return await handoff(environment, directory, command, json_output=json_output, skip=True,
                                     dry_run=dry_run, fixture=fixture, **({"enable_startup": True} if enable_startup else {}))
            except (UpdateError, OSError, RuntimeError, asyncio.TimeoutError):
                if command != "tray":
                    raise
                output("Updated tray could not start; using the original installation.")
                broken = current
                current = None
                return False
    if check and Config.load(directory).auto_update:
        try:
            await update(directory, updater=updater, output=output, restart_running=command in {"setup", "tray"})
        except (httpx.HTTPError, OSError, ValueError, UpdateError, asyncio.TimeoutError):
            output("Update check/install unavailable; continuing with the installed app.")
            return False
        installed = updater.current()
        if installed and installed != broken and (current is None or installed != current):
            environment = updater.root / installed["environment"]
            try:
                return await handoff(environment, directory, command, json_output=json_output, skip=True,
                                     **({"enable_startup": True} if enable_startup else {}))
            except (UpdateError, OSError, RuntimeError, asyncio.TimeoutError):
                if command != "tray":
                    raise
                output("Updated tray could not start; continuing with the original installation.")
                return False
    return False


async def handoff(environment, directory, command, *, json_output=False, skip=False, dry_run=False, fixture=None, enable_startup=False):
    arguments = ["-m", "osu_scrobbler", "--data-dir", str(Path(directory).resolve())]
    if json_output:
        arguments.append("--json")
    if skip:
        arguments.append("--skip-update-check")
    arguments.append(command)
    if enable_startup:
        arguments.append("--enable-startup")
    if dry_run:
        arguments.append("--dry-run")
    if fixture:
        arguments += ["--fixture", str(Path(fixture).resolve())]
    executable = environment_python(environment, hidden=command == "tray")
    # An editable checkout must not shadow the verified wheel in the new process.
    environment_vars = os.environ.copy()
    environment_vars.pop("PYTHONPATH", None)
    environment_vars.pop("PYTHONHOME", None)
    options = {"env": environment_vars, "cwd": str(environment)}
    if command == "tray":
        options.update(stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if sys.platform == "win32":
            options["creationflags"] = subprocess.CREATE_NO_WINDOW
    process = await asyncio.create_subprocess_exec(str(executable), *arguments, **options)
    if command == "tray":
        from .tray import wait_for_tray
        try:
            await wait_for_tray(directory, process)
        except (RuntimeError, OSError, asyncio.TimeoutError) as error:
            if process.returncode is None:
                process.terminate()
                await process.wait()
            raise UpdateError("Updated tray failed to start; the installed app is preserved.") from error
    else:
        if await process.wait():
            raise UpdateError("Updated app exited unsuccessfully. Use update --rollback to restore the previous release.")
    return True
