"""Domain objects shared across replaceable API adapters."""

from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any


class Phase(str, Enum):
    MENU = "menu"
    PLAYING = "playing"
    PAUSED = "paused"
    RESULTS = "results"
    FAILED = "failed"
    DISCONNECTED = "disconnected"


@dataclass(frozen=True)
class Beatmap:
    selection_key: str
    title: str
    artist: str
    title_unicode: str = ""
    artist_unicode: str = ""
    duration: float | None = None
    map_id: int | None = None
    set_id: int | None = None
    difficulty: str = ""
    audio_path: str = ""

    @property
    def track_key(self) -> str:
        # Different difficulties may share a recording. Mapping identity is separate.
        return f"{self.artist.casefold().strip()}\0{self.title.casefold().strip()}"


@dataclass(frozen=True)
class Telemetry:
    beatmap: Beatmap | None
    phase: Phase
    position: float | None = None
    attempt_token: str | None = None
    connected: bool = True
    # False when an observation cannot establish whether the preceding sampled
    # interval was personal gameplay (e.g. missing/stale companion evidence).
    interval_reliable: bool = True


@dataclass
class Track:
    title: str
    artist: str
    album: str
    album_artist: str
    duration: float | None = None
    recording_id: str | None = None
    release_id: str | None = None
    album_fallback: bool = False
    confidence: float = 0.0
    verified: bool = False
    details: dict[str, Any] = field(default_factory=dict)

    @property
    def identity(self) -> str:
        return self.recording_id or f"{self.artist.casefold().strip()}\0{self.title.casefold().strip()}"

    def to_dict(self, *, include_details=True) -> dict[str, Any]:
        value = asdict(self)
        if not include_details:
            value.pop("details")
        return value

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "Track":
        return cls(**value)

    @classmethod
    def fallback(cls, beatmap: Beatmap) -> "Track":
        artist = beatmap.artist_unicode or beatmap.artist
        title = beatmap.title_unicode or beatmap.title
        return cls(title, artist, title, artist, beatmap.duration, album_fallback=True)


@dataclass
class LookupResult:
    track: Track | None
    candidates: list[Track] = field(default_factory=list)
    reason: str = ""


@dataclass(frozen=True)
class Submission:
    id: str
    attempt_id: str
    track: Track
    playback_start: int
    requested_at: float
    manual: bool = False
