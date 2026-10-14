"""Optional last-object timing from the selected .osu file, never audio capture."""

import hashlib
import math
from pathlib import Path


def last_object(path, checksum):
    """Read only a bounded, checksum-matching map; unknown timing stays unknown."""
    try:
        path = Path(path)
        if path.stat().st_size > 16 * 1024 * 1024:
            return None
        with path.open('rb') as handle:
            content = handle.read(16 * 1024 * 1024 + 1)
        if len(content) > 16 * 1024 * 1024:
            return None
        if hashlib.md5(content).hexdigest() != checksum.lower():
            return None
        section, timing, objects, multiplier = '', [], [], 1.4
        for line in content.decode('utf-8-sig').splitlines():
            line = line.strip()
            if line.startswith('['):
                section = line
            elif section == '[Difficulty]' and line.startswith('SliderMultiplier:'):
                multiplier = float(line.split(':', 1)[1])
            elif section == '[TimingPoints]' and line and not line.startswith('//'):
                fields = line.split(',')
                timing.append((float(fields[0]), float(fields[1]), len(fields) < 7 or fields[6] == '1'))
            elif section == '[HitObjects]' and line and not line.startswith('//'):
                objects.append(line.split(','))
        timing.sort()
        end = 0.0
        for fields in objects:
            start, kind = float(fields[2]), int(fields[3])
            finish = start
            if kind & (8 | 128):
                finish = float(fields[5].split(':')[0])
            elif kind & 2:
                beat, velocity = 500.0, 1.0
                for at, length, uninherited in timing:
                    if at > start:
                        break
                    if uninherited and length > 0:
                        beat, velocity = length, 1.0
                    elif not uninherited and length < 0:
                        velocity = max(.1, min(10, -100 / length))
                finish += float(fields[7]) * int(fields[6]) * beat / (100 * multiplier * velocity)
            end = max(end, finish)
        return end if math.isfinite(end) and 0 < end <= 86400000 else None
    except (OSError, ValueError, IndexError, UnicodeError, ZeroDivisionError):
        return None


def contained_path(root, *parts):
    root = Path(root).resolve()
    path = root.joinpath(*parts).resolve()
    if not path.is_relative_to(root):
        raise ValueError('Selected file is outside the game storage')
    return path
