import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import xml.etree.ElementTree as ET

import pytest

ROOT = Path(__file__).resolve().parents[1]
pytestmark = pytest.mark.skipif(sys.platform != 'win32', reason='Windows Sandbox packaging')
SHELL = str(Path(os.environ.get('SystemRoot', 'C:/Windows')) / 'System32/WindowsPowerShell/v1.0/powershell.exe')


@pytest.mark.parametrize('valid_hash', [True, False])
@pytest.mark.parametrize('module_path', ['inherited', 'missing'])
def test_sandbox_maps_only_versioned_input_and_results(tmp_path, valid_hash, module_path):
    root = tmp_path / '选品 & 验收'
    scripts = root / 'scripts'
    dist = root / 'dist'
    scripts.mkdir(parents=True)
    dist.mkdir()
    for name in ('prepare_sandbox_acceptance.ps1', 'run_sandbox_acceptance.ps1'):
        shutil.copyfile(ROOT / 'scripts' / name, scripts / name)
    archive = dist / 'internal.zip'
    archive.write_bytes(b'fixture archive')
    commit = 'a' * 40
    manifest = {'commit': commit, 'internal_package': archive.name,
                'internal_sha256': hashlib.sha256(archive.read_bytes()).hexdigest() if valid_hash else '0' * 64}
    (dist / '最新部署包.json').write_text(json.dumps(manifest), encoding='utf-8')
    result = subprocess.run([SHELL, '-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass',
                             '-File', str(scripts / 'prepare_sandbox_acceptance.ps1')], capture_output=True, timeout=30,
                            env=dict(os.environ) if module_path == 'inherited' else
                            {**os.environ, 'PSModulePath': str(tmp_path / 'agent-module-path')})
    launch = dist / '验收最新部署包.wsb'
    if not valid_hash:
        assert result.returncode != 0
        assert not launch.exists()
        return
    assert result.returncode == 0, result.stderr
    xml = ET.parse(launch).getroot()
    mappings = xml.findall('MappedFolders/MappedFolder')
    assert len(mappings) == 2
    assert [item.findtext('ReadOnly') for item in mappings] == ['true', 'false']
    expected = root / 'runtime/sandbox-acceptance' / commit[:7]
    assert Path(mappings[0].findtext('HostFolder')) == expected / 'input'
    assert Path(mappings[1].findtext('HostFolder')) == expected / 'results'
    assert (expected / 'input/release.zip').read_bytes() == archive.read_bytes()
    assert (expected / 'input/release.commit').read_text().strip() == commit
    assert xml.findtext('ClipboardRedirection') == 'Disable'


def test_acceptance_scripts_parse_in_powershell5():
    command = r'''
$files=@('scripts/prepare_sandbox_acceptance.ps1','scripts/run_sandbox_acceptance.ps1','scripts/build_release.ps1')
foreach($file in $files) {
 $tokens=$null; $errors=$null
 [System.Management.Automation.Language.Parser]::ParseFile((Join-Path (Get-Location) $file),[ref]$tokens,[ref]$errors) | Out-Null
 if($errors) { $errors; exit 1 }
}
'''
    result = subprocess.run([SHELL, '-NoProfile', '-Command', command], cwd=ROOT, capture_output=True, timeout=30)
    assert result.returncode == 0, result.stdout
