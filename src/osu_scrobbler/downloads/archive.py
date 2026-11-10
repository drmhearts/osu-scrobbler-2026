"""Read-only metadata inspection and full validation; never extract into a library."""

import os
from pathlib import Path
import re
import stat
import uuid
import zlib
from zipfile import BadZipFile, ZipFile

from .models import DownloadError

MAX_ARCHIVE = 2 * 1024**3
MAX_EXPANDED = 8 * 1024**3
MAX_OSU = 16 * 1024**2
SET_ID = re.compile(r"(?m)^\s*BeatmapSetID\s*:\s*(\d+)\s*$")


def metadata_ids(data):
    text = data.decode("utf-8-sig", errors="replace")
    return {int(value) for value in SET_ID.findall(text) if int(value) > 0}


def archive_ids(path, *, expected=None, full=False, check=lambda: None):
    found = set()
    try:
        if Path(path).stat().st_size > MAX_ARCHIVE:
            raise DownloadError("Archive exceeds the 2 GiB safety limit.")
        with ZipFile(path) as archive:
            entries = archive.infolist()
            if len(entries) > 20000 or sum(entry.file_size for entry in entries) > MAX_EXPANDED:
                raise DownloadError("Archive exceeds validation limits.")
            names = set()
            for entry in entries:
                check()
                name = entry.filename.replace("\\", "/")
                normalized = name.casefold()
                if (name.startswith("/") or re.match(r"^[A-Za-z]:", name)
                        or any(part in {".", "..", ""} or part.rstrip(" .") != part
                               or re.fullmatch(r"(?:con|prn|aux|nul|com[1-9]|lpt[1-9])(?:\..*)?", part, re.I)
                               for part in name.rstrip("/").split("/"))
                        or ":" in name or any(ord(c) < 32 for c in name)
                        or stat.S_ISLNK(entry.external_attr >> 16) or entry.flag_bits & 1
                        or normalized in names):
                    raise DownloadError("Unsafe or ambiguous archive entry; nothing was imported.")
                names.add(normalized)
                if name.lower().endswith(".osu"):
                    if entry.file_size > MAX_OSU:
                        raise DownloadError("Beatmap metadata exceeds validation limits.")
                    data = archive.read(entry)
                    if not data.lstrip(b"\xef\xbb\xbf").startswith(b"osu file format"):
                        raise DownloadError("Invalid beatmap metadata.")
                    ids = metadata_ids(data)
                    if expected is not None and ids != {expected}:
                        raise DownloadError("Archive beatmapset ID differs from the requested set.")
                    found.update(ids)
                if full and not entry.is_dir():
                    with archive.open(entry) as file:
                        while file.read(1024 * 1024):
                            check()  # ZipFile verifies CRC while streaming to EOF.
            if not found:
                raise DownloadError("No online beatmapset ID could be verified in this archive.")
        return found
    except DownloadError:
        raise
    except (BadZipFile, RuntimeError, NotImplementedError, EOFError, zlib.error):
        raise DownloadError("Incomplete or invalid .osz archive; nothing was imported.") from None


def publish(source, target, expected, *, check=lambda: None):
    """Stage on target volume, fsync, then publish atomically without overwrite."""
    source, target = Path(source), Path(target)
    archive_ids(source, expected=expected, full=True, check=check)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.parent / (".life-hacks-" + uuid.uuid4().hex + ".part")
    try:
        with source.open("rb") as src, temporary.open("xb") as dst:
            while chunk := src.read(1024 * 1024):
                check()
                dst.write(chunk)
            dst.flush()
            os.fsync(dst.fileno())
        try:
            check()
            if os.name == "nt":
                os.rename(temporary, target)  # Windows refuses an existing target.
            else:
                os.link(temporary, target)
        except FileExistsError:
            archive_ids(target, expected=expected, full=True)
    finally:
        temporary.unlink(missing_ok=True)  # Only our uniquely named staging file.
    return target
