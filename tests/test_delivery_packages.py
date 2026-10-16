import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import zipfile

import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('delivery_builder', ROOT / 'scripts/build_delivery_packages.py')
builder = importlib.util.module_from_spec(spec)
spec.loader.exec_module(builder)


def ps(script, tmp_path, **extra):
    path = tmp_path / 'test.ps1'
    path.write_text(script, encoding='utf-8-sig')
    environment = {**os.environ, **extra}
    modules = Path(environment['WINDIR']) / 'System32/WindowsPowerShell/v1.0/Modules'
    environment['PSModulePath'] = str(modules) + ';' + environment.get('PSModulePath', '')
    # Files wait only for PowerShell, even if a descendant retains its stdio.
    stdout_path, stderr_path = tmp_path / 'stdout.log', tmp_path / 'stderr.log'
    with stdout_path.open('wb') as stdout, stderr_path.open('wb') as stderr:
        result = subprocess.run(['powershell.exe', '-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass', '-File', str(path)],
                                cwd=ROOT, env=environment, stdout=stdout, stderr=stderr, timeout=120)
    assert result.returncode == 0, stderr_path.read_text('utf-8', errors='replace')
    return stdout_path.read_text('utf-8-sig').strip()


def test_private_paths_and_secrets_blocked_without_printing_values():
    for name in ['company.local.json', 'start-web.local.json', 'runtime/secrets/company.dpapi', '.git/config', '../company.local.json',
                 'runs/test/products.jsonl', 'config/sessions.json', '.deployment-backup/previous-company.dpapi']:
        assert not builder.included(name)
    secret = 'TEST_FOR_SCANNER_UNSAFE_VALUE'
    findings = builder.scan_files({'config/unsafe.json': json.dumps({'proxy': {'password': secret}}).encode()})
    assert findings == [{'file': 'config/unsafe.json', 'rule': 'literal-credential', 'field': 'proxy.password'}]
    assert secret not in json.dumps(findings)
    assert not builder.scan_files({'config/example.json': b'{"api_key":"REPLACE_WITH_KEY"}'})
    assert builder.scan_files({'src/unsafe.py': b'url="https://TEST_ONLY_USER:TEST_ONLY_PASSWORD@proxy.example"'})
    assert not builder.scan_files({'tests/security.py': b'url="https://user:secret@www.temu.com/api/search"'})


@pytest.mark.skipif(sys.platform != 'win32', reason='native Windows entry')
def test_confirmed_fake_pack_and_cancel_before_config_read(tmp_path):
    base = tmp_path / 'base.zip'
    with zipfile.ZipFile(base, 'w') as archive:
        archive.writestr('BUILD_INFO.json', json.dumps({'commit': 'fixture', 'contains_company_credentials': False}))
        archive.writestr('deploy.cmd', '@echo off')
    fake = tmp_path / 'company.fake.json'
    fake.write_text(json.dumps({'adspower_api_key': 'TEST_ONLY_KEY', 'proxy': {'username': 'TEST_ONLY_USER_session-fake',
                                                                              'password': 'TEST_ONLY_PASSWORD'}}))
    output = ps(r'''
. (Join-Path $env:SOURCE_ROOT 'scripts\package_company_config.ps1')
$denied=$false
try { Add-CompanyConfigToPackage -BaseZip "$env:TEST_ROOT\base.zip" -ConfigPath "$env:TEST_ROOT\MISSING.json" -Destination "$env:TEST_ROOT\denied.zip" -HumanConfirmed $false }
catch { $denied=$_.Exception.Message.StartsWith('未确认') }
if (-not $denied -or (Test-Path "$env:TEST_ROOT\denied.zip")) { throw 'Cancellation failed' }
Add-CompanyConfigToPackage -BaseZip "$env:TEST_ROOT\base.zip" -ConfigPath "$env:TEST_ROOT\company.fake.json" -Destination "$env:TEST_ROOT\confirmed.zip" -HumanConfirmed $true | Out-Null
$rejected=$false
try { Add-CompanyConfigToPackage -BaseZip "$env:TEST_ROOT\base.zip" -ConfigPath "$env:TEST_ROOT\MISSING.json" -Destination "$env:TEST_ROOT\broken.zip" -HumanConfirmed $true }
catch { $rejected=$true }
if (-not $rejected -or (Test-Path "$env:TEST_ROOT\broken.zip")) { throw 'Failed package not rolled back' }
'OK'
''', tmp_path, SOURCE_ROOT=str(ROOT), TEST_ROOT=str(tmp_path))
    assert output == 'OK'
    with zipfile.ZipFile(tmp_path / 'confirmed.zip') as archive:
        assert json.loads(archive.read('company.local.json'))['adspower_api_key'] == 'TEST_ONLY_KEY'
        assert json.loads(archive.read('BUILD_INFO.json'))['contains_company_credentials'] is True
    with zipfile.ZipFile(base) as archive:
        assert 'company.local.json' not in archive.namelist()
    assert fake.exists()


@pytest.mark.skipif(sys.platform != 'win32', reason='native Windows recovery')
@pytest.mark.parametrize('old_company', [False, True])
def test_install_failure_restores_environment_data_and_fake_company(tmp_path, old_company):
    for relative in ['.venv', 'node_modules', 'runtime/agent-config', 'runs/retained']:
        directory = tmp_path / relative
        directory.mkdir(parents=True, exist_ok=True)
        (directory / 'before.txt').write_text('ORIGINAL')
    secret = tmp_path / 'runtime/secrets/company.dpapi'
    secret.parent.mkdir(parents=True)
    if old_company:
        secret.write_bytes(b'FAKE_OLD_CIPHERTEXT')
    output = ps(r'''
. (Join-Path $env:SOURCE_ROOT 'scripts\deployment_guard.ps1')
$transaction=Start-ProjectDeployment -Root $env:TEST_ROOT -ImportCompany $true
foreach ($name in @('.venv','node_modules','runtime\agent-config')) {
 $target=Join-Path $env:TEST_ROOT $name
 New-Item -ItemType Directory -Path $target -Force | Out-Null
 [IO.File]::WriteAllText((Join-Path $target 'failed.txt'),'FAILED')
}
[IO.File]::WriteAllText((Join-Path $env:TEST_ROOT 'runtime\secrets\company.dpapi'),'FAKE_NEW_CIPHERTEXT')
Restore-ProjectDeployment $transaction
Write-DeploymentStatus -Root $env:TEST_ROOT -Stage 'TEST_ONLY_FAIL' -Succeeded $false -RolledBack $true
$outsideBlocked=$false
try { Assert-ProjectTarget $env:TEST_ROOT (Join-Path $env:TEST_ROOT '..\outside') | Out-Null } catch { $outsideBlocked=$true }
if (-not $outsideBlocked) { throw 'Outside root allowed' }
'OK'
''', tmp_path, SOURCE_ROOT=str(ROOT), TEST_ROOT=str(tmp_path))
    assert output == 'OK'
    for relative in ['.venv', 'node_modules', 'runtime/agent-config', 'runs/retained']:
        assert (tmp_path / relative / 'before.txt').read_text() == 'ORIGINAL'
        assert not (tmp_path / relative / 'failed.txt').exists()
    assert secret.read_bytes() == b'FAKE_OLD_CIPHERTEXT' if old_company else not secret.exists()
    status = json.loads((tmp_path / 'runtime/install-status.json').read_text())
    assert status['project_environment_rolled_back'] is True
    assert 'FAKE_NEW_CIPHERTEXT' not in json.dumps(status)


@pytest.mark.skipif(sys.platform != 'win32', reason='DPAPI current user')
def test_fake_config_import_into_isolated_project(tmp_path):
    import shutil
    shutil.copytree(ROOT / 'src', tmp_path / 'src')
    (tmp_path / 'scripts').mkdir()
    shutil.copy2(ROOT / 'scripts/company_setup.py', tmp_path / 'scripts/company_setup.py')
    fake = {'adspower_api_key': 'TEST_ONLY_KEY', 'proxy': {'host': 'proxy.example', 'port': 5959,
            'username': 'TEST_ONLY_USER_session-fake', 'password': 'TEST_ONLY_PASSWORD'}}
    config = tmp_path / 'fake.json'
    config.write_text(json.dumps(fake))
    result = subprocess.run([sys.executable, str(tmp_path / 'scripts/company_setup.py'), '--import-file', str(config)],
                            cwd=tmp_path, capture_output=True, timeout=15)
    assert result.returncode == 0, result.stderr
    from selection_automation.secrets import load
    cipher = tmp_path / 'runtime/secrets/company.dpapi'
    imported = load(cipher)
    assert imported['proxy'] == fake['proxy']
    assert imported['adspower_api_key'] == fake['adspower_api_key']
    assert imported['schema_version'] == 'company-bundle-v2'
    assert b'TEST_ONLY_KEY' not in cipher.read_bytes()
    assert b'TEST_ONLY_KEY' not in result.stdout + result.stderr


@pytest.mark.skipif(sys.platform != 'win32', reason='installer control flow')
@pytest.mark.parametrize('mode', ['cancel', 'dependencies', 'verification', 'success'])
def test_actual_installer_flow_with_only_fake_commands(tmp_path, mode):
    """Run install.ps1's real branches, replacing all installs/APIs/GUI with fixtures."""
    import shutil
    (tmp_path / 'scripts').mkdir()
    shutil.copy2(ROOT / 'scripts/deployment_guard.ps1', tmp_path / 'scripts/deployment_guard.ps1')
    for relative in ['requirements.lock.txt', 'package-lock.json', 'scripts/company_setup.py', 'web/index.html']:
        path = tmp_path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('TEST_ONLY')
    for relative in ['.venv', 'node_modules', 'runtime/agent-config', 'runs/retained']:
        path = tmp_path / relative
        path.mkdir(parents=True, exist_ok=True)
        (path / 'before.txt').write_text('ORIGINAL')
    secret = tmp_path / 'runtime/secrets/company.dpapi'
    secret.parent.mkdir(parents=True)
    secret.write_bytes(b'TEST_ONLY_OLD_CIPHER')
    # Invalid plaintext on purpose: cancellation must happen without parsing it.
    (tmp_path / 'company.local.json').write_text('TEST_ONLY_INVALID_JSON_NOT_TO_READ')
    injected = r'''
function Get-DeploymentPreflight {
 param($Root)
 [pscustomobject]@{platform='Windows';source_complete=$true;free_gb=100;configuration_read=$false}
}
function Confirm-CompanyImport {
 param($ConfigPath,$Root)
 [IO.File]::WriteAllText((Join-Path $Root 'TEST_ONLY_CONFIRM_CALLED'),'called')
 if ($env:TEST_MODE -eq 'cancel') { throw 'TEST_ONLY_CANCELLED' }
}
function Find-Python312 { return 'TEST_ONLY_PYTHON' }
function Has-Node20 { return $true }
function Has-SystemPowerShell7 { return $true }
function Has-VCRuntime { return $true }
function Ensure-NetworkClient { }
function Find-NodeToolchain { return [pscustomobject]@{Directory=$env:TEMP;Npm='TEST_ONLY_NPM';Npx='TEST_ONLY_NPX'} }
function Get-CimInstance { return @() }
function Checked {
 param([string]$Exe,[string[]]$Arguments)
 if ($Exe -eq 'TEST_ONLY_PYTHON') {
  $directory=Join-Path $root '.venv\Scripts'
  New-Item -ItemType Directory -Path $directory -Force | Out-Null
  [IO.File]::WriteAllText((Join-Path $directory 'python.exe'),'TEST_ONLY_NEW_ENV')
 }
 if ($env:TEST_MODE -eq 'dependencies' -and $Arguments -contains 'pip') { throw 'TEST_ONLY_PIP_FAIL' }
 if ($Arguments -contains '--import-file') { [IO.File]::WriteAllText((Join-Path $root 'runtime\secrets\company.dpapi'),'TEST_ONLY_NEW_CIPHER') }
 if ($env:TEST_MODE -eq 'verification' -and $Arguments -contains 'scripts/verify_deployment.py') { throw 'TEST_ONLY_VERIFY_FAIL' }
 $global:LASTEXITCODE=0
}
'''
    install = (ROOT / 'install.ps1').read_text(encoding='utf-8-sig')
    anchor = ". (Join-Path $root 'scripts\\deployment_guard.ps1')"
    assert anchor in install
    (tmp_path / 'install.ps1').write_text(install.replace(anchor, anchor + '\n' + injected), encoding='utf-8-sig')
    result = subprocess.run(['powershell.exe', '-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass',
                             '-File', str(tmp_path / 'install.ps1'), '-SkipAdsPower', '-SkipModelDownload'],
                            cwd=tmp_path, env={**os.environ, 'TEST_MODE': mode}, capture_output=True, timeout=30)
    assert result.returncode == (0 if mode == 'success' else 1), result.stderr
    assert (tmp_path / 'TEST_ONLY_CONFIRM_CALLED').exists()
    assert (tmp_path / 'runs/retained/before.txt').read_text() == 'ORIGINAL'
    if mode != 'success':
        assert (tmp_path / '.venv/before.txt').read_text() == 'ORIGINAL'
        assert (tmp_path / 'node_modules/before.txt').read_text() == 'ORIGINAL'
        assert secret.read_bytes() == b'TEST_ONLY_OLD_CIPHER'
    if mode in ('dependencies', 'verification'):
        status = json.loads((tmp_path / 'runtime/install-status.json').read_text(encoding='utf-8'))
        assert status['project_environment_rolled_back'] is True
    if mode == 'success':
        assert not (tmp_path / '.venv/before.txt').exists()
        assert secret.read_bytes() == b'TEST_ONLY_NEW_CIPHER'
        assert json.loads((tmp_path / 'runtime/install-status.json').read_text(encoding='utf-8'))['succeeded'] is True


@pytest.mark.skipif(sys.platform != 'win32', reason='native Windows preflight')
def test_deep_install_path_is_explained_before_config_or_environment_changes(tmp_path):
    output=ps(r'''
. (Join-Path $env:SOURCE_ROOT 'scripts\deployment_guard.ps1')
Assert-DeploymentPathLength 'D:\selection'
$long = 'D:\' + ('x' * 110)
try { Get-DeploymentPreflight $long; throw 'Expected deep path rejection' }
catch { if (-not $_.Exception.Message.StartsWith('安装路径过长：')) { throw }; }
'OK'
''',tmp_path,SOURCE_ROOT=str(ROOT))
    assert output=='OK'
