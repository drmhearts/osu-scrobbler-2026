"""The entire reader contract: music, clock, screen and personal-play evidence."""

from dataclasses import dataclass
from .memory import ReaderUnavailable


@dataclass(frozen=True)
class Sample:
    client: str
    state: int
    beatmap: dict
    position_ms: float
    personal: bool = False
    failed: bool = False

    def validate(self):
        import math
        import re
        if self.client not in {'stable', 'lazer'} or self.state not in range(20):
            raise ReaderUnavailable('Unknown game state')
        if not math.isfinite(self.position_ms) or not -60000 <= self.position_ms <= 86400000:
            raise ReaderUnavailable('Invalid game position')
        if not re.fullmatch('[a-fA-F0-9]{32}', self.beatmap.get('checksum', '')):
            raise ReaderUnavailable('Selected beatmap is not ready')
        if not self.beatmap.get('title') or not self.beatmap.get('artist'):
            raise ReaderUnavailable('Selected song metadata is unavailable')
        duration = self.beatmap['time']['mp3Length']
        if not math.isfinite(duration) or not 0 < duration <= 86400000:
            raise ReaderUnavailable('Audio clock is not ready')
        return self
