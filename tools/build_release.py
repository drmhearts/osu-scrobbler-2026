"""Build the current wheel and checksums for a versioned GitHub release."""

import argparse
import ast
import hashlib
from pathlib import Path
import subprocess
import sys
import tomllib


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
    package = output / f"osu_life_hacks-{version}-py3-none-any.whl"
    if not package.is_file():
        raise RuntimeError("Expected universal release wheel was not built")
    # The Windows ZIP carries the readme and launcher. GitHub supplies source
    # archives automatically; do not publish duplicate or legacy assets.
    checksums = [f"{hashlib.sha256(package.read_bytes()).hexdigest()}  {package.name}"]
    (output / "SHA256SUMS.txt").write_text("\n".join(checksums) + "\n", "utf-8")
    print(f"Built release {version}: wheel and SHA256SUMS.txt")


if __name__ == "__main__":
    main()
