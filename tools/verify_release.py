"""Verify uploaded build artifacts before publishing the tagged release."""

import argparse
import hashlib
import json
from pathlib import Path
import tomllib
from zipfile import ZipFile


def verify(directory, tag):
    root = Path(__file__).resolve().parents[1]
    number = tomllib.loads((root / "pyproject.toml").read_text("utf-8"))["project"]["version"]
    if tag != "v" + number:
        raise RuntimeError("Release tag does not match package version")
    expected = {f"osu_life_hacks-{number}-py3-none-any.whl", "osu-life-hacks-windows.zip", "osu-life-hacks-windows-source.zip"}
    expected.update({f"osu_scrobbler-{number}-py3-none-any.whl", "osu-scrobbler-windows.zip"})
    rows = [line.split() for line in (directory / "SHA256SUMS.txt").read_text("utf-8").splitlines()]
    if len(rows) != len(expected) or {row[1] for row in rows if len(row) == 2} != expected:
        raise RuntimeError("Release checksum list is incomplete")
    for digest, name in rows:
        if hashlib.sha256((directory / name).read_bytes()).hexdigest() != digest:
            raise RuntimeError("Release asset checksum mismatch")
    with ZipFile(directory / "osu-life-hacks-windows.zip") as archive:
        manifest = json.loads(archive.read("osu-life-hacks/app/runtime/portable.json"))
        if manifest["name"] != "osu-life-hacks" or manifest["version"] != number:
            raise RuntimeError("Windows download version is incorrect")
    print(f"Verified all assets for {tag}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tag", required=True)
    parser.add_argument("--directory", type=Path, default=Path("dist"))
    args = parser.parse_args()
    verify(args.directory, args.tag)
