"""Opt-in bounded local metadata diagnostics; no request objects or credentials."""

import json
import os
from pathlib import Path


class ResolutionLog:
    def __init__(self, directory):
        self.path = Path(directory) / "metadata-debug.jsonl"

    def __call__(self, value):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.path.exists() and self.path.stat().st_size > 256 * 1024:
            self.path.replace(self.path.with_name("metadata-debug.1.jsonl"))
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(value, ensure_ascii=False, allow_nan=False) + "\n")
        if os.name != "nt":
            self.path.chmod(0o600)
