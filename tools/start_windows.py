"""Small launcher: use the verified updated runtime before the source copy."""

import hashlib
import asyncio
import json
import os
from pathlib import Path
import re
import subprocess
import sys


def profile_directory():
    return Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData/Local")) / "osu-scrobbler"


def updated_python(directory):
    try:
        updates = directory / "updates"
        current = json.loads((updates / "current.json").read_text("utf-8"))
        if not re.fullmatch(r"\d+\.\d+\.\d+", current["version"]) or not re.fullmatch(r"[a-f0-9]{64}", current["digest"]):
            return None
        if not re.fullmatch(r"versions/[0-9.]+-[a-z0-9_\-]+/(?:venv|runtime)", current["environment"]):
            return None
        environment = updates / current["environment"]
        if environment.resolve() != updates.resolve() / current["environment"]:
            return None
        marker = json.loads((environment.parent / "ready.json").read_text("utf-8"))
        executable = environment / ("python.exe" if environment.name == "runtime" else "Scripts/python.exe")
        return executable if marker == current and executable.is_file() else None
    except (OSError, ValueError, KeyError, TypeError):
        return None


def prepare(root, *, runner=subprocess.run):
    marker = root / ".venv/scrobbler-installed.sha256"
    fingerprint = hashlib.sha256((root / "pyproject.toml").read_bytes()).hexdigest()
    probe = [sys.executable, "-c", "from osu_scrobbler import cli, watcher, tray; import psutil, pystray, PIL"]
    if marker.exists() and marker.read_text("utf-8").strip() == fingerprint:
        if runner(probe, cwd=root, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0:
            return
    print("Setting up…", flush=True)
    runner([sys.executable, "-m", "pip", "--disable-pip-version-check", "install", "--no-input", "-e", str(root)],
           cwd=root, check=True, capture_output=True, text=True)
    runner(probe, cwd=root, check=True)
    marker.write_text(fingerprint + "\n", "utf-8")


def main():
    root = Path(__file__).resolve().parents[1]
    try:
        directory = profile_directory()
        directory.mkdir(parents=True, exist_ok=True)
        if (root / "runtime/portable.json").is_file():
            from osu_scrobbler.portable import install_local
            asyncio.run(install_local(directory, root / "runtime"))
        elif (root / "pyproject.toml").is_file():
            (directory / "install-root.json").write_text(json.dumps({"root": str(root)}) + "\n", "utf-8")
        executable = updated_python(directory)
        if executable is None:
            prepare(root)
            executable = Path(sys.executable)
            working = root
        else:
            working = executable.parent if executable.parent.name == "runtime" else executable.parent.parent
        environment = os.environ.copy()
        environment.pop("PYTHONPATH", None)
        environment.pop("PYTHONHOME", None)
        if sys.stdout is None:
            executable = executable.with_name("pythonw.exe")
        return subprocess.run([str(executable), "-m", "osu_scrobbler", "--data-dir", str(directory.resolve()), "launch", *sys.argv[1:]],
                              cwd=working, env=environment,
                              **({"creationflags": subprocess.CREATE_NO_WINDOW} if sys.platform == "win32" and sys.stdout is None else {})).returncode
    except (OSError, RuntimeError, ValueError, subprocess.CalledProcessError):
        if sys.stderr is not None:
            print("Setup unavailable. Re-extract the Windows download and reopen START.cmd.", file=sys.stderr)
        elif not (directory / "setup-state.json").exists():
            import ctypes
            ctypes.windll.user32.MessageBoxW(None, "Setup unavailable. Re-extract the Windows download and reopen START.cmd.", "osu! life hacks", 0x10)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
