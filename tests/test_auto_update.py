"""Offline coverage for the update checker: validation, digest, extraction."""
import hashlib
import io
import json
import os
import sys
import zipfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))

from selection_automation import auto_update as updater

GOOD = {'name': 'selection-workbench-update-a-to-b.zip',
        'url': 'https://example.com/update.zip',
        'sha256': 'a' * 64, 'bytes': 4096, 'target_commit': 'b' * 40}


def test_manifest_requires_every_field():
    for missing in ('name', 'url', 'sha256', 'target_commit'):
        value = {key: item for key, item in GOOD.items() if key != missing}
        with pytest.raises(ValueError):
            updater.valid_manifest(value)


@pytest.mark.parametrize('change', [
    {'sha256': 'not-a-digest'},
    {'url': 'http://example.com/update.zip'},
    {'url': 'https://example.com/update.exe'},
    {'name': '../escape.zip'},
    {'bytes': 10},
    {'bytes': 10 ** 9},
    {'target_commit': 'zz'},
])
def test_manifest_rejects_unsafe_values(change):
    with pytest.raises(ValueError):
        updater.valid_manifest({**GOOD, **change})


def test_check_compares_against_installed_build(tmp_path):
    (tmp_path / 'config').mkdir()
    (tmp_path / 'config/update-source.json').write_text(
        json.dumps({'manifest_url': 'https://example.com/latest.json'}), encoding='utf-8')
    (tmp_path / 'BUILD_INFO.json').write_text(json.dumps({'commit': 'b' * 40}), encoding='utf-8')
    current = updater.check(tmp_path, fetch=lambda url, timeout=20: updater.valid_manifest(GOOD))
    assert current['status'] == 'CURRENT'
    (tmp_path / 'BUILD_INFO.json').write_text(json.dumps({'commit': 'c' * 40}), encoding='utf-8')
    newer = updater.check(tmp_path, fetch=lambda url, timeout=20: updater.valid_manifest(GOOD))
    assert newer['status'] == 'AVAILABLE' and newer['version'] == 'b' * 40


def test_check_is_disabled_without_a_source(tmp_path):
    assert updater.check(tmp_path)['status'] == 'DISABLED'


def test_download_rejects_a_wrong_digest(tmp_path):
    payload = b'x' * 4096

    class Response(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    class Opener:
        def open(self, request, timeout=None):
            return Response(payload)

    with pytest.raises(ValueError):
        updater.download(tmp_path, updater.valid_manifest(GOOD), opener=Opener())
    assert not list((tmp_path / 'runtime/updates').glob('*.zip'))


def test_download_writes_a_verified_package(tmp_path):
    payload = b'x' * 4096
    manifest = updater.valid_manifest({**GOOD, 'sha256': hashlib.sha256(payload).hexdigest()})

    class Response(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    class Opener:
        def open(self, request, timeout=None):
            return Response(payload)

    path = updater.download(tmp_path, manifest, opener=Opener())
    assert path.is_file() and path.stat().st_size == len(payload)


def test_extract_requires_the_update_manifest(tmp_path):
    package = tmp_path / 'pkg.zip'
    with zipfile.ZipFile(package, 'w') as archive:
        archive.writestr('scripts/web_console.py', 'x')
    with pytest.raises(ValueError):
        updater.extract(tmp_path, package)


def test_extract_refuses_path_traversal(tmp_path):
    package = tmp_path / 'pkg.zip'
    with zipfile.ZipFile(package, 'w') as archive:
        archive.writestr('update-manifest.json', '{}')
        archive.writestr('../escape.txt', 'x')
    folder = updater.extract(tmp_path, package)
    assert (folder / 'update-manifest.json').is_file()
    assert not (tmp_path / 'runtime/updates/escape.txt').exists()


def test_cleanup_keeps_only_the_newest_packages(tmp_path):
    folder = tmp_path / 'runtime/updates'
    folder.mkdir(parents=True)
    for index in range(4):
        path = folder / f'p{index}.zip'
        path.write_bytes(b'x')
        os.utime(path, (1_700_000_000 + index, 1_700_000_000 + index))
    updater.cleanup(tmp_path, keep=2)
    assert sorted(p.name for p in folder.glob('*.zip')) == ['p2.zip', 'p3.zip']

