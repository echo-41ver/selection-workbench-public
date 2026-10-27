"""Run the shipped PowerShell setup against an existing managed D1 fixture."""
import json
import os
from pathlib import Path
import shutil
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.skipif(os.name != 'nt', reason='Windows native D1 setup')
def test_managed_d1_with_different_line_endings_is_retained(tmp_path):
    scripts = tmp_path / 'scripts'
    scripts.mkdir()
    shutil.copy2(ROOT / 'scripts/setup_local_d1.ps1', scripts / 'setup_local_d1.ps1')
    template = (ROOT / 'scripts/local_d1_api.py').read_text('utf-8').replace('\r\n', '\n')
    (scripts / 'local_d1_api.py').write_bytes(template.replace('\n', '\r\n').encode())
    model = tmp_path / 'native model'
    (model / 'model').mkdir(parents=True)
    (model / 'model/config.json').write_text('{"auto_map":{"AutoModel":"modeling_d1.D1Model"}}')
    (model / '.venv/Scripts').mkdir(parents=True)
    (model / '.venv/Scripts/python.exe').write_bytes(b'TEST_ONLY_NOT_EXECUTABLE')
    (model / 'runtime.py').write_text('# TEST_ONLY')
    app = model / 'app.py'
    app.write_bytes(template.encode())
    harness = tmp_path / 'check.ps1'
    harness.write_text(r'''
$ErrorActionPreference='Stop'
$fixture=Join-Path $PSScriptRoot 'native model'
function Get-NetTCPConnection { [pscustomobject]@{OwningProcess=99999} }
function Invoke-RestMethod { [pscustomobject]@{ready=$true;model='LiquidAI/d1-3B';api_version='swb-native-d1-concurrent-v1'} }
function Get-CimInstance { [pscustomobject]@{CommandLine=('python -u "'+(Join-Path $fixture 'app.py')+'"')} }
function Get-ScheduledTask { [pscustomobject]@{State='Running';Actions=[pscustomobject]@{Arguments=(' -File "'+(Join-Path $fixture 'selection-workbench-service.ps1')+'"')}} }
function Get-FileHash { throw 'Managed text match must not hash the model script' }
function Stop-Process { throw 'Do not stop the existing managed service' }
function Register-ScheduledTask { throw 'Do not replace the existing managed task' }
& (Join-Path $PSScriptRoot 'scripts/setup_local_d1.ps1')
exit $LASTEXITCODE
''', encoding='utf-8-sig')
    result = subprocess.run(['powershell.exe', '-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass', '-File', str(harness)],
                            cwd=tmp_path, capture_output=True, timeout=30)
    assert result.returncode == 0, result.stderr
    receipt = json.loads((tmp_path / 'runtime/local-models/native-d1-setup.json').read_text('utf-8-sig'))
    assert receipt['stage'] == '完成' and receipt['independent_of_codex'] is True
    assert app.read_bytes() == template.encode()
