"""Add official Windows runtimes, Chromium and AdsPower to the offline dependency set."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import subprocess
import sys
import time
from urllib.request import Request, urlopen
import zipfile

ROOT = Path(__file__).resolve().parents[1]


def sha256(path):
    digest = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def download(url, destination, expected=None):
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.is_file() and (not expected or sha256(destination) == expected.lower()):
        return destination
    temporary = destination.with_suffix(destination.suffix + '.partial')
    for attempt in range(3):
        try:
            with urlopen(Request(url, headers={'User-Agent': 'SelectionWorkbench-Deployment/1'}), timeout=90) as response, temporary.open('wb') as output:
                shutil.copyfileobj(response, output)
            if expected and sha256(temporary) != expected.lower():
                raise RuntimeError('Official download checksum mismatch: ' + destination.name)
            temporary.replace(destination)
            print('Prepared', destination.name, destination.stat().st_size, flush=True)
            return destination
        except (OSError, RuntimeError):
            if attempt == 2:
                raise
            time.sleep(2)


def official_checksum(url, name):
    with urlopen(url, timeout=30) as response:
        raw = response.read()
        lines = raw.decode('utf-16' if raw.startswith((b'\xff\xfe', b'\xfe\xff')) else 'utf-8-sig').splitlines()
    for line in lines:
        fields = line.split()
        if len(fields) == 2 and fields[1].lstrip('*') == name and len(fields[0]) == 64:
            return fields[0].lower()
    raise RuntimeError('Official checksum entry missing: ' + name)


def verify_signature(path):
    command = "$env:PSModulePath=(Join-Path $PSHOME 'Modules')+';'+$env:PSModulePath; $s=Get-AuthenticodeSignature -LiteralPath $env:SELECTION_SIGNED_ASSET; if ($s.Status -ne 'Valid') { exit 1 }"
    result = subprocess.run(['powershell.exe', '-NoProfile', '-NonInteractive', '-Command', command],
        env={**os.environ, 'SELECTION_SIGNED_ASSET': str(path)}, capture_output=True, timeout=60)
    if result.returncode:
        raise RuntimeError('Installer signature verification failed: ' + Path(path).name)


def extract_tools(archive, destination, strip_first=False):
    destination = Path(destination)
    destination.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(archive) as source:
        for entry in source.infolist():
            parts = PurePosixPath(entry.filename.replace('\\', '/')).parts
            if not parts or '..' in parts or ':' in parts[0] or entry.filename.startswith(('/', '\\')):
                raise RuntimeError('Unsafe runtime archive member')
            parts = parts[1:] if strip_first else parts
            if not parts:
                continue
            target = destination.joinpath(*parts)
            if entry.is_dir():
                target.mkdir(parents=True, exist_ok=True)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                with source.open(entry) as data, target.open('wb') as output:
                    shutil.copyfileobj(data, output)


def prepare(root=ROOT):
    root = Path(root).resolve()
    assets = root / 'deployment-assets'
    manifest_path = assets / 'manifest.json'
    if not manifest_path.is_file():
        raise RuntimeError('Run prepare_deployment_assets.py first')
    manifest = json.loads(manifest_path.read_text('utf-8'))
    cache = root / 'work/official-downloads'
    urls = {
        'python-3.12.10-amd64.exe': 'https://www.python.org/ftp/python/3.12.10/python-3.12.10-amd64.exe',
        'VC_redist.x64.exe': 'https://aka.ms/vs/17/release/vc_redist.x64.exe',
        'AdsPower-Global-8.7.23-x64.exe': 'https://version.adspower.net/software/win64-global/8.7.23/AdsPower-Global-8.7.23-x64.exe',
    }
    provenance = {}
    for name, url in urls.items():
        target = download(url, assets / 'installers' / name)
        verify_signature(target)
        provenance[name] = {'url': url, 'sha256': sha256(target), 'authenticode': 'Valid'}

    node_name = 'node-v24.19.0-win-x64.zip'
    node_url = 'https://nodejs.org/dist/v24.19.0/'
    node_hash = official_checksum(node_url + 'SHASUMS256.txt', node_name)
    node_zip = download(node_url + node_name, cache / node_name, node_hash)
    extract_tools(node_zip, assets / 'tools/node', strip_first=True)
    verify_signature(assets / 'tools/node/node.exe')
    provenance['node'] = {'url': node_url + node_name, 'sha256': node_hash}

    shell_name = 'PowerShell-7.6.5-win-x64.zip'
    shell_url = 'https://github.com/PowerShell/PowerShell/releases/download/v7.6.5/'
    shell_hash = official_checksum(shell_url + 'hashes.sha256', shell_name)
    shell_zip = download(shell_url + shell_name, cache / shell_name, shell_hash)
    extract_tools(shell_zip, assets / 'tools/powershell')
    verify_signature(assets / 'tools/powershell/pwsh.exe')
    provenance['powershell'] = {'url': shell_url + shell_name, 'sha256': shell_hash}

    browser_cache = assets / 'browsers'
    subprocess.run([str(assets / 'tools/node/node.exe'), str(assets / 'node_modules/playwright/cli.js'), 'install', 'chromium'],
        cwd=root, env={**os.environ, 'PLAYWRIGHT_BROWSERS_PATH': str(browser_cache)}, check=True)
    # Playwright's machine-specific package links are regenerated by the target install.
    links = browser_cache / '.links'
    if links.is_dir():
        for link in links.iterdir():
            if link.is_file():
                link.unlink()
    files = sorted(p for p in assets.rglob('*') if p.is_file() and p != manifest_path)
    manifest.update(complete_offline_dependencies=True, download_provenance=provenance,
        source_commit=subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=root, text=True).strip(),
        files={p.relative_to(assets).as_posix(): {'bytes': p.stat().st_size, 'sha256': sha256(p)} for p in files})
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(json.dumps({'complete': True, 'files': len(files), 'bytes': sum(p.stat().st_size for p in files)}, ensure_ascii=False), flush=True)
    return manifest


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=ROOT)
    args = parser.parse_args()
    prepare(args.root)
