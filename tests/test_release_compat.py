"""The rename must not strand installations with the previous update validator."""

import base64
import csv
import hashlib
import importlib.util
import io
import json
from pathlib import Path
from zipfile import ZipFile, ZIP_DEFLATED

from osu_scrobbler import portable
from osu_scrobbler.updater import Release, verify_wheel


def helpers():
    path = Path(__file__).parents[1] / 'tools/compat_release.py'
    spec = importlib.util.spec_from_file_location('compat_release', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_legacy_wheel_keeps_code_and_rebuilds_every_record(tmp_path):
    current = tmp_path / 'osu_life_hacks-1.0.0-py3-none-any.whl'
    legacy = tmp_path / 'osu_scrobbler-1.0.0-py3-none-any.whl'
    dist = 'osu_life_hacks-1.0.0.dist-info/'
    with ZipFile(current, 'w', ZIP_DEFLATED) as archive:
        archive.writestr('osu_scrobbler/__init__.py', '__version__ = "1.0.0"\n')
        archive.writestr(dist + 'METADATA', 'Metadata-Version: 2.1\nName: osu-life-hacks\nVersion: 1.0.0\n')
        archive.writestr(dist + 'WHEEL', 'Wheel-Version: 1.0\nTag: py3-none-any\n')
        archive.writestr(dist + 'RECORD', 'discard this old record')
    helpers().legacy_wheel(current, legacy, '1.0.0')
    with ZipFile(legacy) as archive, ZipFile(current) as original:
        assert archive.read('osu_scrobbler/__init__.py') == original.read('osu_scrobbler/__init__.py')
        assert b'Name: osu-scrobbler\n' in archive.read('osu_scrobbler-1.0.0.dist-info/METADATA')
        assert b'Requires-Dist: websockets>=14,<17' in archive.read('osu_scrobbler-1.0.0.dist-info/METADATA')
        rows = list(csv.reader(io.StringIO(archive.read('osu_scrobbler-1.0.0.dist-info/RECORD').decode())))
        assert {row[0] for row in rows} == set(archive.namelist())
        for name, digest, size in rows:
            if name.endswith('/RECORD'):
                assert (digest, size) == ('', '')
                continue
            content = archive.read(name)
            assert int(size) == len(content)
            assert digest == 'sha256=' + base64.urlsafe_b64encode(hashlib.sha256(content).digest()).decode().rstrip('=')


def test_legacy_portable_is_accepted_under_old_prefix_and_local_manifest(tmp_path, monkeypatch):
    files = {'python.exe': b'fixture', 'pythonw.exe': b'fixture',
             'Lib/site-packages/osu_scrobbler/__init__.py': b'__version__="1.0.0"',
             'python312.dll': b'fixture'}
    manifest = {'name': 'osu-life-hacks', 'schema': 1, 'version': '1.0.0',
                'files': {name: hashlib.sha256(content).hexdigest() for name, content in files.items()}}
    current, legacy = tmp_path / 'current.zip', tmp_path / 'legacy.zip'
    with ZipFile(current, 'w', ZIP_DEFLATED) as archive:
        for name, content in files.items():
            archive.writestr(portable.PREFIX + name, content)
        archive.writestr(portable.PREFIX + 'portable.json', json.dumps(manifest))
    compatibility = {'Lib/site-packages/websockets/__init__.py': b'# synthetic dependency fixture\n'}
    helpers().legacy_bundle(current, legacy, compatibility)
    content = legacy.read_bytes()
    monkeypatch.setattr(portable, 'PREFIX', 'osu-scrobbler-2026/app/runtime/')
    release = Release('1.0.0', 'osu-scrobbler-windows.zip', '', hashlib.sha256(content).hexdigest(), len(content))
    portable.verify_archive(content, release)
    with ZipFile(legacy) as archive:
        data = portable.manifest(archive.read(portable.PREFIX + 'portable.json'), '1.0.0')
        assert data['name'] == 'osu-scrobbler'
        for name, expected in files.items():
            assert archive.read(portable.PREFIX + name) == expected
        for name, expected in compatibility.items():
            assert archive.read(portable.PREFIX + name) == expected
            assert data['files'][name] == hashlib.sha256(expected).hexdigest()
