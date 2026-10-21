"""Exercise real installer branches without installing tools or calling services."""
import ctypes
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import zipfile

import pytest

ROOT = Path(__file__).resolve().parents[1]
pytestmark = pytest.mark.skipif(sys.platform != 'win32', reason='Windows installer')
spec = importlib.util.spec_from_file_location('asset_delivery_builder', ROOT / 'scripts/build_delivery_packages.py')
builder = importlib.util.module_from_spec(spec)
spec.loader.exec_module(builder)


@pytest.mark.parametrize('corrupt', [False, True])
def test_extracted_package_uses_and_checks_bundled_dependencies(corrupt):
    with tempfile.TemporaryDirectory(prefix='deploy-assets-', dir=ROOT.parent) as directory:
        base = Path(directory)
        source = base / 'source'
        assets = source / 'deployment-assets'
        contents = {
            'wheelhouse/pip-fixture.whl': b'TEST_ONLY_WHEEL',
            'node_modules/playwright/package.json': b'{"name":"playwright"}',
            'node_modules/playwright-core/package.json': b'{"name":"playwright-core"}',
            'models/resnet50-11ad3fa6.pth': b'TEST_ONLY_MODEL',
        }
        for name, data in contents.items():
            path = assets / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
        manifest = {'files': {name: {'sha256': hashlib.sha256(data).hexdigest()} for name, data in contents.items()}}
        (assets / 'manifest.json').write_text(json.dumps(manifest), encoding='utf-8')
        if corrupt:
            (assets / 'models/resnet50-11ad3fa6.pth').write_bytes(b'TEST_ONLY_CORRUPTED')
        files = builder.bundled_assets(source)
        for name in ('install.ps1', 'scripts/deployment_guard.ps1', 'requirements.lock.txt',
                     'package-lock.json', 'scripts/company_setup.py', 'web/index.html'):
            files[name] = (ROOT / name).read_bytes()
        package = base / 'fixture.zip'
        builder.write_zip(package, files)
        target = base / 'app'
        with zipfile.ZipFile(package) as archive:
            archive.extractall(target)
        injected = r'''
function Find-Python312 { return 'TEST_ONLY_PYTHON' }
function Has-Node20 { return $true }
function Has-SystemPowerShell7 { return $true }
function Has-VCRuntime { return $true }
function Ensure-NetworkClient { }
function Find-NodeToolchain { return [pscustomobject]@{Directory=$env:TEMP;Npm='TEST_ONLY_NPM';Npx='TEST_ONLY_NPX'} }
function Get-CimInstance { return @() }
function Checked {
 param([string]$Exe,[string[]]$Arguments)
 [pscustomobject]@{exe=$Exe;arguments=@($Arguments)} | ConvertTo-Json -Compress | Add-Content -LiteralPath $env:COMMAND_LOG -Encoding UTF8
 if ($Exe -eq 'TEST_ONLY_NPM') { throw 'Unexpected online npm install' }
 if ($Arguments -contains 'pip' -and $Arguments -notcontains '--no-index') { throw 'Unexpected online pip install' }
 if ($Arguments -contains '-c' -and ($Arguments -join ' ') -match 'resnet50') { throw 'Unexpected model download' }
 if ($Arguments -contains 'venv') {
  $folder=Join-Path $root '.venv\Scripts'
  New-Item -ItemType Directory -Path $folder -Force | Out-Null
  [IO.File]::WriteAllText((Join-Path $folder 'python.exe'),'TEST_ONLY_ENV')
 }
 $global:LASTEXITCODE=0
}
'''
        script = target / 'install.ps1'
        original = script.read_text(encoding='utf-8-sig')
        anchor = ". (Join-Path $root 'scripts\\deployment_guard.ps1')"
        assert original.count(anchor) == 1
        script.write_text(original.replace(anchor, anchor + '\n' + injected), encoding='utf-8-sig')
        log = base / 'commands.jsonl'
        result = subprocess.run(['powershell.exe', '-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass',
                                 '-File', str(script), '-SkipAdsPower', '-SkipProxyClient'],
                                env={**os.environ, 'COMMAND_LOG': str(log)}, capture_output=True, timeout=30)
        diagnostic = (result.stdout + result.stderr).decode('utf-8', errors='replace')
        commands = [json.loads(line) for line in log.read_text(encoding='utf-8-sig').splitlines()] if log.exists() else []
        pip_calls = [call for call in commands if 'pip' in call['arguments']]
        status = json.loads((target / 'runtime/install-status.json').read_text(encoding='utf-8-sig'))
        if corrupt:
            assert result.returncode != 0, diagnostic
            assert not pip_calls
            assert status['succeeded'] is False
            # Asset verification now runs before any environment is moved or tool executed.
            assert status['project_environment_rolled_back'] is False
        else:
            assert result.returncode == 0, diagnostic
            assert status['succeeded'] is True
            assert len(pip_calls) == 3
            assert all('--no-index' in call['arguments'] for call in pip_calls)
            assert (target / 'node_modules/playwright/package.json').read_bytes() == contents['node_modules/playwright/package.json']
            assert (target / 'node_modules/playwright-core/package.json').is_file()
            assert (target / 'runtime/models/torch/hub/checkpoints/resnet50-11ad3fa6.pth').read_bytes() == contents['models/resnet50-11ad3fa6.pth']


def test_real_python_import_check_works_under_windows_powershell(tmp_path):
    harness = r'''
$ErrorActionPreference='Stop'
$tokens=$null; $errors=$null
$ast=[System.Management.Automation.Language.Parser]::ParseFile($env:INSTALL_FILE,[ref]$tokens,[ref]$errors)
if ($errors) { throw ($errors | Out-String) }
foreach ($statement in $ast.EndBlock.Statements) {
 if ($statement -is [System.Management.Automation.Language.FunctionDefinitionAst] -and $statement.Name -eq 'Checked') { . ([scriptblock]::Create($statement.Extent.Text)) }
}
$python=$env:TEST_PYTHON
$checks=@($ast.FindAll({param($node) $node -is [System.Management.Automation.Language.PipelineAst] -and $node.Extent.Text.StartsWith('Checked $python') -and $node.Extent.Text.Contains('Python dependencies loaded')},$true))
if ($checks.Count -ne 1) { throw 'Expected the real Python dependency check' }
. ([scriptblock]::Create($checks[0].Extent.Text))
'''
    script = tmp_path / 'real-python-check.ps1'
    script.write_text(harness, encoding='utf-8-sig')
    result = subprocess.run(['powershell.exe', '-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass', '-File', str(script)],
                            env={**os.environ, 'INSTALL_FILE': str(ROOT / 'install.ps1'), 'TEST_PYTHON': sys.executable},
                            capture_output=True, timeout=60)
    assert result.returncode == 0, result.stderr
    assert b'Python dependencies loaded' in result.stdout


def test_python_install_target_with_spaces_remains_one_argument(tmp_path):
    harness = r'''
$ErrorActionPreference='Stop'
$tokens=$null; $errors=$null
$ast=[System.Management.Automation.Language.Parser]::ParseFile($env:INSTALL_FILE,[ref]$tokens,[ref]$errors)
if ($errors) { throw ($errors | Out-String) }
foreach ($statement in $ast.EndBlock.Statements) {
 if ($statement -is [System.Management.Automation.Language.FunctionDefinitionAst]) { . ([scriptblock]::Create($statement.Extent.Text)) }
}
$root=$env:PROBE_ROOT
$env:LOCALAPPDATA='C:\Users\Test User\AppData\Local'
function Invoke-WebRequest { param($Uri,$OutFile,[switch]$UseBasicParsing) }
function Get-AuthenticodeSignature { param($LiteralPath) return [pscustomobject]@{Status='Valid'} }
function Start-Process {
 param($FilePath,$ArgumentList,$WindowStyle,[switch]$Wait,[switch]$PassThru)
 $script:CapturedArguments=@($ArgumentList)
 $process=[pscustomobject]@{ExitCode=0;Handle=[IntPtr]1;Id=4242;StartTime=(Get-Date)}
 $process | Add-Member -MemberType ScriptMethod -Name WaitForExit -Value {$script:FixtureWaited=$true}
 $process | Add-Member -MemberType ScriptMethod -Name Refresh -Value {$script:FixtureRefreshed=$true}
 $process | Add-Member -MemberType ScriptMethod -Name Dispose -Value {$script:FixtureDisposed=$true}
 return $process
}
function Refresh-Path { }
Install-FixedRuntime 'python'
@{arguments=@($script:CapturedArguments);waited=[bool]$script:FixtureWaited;refreshed=[bool]$script:FixtureRefreshed;disposed=[bool]$script:FixtureDisposed}|ConvertTo-Json -Compress
'''
    script = tmp_path / 'probe.ps1'
    script.write_text(harness, encoding='utf-8-sig')
    result = subprocess.run(['powershell.exe', '-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass', '-File', str(script)],
                            env={**os.environ, 'INSTALL_FILE': str(ROOT / 'install.ps1'), 'PROBE_ROOT': str(tmp_path)},
                            capture_output=True, timeout=30)
    assert result.returncode == 0, result.stderr
    output = json.loads(result.stdout.decode('utf-8-sig'))
    assert output['waited'] and output['refreshed'] and output['disposed']
    arguments = output['arguments']
    parse = ctypes.windll.shell32.CommandLineToArgvW
    parse.argtypes = (ctypes.c_wchar_p, ctypes.POINTER(ctypes.c_int))
    parse.restype = ctypes.POINTER(ctypes.c_wchar_p)
    count = ctypes.c_int()
    argv = parse('installer.exe ' + ' '.join(arguments), ctypes.byref(count))
    assert argv
    try:
        received = list(argv[:count.value])[1:]
        assert len(received) == len(arguments)
        assert r'TargetDir=C:\Users\Test User\AppData\Local\Programs\Python\Python312' in received
    finally:
        ctypes.windll.kernel32.LocalFree.argtypes = (ctypes.c_void_p,)
        ctypes.windll.kernel32.LocalFree(ctypes.cast(argv, ctypes.c_void_p))
