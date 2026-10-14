"""An optional, managed tosu copy: official download, local-only, no dashboard."""

import hashlib
import io
from pathlib import Path
import shutil
import stat
import sys
import tempfile
from zipfile import ZipFile, BadZipFile

from .config import Config, WatchLock
from .portable import safe_path
from .updater import Updater, UpdateError

TOSU_VERSION = "4.26.2"
TOSU_URL = f"https://github.com/tosuapp/tosu/releases/download/v{TOSU_VERSION}/tosu-windows-v{TOSU_VERSION}.zip"
TOSU_SHA256 = "d34c2bfc495959fcc79ff01f10b7dc9aea77eb508ed23c8f0d0fb8342773faa3"
TOSU_SIZE = 45491440
SETTINGS = """OPEN_DASHBOARD_ON_STARTUP=false
ENABLE_AUTOUPDATE=false
ENABLE_INGAME_OVERLAY=false
ENABLE_KEY_OVERLAY=false
CALCULATE_PP=false
READ_MANIA_SCROLL_SPEED=false
SHOW_MP_COMMANDS=false
DEBUG_LOG=false
SERVER_IP=127.0.0.1
SERVER_PORT=24050
ALLOWED_IPS=127.0.0.1,localhost
POLL_RATE=150
PRECISE_DATA_POLL_RATE=150
"""


def managed_path(directory):
    bundled = Path(sys.prefix) / "tosu/tosu.exe"
    if bundled.is_file() and (Path(sys.prefix) / "portable.json").is_file():
        return bundled
    downloaded = Path(directory) / "dependencies" / f"tosu-{TOSU_VERSION}" / "tosu.exe"
    return downloaded if downloaded.is_file() else None


def prepare_settings(path, config=None):
    from urllib.parse import urlsplit
    port = urlsplit((config or Config()).tosu_url).port or 24050
    content = SETTINGS.replace("SERVER_PORT=24050", f"SERVER_PORT={port}")
    (path.parent / "tosu.env").write_text(content, "utf-8")


async def ensure_tosu(directory, *, client=None):
    config = Config.load(directory)
    if config.tosu_path:
        # An explicitly chosen binary gets its own working folder/settings.
        # Reading/copying that one file leaves the user's other tosu untouched.
        source = Path(config.tosu_path).expanduser()
        if not source.is_file() or source.stat().st_size > 160 * 1024 * 1024:
            raise UpdateError("Configured tosu is unavailable. Check tosu_path or clear it for automatic setup.")
        target = Path(directory) / "dependencies/tosu-configured/tosu.exe"
        target.parent.mkdir(parents=True, exist_ok=True)
        with WatchLock(target.parent, "tosu.lock"):
            content = source.read_bytes()
            if not target.is_file() or hashlib.sha256(target.read_bytes()).digest() != hashlib.sha256(content).digest():
                temporary = target.with_suffix(".installing")
                temporary.write_bytes(content)
                temporary.replace(target)
            prepare_settings(target, config)
        return target
    path = managed_path(directory)
    if path:
        prepare_settings(path, config)
        return path
    import httpx
    if client is None:
        async with httpx.AsyncClient(timeout=httpx.Timeout(30, connect=5), follow_redirects=False) as session:
            return await ensure_tosu(directory, client=session)
    dependencies = Path(directory) / "dependencies"
    dependencies.mkdir(parents=True, exist_ok=True)
    with WatchLock(dependencies, "tosu.lock"):
        path = managed_path(directory)
        if path:
            return path
        try:
            content = await Updater(directory, client=client)._download_bytes(TOSU_URL, TOSU_SIZE)
        except httpx.HTTPError as error:
            raise UpdateError("tosu setup unavailable. Check your connection and reopen START.cmd.") from error
        if len(content) != TOSU_SIZE or hashlib.sha256(content).hexdigest() != TOSU_SHA256:
            raise UpdateError("tosu download checksum did not match. Reopen START.cmd to retry.")
        staged = Path(tempfile.mkdtemp(prefix="tosu-", dir=dependencies))
        try:
            with ZipFile(io.BytesIO(content)) as archive:
                entries = archive.infolist()
                if (len(entries) != 1 or entries[0].filename != "tosu.exe"
                        or entries[0].file_size > 160 * 1024 * 1024
                        or not safe_path(entries[0].filename) or stat.S_ISLNK(entries[0].external_attr >> 16)):
                    raise UpdateError("tosu package contents are invalid.")
                archive.extractall(staged)
            prepare_settings(staged / "tosu.exe", Config.load(directory))
            from importlib.resources import files
            (staged / "LICENSE").write_bytes(files("osu_scrobbler").joinpath("launcher/tosu-LICENSE").read_bytes())
            (staged / "SOURCE.txt").write_text(f"Unmodified tosu {TOSU_VERSION} (LGPL-3.0).\n"
                f"Source: https://github.com/tosuapp/tosu/tree/v{TOSU_VERSION}\n"
                f"https://github.com/tosuapp/tosu/archive/refs/tags/v{TOSU_VERSION}.zip\n", "utf-8")
            destination = dependencies / f"tosu-{TOSU_VERSION}"
            staged.rename(destination)
            return destination / "tosu.exe"
        except (BadZipFile, OSError, UpdateError):
            shutil.rmtree(staged, ignore_errors=True)
            raise
