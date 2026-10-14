"""Build a Windows download with its own CPython and installed dependencies."""

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import tomllib
from zipfile import ZipFile, ZIP_DEFLATED
from compat_release import legacy_bundle


def build(output):
    if os.name != "nt" or sys.maxsize <= 2**32:
        raise RuntimeError("Build the Windows x64 package on Windows x64.")
    root = Path(__file__).resolve().parents[1]
    number = tomllib.loads((root / "pyproject.toml").read_text("utf-8"))["project"]["version"]
    wheel = output / f"osu_life_hacks-{number}-py3-none-any.whl"
    if not wheel.is_file():
        raise RuntimeError("Build the release wheel first.")
    with tempfile.TemporaryDirectory(prefix="osu-scrobbler-build-") as temporary:
        package = Path(temporary) / "osu-life-hacks"
        runtime = package / "app/runtime"
        runtime.mkdir(parents=True)
        python_home = Path(sys.base_prefix)
        # Copy the official setup-python installation, excluding its packages,
        # tests and caches. The isolated path file prevents global imports.
        for filename in ("python.exe", "pythonw.exe", "python3.dll", f"python{sys.version_info.major}{sys.version_info.minor}.dll", "LICENSE.txt"):
            shutil.copyfile(python_home / filename, runtime / filename)
        for filename in python_home.glob("vcruntime*.dll"):
            shutil.copyfile(filename, runtime / filename.name)
        for folder in ("Lib", "DLLs"):
            shutil.copytree(python_home / folder, runtime / folder,
                ignore=shutil.ignore_patterns("site-packages", "__pycache__", "*.pyc", "test", "tests", "idlelib", "ensurepip", "tkinter"))
        (runtime / f"python{sys.version_info.major}{sys.version_info.minor}._pth").write_text(
            ".\nDLLs\nLib\nLib/site-packages\nimport site\n", "utf-8")
        subprocess.run([sys.executable, "-m", "pip", "--disable-pip-version-check", "install", "--no-input",
            "--no-compile", "--only-binary=:all:", "--target", str(runtime / "Lib/site-packages"), str(wheel)], check=True)
        manifest = {"schema": 1, "name": "osu-life-hacks", "version": number, "files": {
            path.relative_to(runtime).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in sorted(runtime.rglob("*")) if path.is_file()}}
        (runtime / "portable.json").write_text(json.dumps(manifest, indent=2) + "\n", "utf-8")
        tools = package / "app/tools"
        tools.mkdir()
        shutil.copyfile(root / "tools/start_windows.py", tools / "start_windows.py")
        for filename in ("START.cmd", "README.md", "LICENSE"):
            shutil.copyfile(root / filename, package / filename)
        shutil.copytree(root / 'docs', package / 'docs')
        # Run the actual copied interpreter with no PATH Python and no user-site.
        clean_env = os.environ.copy()
        clean_env["PATH"] = os.environ["SystemRoot"] + r"\System32"
        clean_env.pop("PYTHONPATH", None)
        clean_env.pop("PYTHONHOME", None)
        subprocess.run([str(runtime / "python.exe"), "-c",
            "from osu_scrobbler import cli, watcher, tray, console; import httpx, keyring, psutil, pystray, PIL, sqlite3; "
            "from keyring.backends.Windows import WinVaultKeyring; import ctypes; "
            "c=sqlite3.connect(':memory:'); assert c.execute('select 1').fetchone()==(1,); "
            f"from osu_scrobbler import __version__; assert __version__=={number!r}"], cwd=runtime, env=clean_env, check=True)
        # Exercise copy/activation and prove the runtime survives deleting the download.
        smoke_profile = Path(temporary) / "A profile with spaces"
        subprocess.run([str(runtime / "python.exe"), "-c",
            "import asyncio,sys; from pathlib import Path; from osu_scrobbler.portable import install_local; "
            "asyncio.run(install_local(Path(sys.argv[1]),Path(sys.prefix)))", str(smoke_profile)], env=clean_env, check=True)
        current = json.loads((smoke_profile / "updates/current.json").read_text("utf-8"))
        installed = smoke_profile / "updates" / current["environment"] / "python.exe"
        # Smoke imports may produce pycache files; only the verified manifest ships.
        asset = output / "osu-life-hacks-windows.zip"
        with ZipFile(asset, "w", ZIP_DEFLATED, compresslevel=6) as archive:
            for path in sorted(package.rglob("*")):
                if path.is_file() and "__pycache__" not in path.parts and path.suffix != ".pyc":
                    archive.write(path, path.relative_to(package.parent).as_posix())
        shutil.rmtree(package)
        subprocess.run([str(installed), "-c", "from osu_scrobbler.config import default_data_dir; from pathlib import Path; import sys; "
            "assert default_data_dir()==Path(sys.argv[1]); from osu_scrobbler.cli import parser; assert parser().parse_args(['launch']).command=='launch'",
            str(smoke_profile)], env=clean_env, check=True)
        for _ in range(3):
            subprocess.run([str(installed), str(root / "tools/smoke_windows.py"), str(smoke_profile)], env=clean_env, check=True, timeout=90)
        # Validate the public archive, not just the build directory.
        subprocess.run([str(installed), "-c",
            "import sys,hashlib; from pathlib import Path; from osu_scrobbler.portable import verify_archive; "
            "from osu_scrobbler.updater import Release; c=Path(sys.argv[1]).read_bytes(); "
            f"verify_archive(c,Release({number!r},'osu-life-hacks-windows.zip','',hashlib.sha256(c).hexdigest(),len(c)))", str(asset)], env=clean_env, check=True)
        checksums = output / "SHA256SUMS.txt"
        with checksums.open("a", encoding="utf-8") as handle:
            handle.write(f"{hashlib.sha256(asset.read_bytes()).hexdigest()}  {asset.name}\n")
            legacy = output / 'osu-scrobbler-windows.zip'
            compatibility = Path(temporary) / 'legacy-dependencies'
            subprocess.run([sys.executable, '-m', 'pip', '--disable-pip-version-check', 'install',
                '--no-compile', '--no-deps', '--only-binary=:all:', '--target', str(compatibility),
                'websockets>=14,<17'], check=True)
            extra_files = {'Lib/site-packages/' + path.relative_to(compatibility).as_posix(): path.read_bytes()
                           for path in compatibility.rglob('*') if path.is_file()}
            legacy_bundle(asset, legacy, extra_files)
            handle.write(f"{hashlib.sha256(legacy.read_bytes()).hexdigest()}  {legacy.name}\n")
        print(f"Windows x64 runtime built and verified: {asset.name} ({asset.stat().st_size} bytes)")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path("dist"))
    try:
        build(parser.parse_args().output.resolve())
    except Exception:
        # Public CI annotations expose build failures even without log access.
        import traceback
        message = traceback.format_exc().replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")
        print("::error::" + message)
        raise
