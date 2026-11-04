"""Check, download and verify incremental updates without touching user data.

The updater never replaces files itself: it downloads and verifies a package,
then hands the extracted folder to apply_update.py, which already fences live
jobs, backs up, applies atomically and rolls back on failure.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import tarfile
import zipfile
from pathlib import Path
from urllib.parse import urlsplit
from urllib.request import Request, build_opener

SHA256 = re.compile(r'[0-9a-f]{64}')
COMMIT = re.compile(r'[0-9a-f]{7,40}')
MAX_BYTES = 200 * 1024 * 1024
CONFIG = 'config/update-source.json'
STATE = 'runtime/updates/state.json'
CACHE = 'runtime/updates'


def source(root):
    """Configured update source; disabled unless a manifest URL is present."""
    path = Path(root) / CONFIG
    value = {}
    try:
        value = json.loads(path.read_text('utf-8'))
    except (OSError, ValueError):
        value = {}
    if not isinstance(value, dict):
        value = {}
    url = value.get('manifest_url') or os.getenv('SELECTION_UPDATE_MANIFEST', '')
    return {'manifest_url': url.strip() if isinstance(url, str) else '',
            'auto_apply': bool(value.get('auto_apply', True)),
            'interval_seconds': max(60, min(3600, int(value.get('interval_seconds') or 300)))}


def local_commit(root):
    try:
        info = json.loads((Path(root) / 'BUILD_INFO.json').read_text('utf-8'))
        commit = info.get('commit')
        return commit if isinstance(commit, str) and COMMIT.fullmatch(commit) else None
    except (OSError, ValueError):
        return None


def valid_manifest(value):
    """Reject anything that is not a bounded, fully specified update."""
    if not isinstance(value, dict):
        raise ValueError('更新清单格式无效')
    for key in ('name', 'url', 'sha256', 'target_commit'):
        if not isinstance(value.get(key), str) or not value[key].strip():
            raise ValueError('更新清单缺少 ' + key)
    if not SHA256.fullmatch(value['sha256']):
        raise ValueError('更新清单的校验值无效')
    if not COMMIT.fullmatch(value['target_commit']):
        raise ValueError('更新清单的目标版本无效')
    size = value.get('bytes')
    if type(size) is not int or not 4 * 1024 <= size <= MAX_BYTES:
        raise ValueError('更新包大小超出允许范围')
    name = value['name']
    if not name.endswith('.zip') or Path(name).name != name or '..' in name:
        raise ValueError('更新包文件名无效')
    parsed = urlsplit(value['url'])
    if parsed.scheme != 'https' or not parsed.hostname or not parsed.path.lower().endswith('.zip'):
        raise ValueError('更新包地址必须是 HTTPS 的 zip')
    return {'name': name, 'url': value['url'], 'sha256': value['sha256'], 'bytes': size,
            'target_commit': value['target_commit'],
            'base_commit': value.get('base_commit'), 'channel': value.get('channel') or 'internal',
            'changed_files': value.get('changed_files')}


def fetch_manifest(url, timeout=20):
    opener = build_opener()
    request = Request(url, headers={'User-Agent': 'selection-workbench-updater', 'Accept': 'application/json'})
    with opener.open(request, timeout=timeout) as response:
        raw = response.read(1024 * 1024)
    return valid_manifest(json.loads(raw.decode('utf-8')))


def check(root, fetch=fetch_manifest, timeout=20):
    """Compare the remote manifest with the installed build; never writes files."""
    source_now = source(root)
    if not source_now['manifest_url']:
        return {'status': 'DISABLED', 'reason': '未配置更新地址'}
    try:
        manifest = fetch(source_now['manifest_url'], timeout)
    except Exception as error:
        return {'status': 'ERROR', 'reason': type(error).__name__}
    current = local_commit(root)
    if current and manifest['target_commit'] == current:
        return {'status': 'CURRENT', 'version': current, 'manifest': manifest}
    return {'status': 'AVAILABLE', 'version': manifest['target_commit'], 'current': current,
            'manifest': manifest, 'auto_apply': source_now['auto_apply']}


def download(root, manifest, timeout=120, opener=None):
    """Download to a temporary file and verify size and digest before use."""
    folder = Path(root) / CACHE
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / manifest['name']
    partial = folder / (manifest['name'] + '.part')
    client = opener or build_opener()
    digest = hashlib.sha256()
    total = 0
    request = Request(manifest['url'], headers={'User-Agent': 'selection-workbench-updater'})
    with client.open(request, timeout=timeout) as response, partial.open('wb') as out:
        while True:
            chunk = response.read(256 * 1024)
            if not chunk:
                break
            total += len(chunk)
            if total > manifest['bytes']:
                break
            digest.update(chunk)
            out.write(chunk)
    if total != manifest['bytes'] or digest.hexdigest() != manifest['sha256']:
        partial.unlink(missing_ok=True)
        raise ValueError('更新包校验失败')
    os.replace(partial, target)
    return target


def extract(root, package):
    """Unpack beside the download and require the updater manifest at the root."""
    folder = Path(root) / CACHE / (Path(package).stem + '.dir')
    if folder.exists():
        shutil.rmtree(folder, ignore_errors=True)
    folder.mkdir(parents=True)
    with zipfile.ZipFile(package) as archive:
        for row in archive.infolist():
            name = row.filename
            if name.startswith('/') or '..' in Path(name).parts or row.is_dir():
                continue
            destination = folder / name
            destination.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(row) as source_file, destination.open('wb') as target_file:
                shutil.copyfileobj(source_file, target_file)
    if not (folder / 'update-manifest.json').is_file():
        raise ValueError('更新包缺少 update-manifest.json')
    return folder


def cleanup(root, keep=2):
    """Keep the newest packages only; never touch runs, runtime data or secrets."""
    folder = Path(root) / CACHE
    if not folder.is_dir():
        return
    packages = sorted(folder.glob('*.zip'), key=lambda p: p.stat().st_mtime, reverse=True)
    for stale in packages[keep:]:
        stale.unlink(missing_ok=True)
    for stale in folder.glob('*.dir'):
        if stale.name[:-4] + '.zip' not in {p.name for p in packages[:keep]}:
            shutil.rmtree(stale, ignore_errors=True)
