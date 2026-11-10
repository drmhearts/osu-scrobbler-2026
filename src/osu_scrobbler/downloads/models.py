"""Validated filters and destinations; no credentials belong in these objects."""

from dataclasses import asdict, dataclass
from datetime import date, datetime, timezone
import math
import unicodedata

MODES = {"osu": 0, "taiko": 1, "catch": 2, "mania": 3}
STATUSES = {"ranked", "approved", "loved", "qualified", "pending", "wip", "graveyard", "any"}
GENRES = {"any": 0, "unspecified": 1, "video game": 2, "anime": 3, "rock": 4,
          "pop": 5, "other": 6, "novelty": 7, "hip hop": 9, "electronic": 10,
          "metal": 11, "classical": 12, "folk": 13, "jazz": 14}


class DownloadError(RuntimeError):
    """Safe local/domain message, never an upstream body or credential-bearing URL."""


class AuthRequired(DownloadError):
    pass


class RateLimited(DownloadError):
    def __init__(self, seconds=3600):
        super().__init__("osu! download limit reached. Progress saved; resume after the cooldown.")
        self.seconds = seconds


class TransientError(DownloadError):
    def __init__(self, seconds=60):
        super().__init__("Download temporarily unavailable. Progress saved for a later retry.")
        self.seconds = seconds


def normalized(value):
    return " ".join(unicodedata.normalize("NFKC", str(value or "")).casefold().split())


def calendar_day(value):
    return date.fromisoformat(str(value)[:10])


@dataclass(frozen=True)
class Filters:
    mode: str = "osu"
    genre: str = "any"
    category: str = "ranked"
    ranked_from: str = ""
    ranked_to: str = ""
    limit: int = 25
    min_length: float | None = None
    max_length: float | None = None
    mapper: str = ""

    def validate(self):
        if self.mode not in MODES or self.genre not in GENRES or self.category not in STATUSES:
            raise ValueError("Choose a listed mode, genre and category.")
        if type(self.limit) is not int or not 1 <= self.limit <= 10000:
            raise ValueError("Maximum beatmapsets must be 1–10000.")
        for value in (self.min_length, self.max_length):
            if value is not None and (type(value) not in (int, float) or not math.isfinite(value) or value < 0):
                raise ValueError("Length must be a finite non-negative number of seconds.")
        if self.min_length is not None and self.max_length is not None and self.min_length > self.max_length:
            raise ValueError("Minimum length cannot exceed maximum length.")
        for value in (self.ranked_from, self.ranked_to):
            if value and (len(value) != 10 or calendar_day(value).isoformat() != value):
                raise ValueError("Ranked dates must use YYYY-MM-DD (inclusive UTC calendar days).")
        if self.ranked_from and self.ranked_to and self.ranked_from > self.ranked_to:
            raise ValueError("Ranked start date cannot follow the end date.")
        if (self.ranked_from or self.ranked_to) and self.category not in {"ranked", "approved", "loved", "any"}:
            raise ValueError("This category has no ranked date. Clear ranked-date filters or choose ranked/approved/loved/any.")
        if not isinstance(self.mapper, str) or len(self.mapper) > 128 or any(ord(c) < 32 for c in self.mapper):
            raise ValueError("Mapper must be a username or numeric user ID.")
        return self

    def query(self, cursor=None):
        self.validate()
        parts = []
        for name, op, value in (("length", ">=", self.min_length), ("length", "<=", self.max_length),
                                ("ranked", ">=", self.ranked_from), ("ranked", "<=", self.ranked_to)):
            if value is not None and value != "":
                parts.append(f"{name}{op}{value}")
        if self.mapper:
            escaped = self.mapper.replace("\\", "\\\\").replace('"', '\\"')
            parts.append("creator=" + self.mapper if self.mapper.isdecimal() else 'creator="' + escaped + '"')
        params = {"m": str(MODES[self.mode]), "s": "ranked" if self.category == "approved" else self.category,
                  "sort": "ranked_desc" if self.category in {"ranked", "approved", "loved", "any"} else "updated_desc"}
        if self.genre != "any":
            params["g"] = str(GENRES[self.genre])
        if parts:
            params["q"] = " ".join(parts)
        if cursor:
            params["cursor_string"] = cursor
        return params

    def matches(self, item):
        if item.get("download_disabled") or item.get("status") not in STATUSES - {"any"}:
            return False
        if self.category != "any" and item.get("status") != self.category:
            return False
        genre = item.get("genre") or {}
        genre_id = item.get("genre_id", genre.get("id") if isinstance(genre, dict) else None)
        if self.genre != "any" and str(genre_id) != str(GENRES[self.genre]):
            return False
        if self.mapper:
            if self.mapper.isdecimal():
                if str(item.get("user_id")) != self.mapper:
                    return False
            elif normalized(item.get("creator")) != normalized(self.mapper):
                return False
        if self.ranked_from or self.ranked_to:
            try:
                ranked = datetime.fromisoformat(str(item["ranked_date"]).replace("Z", "+00:00"))
                ranked = ranked.astimezone(timezone.utc).date().isoformat()
            except (ValueError, TypeError, KeyError):
                return False  # Missing dates are excluded, never replaced by update dates.
            if self.ranked_from and ranked < self.ranked_from or self.ranked_to and ranked > self.ranked_to:
                return False
        # One difficulty must satisfy mode AND length; two separate difficulties do not qualify.
        for beatmap in item.get("beatmaps") or []:
            mode = beatmap.get("mode_int", {"osu": 0, "taiko": 1, "fruits": 2, "mania": 3}.get(beatmap.get("mode")))
            length = beatmap.get("total_length")
            if mode != MODES[self.mode]:
                continue
            if self.min_length is not None or self.max_length is not None:
                if type(length) not in (float, int) or not math.isfinite(length):
                    continue
                if self.min_length is not None and length < self.min_length or self.max_length is not None and length > self.max_length:
                    continue
            return True
        return False

    def to_dict(self):
        return asdict(self)


@dataclass(frozen=True)
class Destination:
    client: str
    root: str
    executable: str = ""

    def validate(self):
        from pathlib import Path
        if self.client not in {"stable", "lazer"}:
            raise ValueError("Destination must be stable or lazer.")
        root = Path(self.root).expanduser()
        if not root.is_dir():
            raise ValueError("Choose an existing installation/storage directory.")
        if self.client == "stable" and root.name.casefold() != "songs" and not (root / "Songs").is_dir():
            raise ValueError("Choose Stable's installation or Songs directory.")
        if self.client == "lazer" and not (root / "files").is_dir():
            raise ValueError("Choose lazer's storage directory containing files, not its executable directory.")
        if self.executable and (not Path(self.executable).is_file() or Path(self.executable).suffix.lower() != ".exe"):
            raise ValueError("Choose the client's existing Windows executable.")
        return self

    @property
    def songs(self):
        from pathlib import Path
        root = Path(self.root).expanduser().resolve()
        return root if root.name.casefold() == "songs" else root / "Songs"

    def to_dict(self):
        return asdict(self)
