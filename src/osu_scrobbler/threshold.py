"""Listening preferences, separate from settings read by older launchers."""

from dataclasses import asdict, dataclass
import json
import math
from pathlib import Path


@dataclass
class Threshold:
    mode: str = "half"
    seconds: float = 30.0

    def validate(self):
        if self.mode not in {"half", "full", "seconds"}:
            raise ValueError("Choose half, full, or seconds.")
        if isinstance(self.seconds, bool) or not isinstance(self.seconds, (int, float)) or not math.isfinite(self.seconds) or self.seconds <= 0:
            raise ValueError("Seconds must be a positive number.")
        return self

    @classmethod
    def load(cls, directory):
        path = Path(directory) / "threshold.json"
        return cls(**json.loads(path.read_text("utf-8"))).validate() if path.exists() else cls()

    def save(self, directory):
        self.validate()
        path = Path(directory) / "threshold.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps(asdict(self)) + "\n", "utf-8")
        temporary.replace(path)

    @property
    def label(self):
        return {"half": "Half song", "full": "Full map", "seconds": f"{self.seconds:g}s"}[self.mode]


def parse_threshold(value):
    value = value.strip().lower()
    if not value:
        return None
    if value in {"h", "half"}:
        return Threshold()
    if value in {"f", "full"}:
        return Threshold("full")
    try:
        return Threshold("seconds", float(value)).validate()
    except ValueError as error:
        raise ValueError("Use h, f, or positive seconds.") from error
