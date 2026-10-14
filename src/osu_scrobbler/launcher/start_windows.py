"""Small launcher: use the verified updated runtime before the source copy."""

import hashlib
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
        if not re.fullmatch(r"versions/[0-9.]+-[a-z0-9_\-]+/venv", current["environment"]):
            return None
        environment = updates / current["environment"]
        if environment.resolve() != updates.resolve() / current["environment"]:
            return None
        marker = json.loads((environment.parent / "ready.json").read_text("utf-8"))
        executable = environment / "Scripts/python.exe"
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
        (directory / "install-root.json").write_text(json.dumps({"root": str(root)}) + "\n", "utf-8")
        executable = updated_python(directory)
        if executable is None:
            prepare(root)
            executable = Path(sys.executable)
            working = root
        else:
            working = executable.parent.parent
        environment = os.environ.copy()
        environment.pop("PYTHONPATH", None)
        environment.pop("PYTHONHOME", None)
        return subprocess.run([str(executable), "-m", "osu_scrobbler", "--data-dir", str(directory.resolve()), "setup", *sys.argv[1:]],
                              cwd=working, env=environment).returncode
    except (OSError, subprocess.CalledProcessError):
        print("Setup unavailable. Check Python and your connection, then reopen START.cmd.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
