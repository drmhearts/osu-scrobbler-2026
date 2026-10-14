"""Non-secret configuration. Credentials live only in the OS credential store."""

import json
import os
from dataclasses import asdict, dataclass
from pathlib import Path
from urllib.parse import urlparse
from . import __version__


def default_data_dir() -> Path:
    if os.name == "nt":
        return Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData/Local")) / "osu-scrobbler"
    return Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local/share")) / "osu-scrobbler"


@dataclass
class Config:
    tosu_url: str = "ws://127.0.0.1:24050/websocket/v2"
    tosu_path: str = ""
    ipc_port: int = 42381
    threshold_mode: str = "custom"
    auto_export: bool = False
    auto_update: bool = True
    export_path: str = ""
    musicbrainz_user_agent: str = f"osu-scrobbler/{__version__} (https://github.com/drmhearts/osu-scrobbler-2026)"

    def validate(self) -> None:
        if not isinstance(self.tosu_path, str) or any(char in self.tosu_path for char in "\r\n\x00"):
            raise ValueError("tosu_path must be an executable path")
        if not isinstance(self.tosu_url, str):
            raise ValueError("tosu_url must be a string")
        parsed = urlparse(self.tosu_url)
        if (parsed.scheme not in {"ws", "wss"} or parsed.hostname not in {"localhost", "127.0.0.1", "::1"}
                or parsed.path != "/websocket/v2" or parsed.username or parsed.password or parsed.query or parsed.fragment):
            raise ValueError("tosu_url must use a loopback ws(s)://host:port/websocket/v2 endpoint")
        _ = parsed.port
        if type(self.ipc_port) is not int or not 1024 <= self.ipc_port <= 65535:
            raise ValueError("ipc_port must be between 1024 and 65535")
        if not isinstance(self.threshold_mode, str) or self.threshold_mode not in {"custom", "standard"}:
            raise ValueError("threshold_mode must be custom or standard")
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
        config = cls(**json.loads(path.read_text("utf-8"))) if path.exists() else cls()
        config.validate()
        return config

    def save(self, directory: Path) -> None:
        self.validate()
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / "config.json"
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps(asdict(self), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
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
