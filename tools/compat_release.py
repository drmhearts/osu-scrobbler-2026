"""Transition assets understood by pre-rebrand update validators.

Only archive/distribution identities differ. All executable code is identical.
Remove once the supported installed base has migrated to the new updater.
"""

import base64
import csv
import hashlib
import io
import json
from zipfile import ZipFile, ZIP_DEFLATED


def legacy_wheel(source, destination, version):
    modern = f'osu_life_hacks-{version}.dist-info/'
    legacy = f'osu_scrobbler-{version}.dist-info/'
    records = []
    with ZipFile(source) as original, ZipFile(destination, 'w', ZIP_DEFLATED) as output:
        for name in original.namelist():
            if name == modern + 'RECORD':
                continue
            content = original.read(name)
            name = name.replace(modern, legacy)
            if name == legacy + 'METADATA':
                content = content.replace(b'Name: osu-life-hacks', b'Name: osu-scrobbler')
                # Old updaters import this module in their installation smoke
                # check. Only the transition wheel retains it; readers never use it.
                head, separator, body = content.partition(b'\n\n')
                content = head.rstrip(b'\n') + b'\nRequires-Dist: websockets>=14,<17\n\n' + body
            output.writestr(name, content)
            digest = base64.urlsafe_b64encode(hashlib.sha256(content).digest()).rstrip(b'=').decode()
            records.append((name, 'sha256=' + digest, str(len(content))))
        records.append((legacy + 'RECORD', '', ''))
        record = io.StringIO(newline='')
        csv.writer(record).writerows(records)
        output.writestr(legacy + 'RECORD', record.getvalue())


def legacy_bundle(source, destination, extra_files=None):
    extra_files = extra_files or {}
    with ZipFile(source) as original, ZipFile(destination, 'w', ZIP_DEFLATED) as output:
        for name in original.namelist():
            content = original.read(name)
            name = name.replace('osu-life-hacks/', 'osu-scrobbler-2026/', 1)
            if name.endswith('/app/runtime/portable.json'):
                manifest = json.loads(content)
                manifest['name'] = 'osu-scrobbler'
                manifest['files'].update({name: hashlib.sha256(value).hexdigest() for name, value in extra_files.items()})
                content = (json.dumps(manifest, indent=2) + '\n').encode()
            output.writestr(name, content)
        for name, content in extra_files.items():
            output.writestr('osu-scrobbler-2026/app/runtime/' + name, content)
