"""First-click bootstrap: standard library only until app dependencies exist."""

import hashlib
from pathlib import Path
import subprocess
import sys


def prepare(root, *, runner=subprocess.run):
    marker = root / ".venv/scrobbler-installed.sha256"
    fingerprint = hashlib.sha256((root / "pyproject.toml").read_bytes()).hexdigest()
    probe = [sys.executable, "-c", "from osu_scrobbler import cli, watcher, tray; import psutil, pystray, PIL"]
    if marker.exists() and marker.read_text("utf-8").strip() == fingerprint:
        if runner(probe, cwd=root, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0:
            return
    print("Installing osu! scrobbler and its dependencies. This is needed only for setup or changed requirements.", flush=True)
    runner([sys.executable, "-m", "pip", "--disable-pip-version-check", "install", "--no-input", "-e", str(root)], cwd=root, check=True)
    runner(probe, cwd=root, check=True)
    marker.write_text(fingerprint + "\n", "utf-8")


def main():
    root = Path(__file__).resolve().parents[1]
    try:
        prepare(root)
        return subprocess.run([sys.executable, "-m", "osu_scrobbler", "setup", *sys.argv[1:]], cwd=root).returncode
    except (OSError, subprocess.CalledProcessError):
        print("Setup could not finish. The messages above explain which step failed.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
