"""Read-only discovery. No Realm access, game-process inspection or library writes."""

import os
from pathlib import Path
import re

from .archive import archive_ids, metadata_ids, MAX_OSU
from .models import Destination, DownloadError


def discover():
    """Known Windows locations and registered open commands; no recursive disk hunt."""
    roots = set()
    executables = set()
    if os.name == "nt":
        import winreg
        for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
            for key in (r"Software\Classes\osu\shell\open\command",
                        r"Software\Classes\osu!\shell\open\command",
                        r"Software\Classes\osu!lazer\shell\open\command"):
                try:
                    with winreg.OpenKey(hive, key) as entry:
                        command, _ = winreg.QueryValueEx(entry, "")
                    match = re.match(r'^\s*"([^"]+\.exe)"', command, re.I)
                    if match:
                        executables.add(Path(match[1]))
                except OSError:
                    pass
    local = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData/Local"))
    roaming = Path(os.environ.get("APPDATA", Path.home() / "AppData/Roaming"))
    roots.update({local / "osu!", roaming / "osu", local / "osulazer", local / "osu"})
    for executable in executables:
        roots.add(executable.parent)
    results = []
    for root in sorted(roots):
        if (root / "Songs").is_dir():
            exe = root / "osu!.exe"
            results.append(Destination("stable", str(root.resolve()), str(exe) if exe.is_file() else ""))
        if (root / "files").is_dir():
            exe = next((p for p in executables if "lazer" in str(p).casefold()), None)
            results.append(Destination("lazer", str(root.resolve()), str(exe) if exe else ""))
    return results


def _regular_files(root):
    if not root.is_dir():
        raise DownloadError("Library directory is unavailable; no downloads started.")
    # Do not traverse links/junctions. A user-selected root itself may be relocated.
    for directory, folders, files in os.walk(root, followlinks=False):
        folders[:] = [name for name in folders if not (Path(directory) / name).is_symlink()
                      and not getattr(os.lstat(Path(directory) / name), "st_file_attributes", 0) & 0x400]
        for name in files:
            path = Path(directory) / name
            if not path.is_symlink():
                yield path


def scan(destination, *, cache=None, pending=()):
    """Return conservative IDs, including renamed imports and pending archives.

    Lazer's documented hash store contains ordinary .osu data but may retain
    unreferenced/deleted files. Presence means skip conservatively, not proof of
    active Realm membership. We never modify or open Realm.
    """
    destination.validate()
    root = Path(destination.root).resolve()
    ids = set()
    paths = []
    if destination.client == "stable":
        paths.extend(p for p in _regular_files(destination.songs) if p.suffix.casefold() in {".osu", ".osz"})
        downloads = destination.songs.parent / "Downloads"
        if downloads.is_dir():
            paths.extend(p for p in _regular_files(downloads) if p.suffix.casefold() == ".osz")
    else:
        files = root / "files"
        paths.extend(_regular_files(files))
        for folder in (root / "exports", root / "imports"):
            if folder.is_dir():
                paths.extend(p for p in _regular_files(folder) if p.suffix.casefold() in {".osu", ".osz"})
    for folder in pending:
        folder = Path(folder)
        if folder.is_dir():
            paths.extend(p for p in _regular_files(folder) if p.suffix.casefold() == ".osz")
    for path in paths:
        try:
            info = path.stat()
            key = str(path.resolve())
            stamp = (info.st_size, info.st_mtime_ns)
            prior = cache.get(key, stamp) if cache else None
            if prior is not None:
                ids.update(prior)
                continue
            found = set()
            if path.suffix.casefold() == ".osz":
                found = archive_ids(path)
            elif info.st_size <= MAX_OSU:
                with path.open("rb") as file:
                    prefix = file.read(32)
                    if prefix.lstrip(b"\xef\xbb\xbf").startswith(b"osu file format"):
                        file.seek(0)
                        found = metadata_ids(file.read(MAX_OSU))
            if cache:
                cache.put(key, stamp, found)
            ids.update(found)
        except (OSError, ValueError, DownloadError):
            raise DownloadError("An existing library/archive file could not be checked. Library left unchanged; fix access before resuming.") from None
    return ids


def imported_stable_ids(destination):
    # Archives in Songs are pending, not imported. Scan .osu metadata only.
    found = set()
    for path in _regular_files(destination.songs):
        if path.suffix.casefold() == ".osu":
            with path.open("rb") as file:
                found.update(metadata_ids(file.read(MAX_OSU)))
    return found


def imported_stable_file(destination, set_id):
    for path in _regular_files(destination.songs):
        if path.suffix.casefold() == ".osu":
            with path.open("rb") as file:
                if set_id in metadata_ids(file.read(MAX_OSU)):
                    return path
    return None


def scan_all(destinations, *, cache=None, pending=()):
    found, seen = set(), set()
    for destination in destinations:
        key = (destination.client, str(Path(destination.root).resolve()))
        if key not in seen:
            found.update(scan(destination, cache=cache, pending=pending))
            seen.add(key)
    return found
