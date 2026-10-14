"""Validate the bundled Windows runtime before a local install or upgrade."""

import hashlib
import io
import json
from pathlib import Path, PurePosixPath
import re
import stat
from zipfile import ZipFile, BadZipFile

MAX_BUNDLE = 160 * 1024 * 1024
MAX_EXPANDED = 450 * 1024 * 1024
PREFIX = "osu-scrobbler-2026/app/runtime/"


def safe_path(name):
    path = PurePosixPath(name)
    return (bool(name) and not path.is_absolute() and ".." not in path.parts
            and path.as_posix() == name.rstrip("/")
            and not any(char in name for char in '\\:<>"|?*') and not any(ord(char) < 32 for char in name)
            and all(part == part.rstrip(" .") and not re.fullmatch(
                r"(?i)(?:con|prn|aux|nul|com[1-9]|lpt[1-9])(?:\..*)?", part) for part in path.parts))


def manifest(content, number=None):
    from .updater import UpdateError, version
    try:
        data = json.loads(content)
        version(data["version"])
        paths = data["files"]
        if (data.get("name") != "osu-scrobbler" or data.get("schema") != 1
                or (number is not None and data["version"] != number)
                or not isinstance(paths, dict) or not 4 <= len(paths) <= 15000
                or not {"python.exe", "pythonw.exe", "Lib/site-packages/osu_scrobbler/__init__.py"} <= paths.keys()
                or len({name.casefold() for name in paths}) != len(paths)
                or any(not safe_path(name) or not isinstance(digest, str) or not re.fullmatch(r"[a-f0-9]{64}", digest)
                       for name, digest in paths.items())):
            raise ValueError
        return data
    except (ValueError, KeyError, TypeError, AttributeError) as error:
        raise UpdateError("Windows package identity is invalid; keeping the installed app.") from error


def verify_directory(runtime, number=None):
    from .updater import UpdateError
    content = (runtime / "portable.json").read_bytes()
    if len(content) > 2 * 1024 * 1024:
        raise UpdateError("Windows package manifest is too large.")
    data = manifest(content, number)
    size = 0
    for name, digest in data["files"].items():
        path = runtime / name
        if path.resolve() != runtime.resolve() / name or not path.is_file():
            raise UpdateError("Windows package has a missing or redirected file.")
        size += path.stat().st_size
        if size > MAX_EXPANDED or hashlib.sha256(path.read_bytes()).hexdigest() != digest:
            raise UpdateError("Windows package checksum did not match.")
    return data, hashlib.sha256(content).hexdigest()


def verify_archive(content, release):
    from .updater import UpdateError
    if (len(content) > MAX_BUNDLE or (release.size and len(content) != release.size)
            or hashlib.sha256(content).hexdigest() != release.digest):
        raise UpdateError("Windows update checksum did not match; keeping the installed app.")
    try:
        with ZipFile(io.BytesIO(content)) as archive:
            entries = archive.infolist()
            names = [entry.filename for entry in entries]
            if (len(entries) > 15020 or sum(entry.file_size for entry in entries) > MAX_EXPANDED
                    or len({name.casefold() for name in names}) != len(names)):
                raise UpdateError("Windows package is too large or has duplicate files.")
            for entry in entries:
                if not safe_path(entry.filename) or stat.S_ISLNK(entry.external_attr >> 16):
                    raise UpdateError("Windows package contains an unsafe path.")
            info = archive.getinfo(PREFIX + "portable.json")
            if info.file_size > 2 * 1024 * 1024:
                raise UpdateError("Windows package manifest is too large.")
            data = manifest(archive.read(info), release.version)
            listed = {name[len(PREFIX):] for name in names if name.startswith(PREFIX) and not name.endswith("/")}
            if listed != set(data["files"]) | {"portable.json"}:
                raise UpdateError("Windows package contents do not match the manifest.")
            for name, digest in data["files"].items():
                if hashlib.sha256(archive.read(PREFIX + name)).hexdigest() != digest:
                    raise UpdateError("Windows package file checksum did not match.")
    except (BadZipFile, KeyError, UnicodeError, OSError) as error:
        raise UpdateError("Windows package is damaged; keeping the installed app.") from error


async def install_local(directory, runtime, *, updater=None):
    """Copy the verified download into the user profile; no global Python install."""
    from .config import WatchLock
    from .updater import Updater, version
    updater = updater or Updater(directory)
    data, digest = verify_directory(runtime)
    updater.root.mkdir(parents=True, exist_ok=True)
    with WatchLock(updater.root, "update.lock"):
        current = updater.current()
        if current and version(current["version"]) >= version(data["version"]) and current["environment"].endswith("/runtime"):
            return current
        return await updater.install_directory(runtime, data, digest)


def install_launcher(directory):
    """An owned, stable local START survives removal of the downloaded folder."""
    from importlib.resources import files
    assets = files("osu_scrobbler").joinpath("launcher")
    state = directory / "local-launcher-state.json"
    try:
        previous = json.loads(state.read_text("utf-8"))
    except (OSError, ValueError):
        previous = {}
    previous = previous if isinstance(previous, dict) else {}
    installed = {}
    for name in ("START.cmd", "launch.ps1"):
        path = directory / name
        content = assets.joinpath(name).read_bytes()
        digest = hashlib.sha256(content).hexdigest()
        if path.is_symlink() or (path.exists() and hashlib.sha256(path.read_bytes()).hexdigest() not in {previous.get(name), digest}):
            continue
        temporary = path.with_suffix(".updating")
        temporary.write_bytes(content)
        temporary.replace(path)
        installed[name] = digest
    from .updater import _write_json
    _write_json(state, installed)
