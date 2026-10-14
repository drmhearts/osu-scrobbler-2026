"""Refresh only this app's known launcher files after a verified update."""

import hashlib
from importlib.resources import files
import json
from pathlib import Path
import os
import re
import sys
import tomllib


def repair_launcher(directory, *, root=None, parent_executable=None, assets=None):
    directory = Path(directory)
    assets = assets or files("osu_scrobbler").joinpath("launcher")
    legacy = json.loads(assets.joinpath("legacy.json").read_text("utf-8"))
    legacy_detected = False
    try:
        if root is None:
            try:
                root = Path(json.loads((directory / "install-root.json").read_text("utf-8"))["root"])
            except (OSError, ValueError, KeyError, TypeError):
                if parent_executable is None and sys.platform == "win32":
                    import psutil
                    # Only this Python launch chain (Windows venvs use redirectors).
                    try:
                        process = psutil.Process(os.getppid())
                        for _ in range(4):
                            if process is None or not re.fullmatch(r"pythonw?(?:[0-9.]+)?\.exe", process.name().casefold()):
                                break
                            candidate = Path(process.exe())
                            if candidate.parent.parent.name == ".venv":
                                parent_executable = candidate
                                break
                            process = process.parent()
                    except psutil.Error:
                        return False
                if parent_executable is None:
                    return False
                parent = Path(parent_executable)
                if parent.name.casefold() not in {"python.exe", "pythonw.exe"} or parent.parent.parent.name != ".venv":
                    return False
                root = parent.parents[2]
        root = Path(root).absolute()
        if root.resolve() != root or tomllib.loads((root / "pyproject.toml").read_text("utf-8"))["project"]["name"] != "osu-scrobbler":
            return False
        (directory / "install-root.json").write_text(json.dumps({"root": str(root)}) + "\n", "utf-8")
        try:
            previous = json.loads((directory / "launcher-state.json").read_text("utf-8"))
        except (OSError, ValueError):
            previous = {}
        if not isinstance(previous, dict):
            previous = {}
        repaired_legacy = False
        installed = {}
        for target, asset in (("START.cmd", "START.cmd"), ("tools/start_windows.py", "start_windows.py"), ("README.md", "README.md")):
            path = root / target
            if target in {"START.cmd", "README.md"} and root.name == "app" and not path.exists():
                path = root.parent / target
            if path.is_symlink() or not path.is_file():
                continue
            old = hashlib.sha256(path.read_bytes()).hexdigest()
            content = assets.joinpath(asset).read_bytes()
            digest = hashlib.sha256(content).hexdigest()
            originals = legacy.get(target)
            originals = originals if isinstance(originals, list) else [originals]
            if old not in (*originals, previous.get(target), digest):
                continue  # Preserve user-modified launchers.
            if old != digest:
                repaired_legacy |= target != "README.md" and old in originals
                legacy_detected |= repaired_legacy
                temporary = path.with_suffix(path.suffix + ".updating")
                temporary.write_bytes(content)
                temporary.replace(path)
            installed[target] = digest
        if installed:
            (directory / "launcher-state.json").write_text(json.dumps(installed) + "\n", "utf-8")
            (directory / "install-root.json").write_text(json.dumps({"root": str(root)}) + "\n", "utf-8")
            for name in ("enable-background.cmd", "disable-background.cmd"):
                path = root / name
                if path.is_file() and not path.is_symlink() and hashlib.sha256(path.read_bytes()).hexdigest() == legacy.get(name):
                    path.unlink()
        return repaired_legacy
    except (OSError, ValueError, KeyError, TypeError):
        return legacy_detected  # A read-only source folder can still use the new terminal.
