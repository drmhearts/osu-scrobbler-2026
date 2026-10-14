import hashlib
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

from osu_scrobbler.bootstrap import repair_launcher


def fixture(tmp_path):
    root = tmp_path / 'installed'
    root.mkdir()
    (root / 'pyproject.toml').write_text('[project]\nname="osu-scrobbler"\n')
    (root / 'tools').mkdir()
    assets = tmp_path / 'assets'
    assets.mkdir()
    legacy = {}
    for target in ('START.cmd', 'tools/start_windows.py', 'enable-background.cmd', 'disable-background.cmd'):
        path = root / target
        data = ('original ' + target).encode()
        path.write_bytes(data)
        legacy[target] = hashlib.sha256(data).hexdigest()
    (assets / 'START.cmd').write_bytes(b'new start')
    (assets / 'start_windows.py').write_bytes(b'new helper')
    (assets / 'legacy.json').write_text(json.dumps(legacy))
    profile = tmp_path / 'profile'
    profile.mkdir()
    return root, assets, profile


def test_existing_install_refreshes_only_owned_launchers_and_keeps_private_data(tmp_path):
    root, assets, profile = fixture(tmp_path)
    (profile / 'history.db').write_bytes(b'private history')
    assert repair_launcher(profile, root=root, assets=assets)
    assert (root / 'START.cmd').read_bytes() == b'new start'
    assert (root / 'tools/start_windows.py').read_bytes() == b'new helper'
    assert not (root / 'enable-background.cmd').exists()
    assert not (root / 'disable-background.cmd').exists()
    assert (profile / 'history.db').read_bytes() == b'private history'
    assert not repair_launcher(profile, assets=assets)


def test_user_edited_launchers_are_preserved(tmp_path):
    root, assets, profile = fixture(tmp_path)
    (root / 'START.cmd').write_bytes(b'user custom launcher')
    (root / 'enable-background.cmd').write_bytes(b'user startup script')
    repair_launcher(profile, root=root, assets=assets)
    assert (root / 'START.cmd').read_bytes() == b'user custom launcher'
    assert (root / 'enable-background.cmd').read_bytes() == b'user startup script'


def test_old_install_location_can_come_only_from_immediate_python_launcher(tmp_path):
    root, assets, profile = fixture(tmp_path)
    assert not repair_launcher(profile, assets=assets, parent_executable=tmp_path / 'other.exe')
    assert repair_launcher(profile, assets=assets, parent_executable=root / '.venv/Scripts/pythonw.exe')


def test_compact_download_start_is_updated_outside_support_folder(tmp_path):
    root, assets, profile = fixture(tmp_path)
    app = root / 'app'
    app.mkdir()
    (root / 'pyproject.toml').replace(app / 'pyproject.toml')
    (root / 'tools').replace(app / 'tools')
    assert repair_launcher(profile, root=app, assets=assets)
    assert (root / 'START.cmd').read_bytes() == b'new start'


def bootstrap_module():
    path = Path(__file__).resolve().parents[1] / 'tools/start_windows.py'
    spec = importlib.util.spec_from_file_location('new_bootstrap', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_bootstrap_uses_verified_updated_runtime_before_loading_old_app(tmp_path, monkeypatch):
    module = bootstrap_module()
    profile = tmp_path / 'profile'
    root = profile / 'updates'
    environment = root / 'versions/0.3.0-fixture/venv'
    executable = environment / 'Scripts/python.exe'
    executable.parent.mkdir(parents=True)
    executable.write_bytes(b'fixture python')
    current = {'version': '0.3.0', 'environment': 'versions/0.3.0-fixture/venv', 'digest': 'a' * 64}
    (environment.parent / 'ready.json').write_text(json.dumps(current))
    (root / 'current.json').write_text(json.dumps(current))
    calls = []
    monkeypatch.setattr(module, 'profile_directory', lambda: profile)
    monkeypatch.setattr(module, 'prepare', lambda _: (_ for _ in ()).throw(AssertionError('Do not import/reinstall old app')))
    monkeypatch.setattr(module.sys, 'argv', ['START'])
    monkeypatch.setattr(module.subprocess, 'run', lambda args, **kwargs: calls.append((args, kwargs)) or SimpleNamespace(returncode=0))
    assert module.main() == 0
    assert calls[0][0][0] == str(executable)
    assert calls[0][0][-1] == 'launch'
    assert calls[0][1]['cwd'] == environment
    assert 'PYTHONPATH' not in calls[0][1]['env']
    current['environment'] = '../../foreign'
    (root / 'current.json').write_text(json.dumps(current))
    assert module.updated_python(profile) is None


def test_windows_redirectors_are_followed_without_reading_other_process_paths(tmp_path, monkeypatch):
    import sys
    import osu_scrobbler.bootstrap as module
    root, assets, profile = fixture(tmp_path)
    paths = []
    class Process:
        def __init__(self, executable, parent=None):
            self.executable, self.previous = executable, parent
        def name(self):
            return 'pythonw.exe'
        def exe(self):
            paths.append(str(self.executable))
            return str(self.executable)
        def parent(self):
            return self.previous
    original = Process(root / '.venv/Scripts/pythonw.exe')
    base = Process(tmp_path / 'Python/pythonw.exe', original)
    cached = Process(tmp_path / 'updates/versions/0.3.0-x/venv/Scripts/pythonw.exe', base)
    monkeypatch.setitem(sys.modules, 'psutil', SimpleNamespace(Process=lambda _: cached, Error=RuntimeError))
    monkeypatch.setattr(module.sys, 'platform', 'win32')
    assert repair_launcher(profile, assets=assets)
    assert len(paths) == 3
