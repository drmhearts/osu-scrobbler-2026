"""Non-secret configuration. Credentials live only in the OS credential store."""

import json
import os
import sys
import re
from dataclasses import asdict, dataclass
from pathlib import Path
from . import __version__


def default_data_dir() -> Path:
    # Cached runtimes belong to their original profile, including Microsoft
    # Store filesystem redirects. Avoid moving settings or duplicating long paths.
    prefix = Path(sys.prefix)
    if prefix.parent.parent.name == "versions" and prefix.parent.parent.parent.name == "updates":
        try:
            marker = json.loads((prefix.parent / "ready.json").read_text("utf-8"))
            if (prefix.parent.name.startswith(__version__ + "-") and marker.get("version") == __version__
                    and isinstance(marker.get("digest"), str) and re.fullmatch(r"[a-f0-9]{64}", marker["digest"])
                    and prefix.name in {"venv", "runtime"}
                    and marker.get("environment") == f"versions/{prefix.parent.name}/{prefix.name}"):
                return prefix.parent.parent.parent.parent
        except (OSError, ValueError, AttributeError):
            pass
    if os.name == "nt":
        return Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData/Local")) / "osu-scrobbler"
    return Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local/share")) / "osu-scrobbler"


@dataclass
class Config:
    ipc_port: int = 42381
    threshold_mode: str = "half"
    auto_export: bool = False
    auto_update: bool = True
    export_path: str = ""
    musicbrainz_user_agent: str = f"osu-life-hacks/{__version__} (Hky Sigh)"

    def validate(self) -> None:
        if type(self.ipc_port) is not int or not 1024 <= self.ipc_port <= 65535:
            raise ValueError("ipc_port must be between 1024 and 65535")
        if not isinstance(self.threshold_mode, str) or self.threshold_mode not in {"half", "custom", "standard"}:
            raise ValueError("threshold_mode must be half")
        # Migrate existing profiles without requiring a manual settings change.
        self.threshold_mode = "half"
        if not isinstance(self.auto_export, bool):
            raise ValueError("auto_export must be true or false")
        if not isinstance(self.auto_update, bool):
            raise ValueError("auto_update must be true or false")
        if not isinstance(self.export_path, str):
            raise ValueError("export_path must be a string")
        if (not isinstance(self.musicbrainz_user_agent, str) or not self.musicbrainz_user_agent.strip()
                or "/" not in self.musicbrainz_user_agent or "(" not in self.musicbrainz_user_agent
                or "\n" in self.musicbrainz_user_agent or "\r" in self.musicbrainz_user_agent):
            raise ValueError("musicbrainz_user_agent must identify the application and provide contact information")

    @classmethod
    def load(cls, directory: Path) -> "Config":
        path = directory / "config.json"
        values = json.loads(path.read_text("utf-8")) if path.exists() else {}
        # Retire legacy transport settings without discarding any user settings.
        values.pop("tosu_url", None)
        values.pop("tosu_path", None)
        old_agent = values.get("musicbrainz_user_agent", "")
        if isinstance(old_agent, str) and re.fullmatch(
                r"osu-scrobbler/[\d.]+ \(https://github\.com/drmhearts/osu-scrobbler-2026\)", old_agent):
            values.pop("musicbrainz_user_agent")
        config = cls(**values)
        config.validate()
        return config

    def save(self, directory: Path) -> None:
        self.validate()
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / "config.json"
        temporary = path.with_suffix(".tmp")
        saved = asdict(self)
        # Older source launchers read config before handing off to the updated
        # runtime. Keep their accepted disk value; this runtime always uses half.
        saved["threshold_mode"] = "custom"
        temporary.write_text(json.dumps(saved, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        if os.name != "nt":
            temporary.chmod(0o600)
        temporary.replace(path)


class WatchLock:
    """An OS-released file lock prevents competing watchers for a data directory."""

    def __init__(self, directory: Path, filename="watch.lock"):
        directory.mkdir(parents=True, exist_ok=True)
        self.file = (directory / filename).open("a+b")
        if self.file.tell() == 0:
            self.file.write(b"0")
            self.file.flush()

    def __enter__(self):
        self.file.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(self.file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            self.file.close()
            raise RuntimeError("A watcher is already using this data directory") from exc
        return self

    def __exit__(self, *args):
        self.file.close()
