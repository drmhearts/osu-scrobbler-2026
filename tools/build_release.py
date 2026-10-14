"""Build the public wheel/source assets for a versioned GitHub release."""

import argparse
import ast
import hashlib
from pathlib import Path
import subprocess
import sys
import tomllib
from zipfile import ZipFile, ZIP_DEFLATED, ZipInfo


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path("dist"))
    parser.add_argument("--tag", help="Require this tag to match pyproject.toml, e.g. v0.2.0")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    version = tomllib.loads((root / "pyproject.toml").read_text("utf-8"))["project"]["version"]
    module = ast.parse((root / "src/osu_scrobbler/__init__.py").read_text("utf-8"))
    runtime_version = next((ast.literal_eval(node.value) for node in module.body if isinstance(node, ast.Assign)
                            and any(isinstance(target, ast.Name) and target.id == "__version__" for target in node.targets)), None)
    if runtime_version != version:
        parser.error("Runtime and package versions must match")
    if args.tag and args.tag != "v" + version:
        parser.error("Release tag must match the project version")
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    subprocess.run([sys.executable, "-m", "pip", "wheel", "--no-deps", "--wheel-dir", str(output), str(root)], check=True)
    package = output / f"osu_scrobbler-{version}-py3-none-any.whl"
    if not package.is_file():
        raise RuntimeError("Expected universal release wheel was not built")
    files = [root / name for name in ("README.md", "LICENSE", ".gitignore", "pyproject.toml", "START.cmd", "enable-background.cmd", "disable-background.cmd")]
    for directory in (".github", "docs", "src/osu_scrobbler", "tests", "tools"):
        files.extend(path for path in (root / directory).rglob("*")
                     if path.is_file() and not path.is_symlink() and "__pycache__" not in path.parts
                     and path.suffix in {".py", ".json", ".md", ".yml"})
    source = output / "osu-scrobbler-windows-source.zip"
    with ZipFile(source, "w", ZIP_DEFLATED) as archive:
        for path in sorted(files):
            info = ZipInfo("osu-scrobbler-2026/" + path.relative_to(root).as_posix())
            info.compress_type = ZIP_DEFLATED
            info.external_attr = 0o100644 << 16
            archive.writestr(info, path.read_bytes())
    checksums = [f"{hashlib.sha256(path.read_bytes()).hexdigest()}  {path.name}" for path in (package, source)]
    (output / "SHA256SUMS.txt").write_text("\n".join(checksums) + "\n", "utf-8")
    print(f"Built release {version}: wheel, Windows source ZIP, and SHA256SUMS.txt")


if __name__ == "__main__":
    main()
