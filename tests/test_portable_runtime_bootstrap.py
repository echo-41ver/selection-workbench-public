"""A slow first portable start must not download a duplicate system installer."""
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

ROOT=Path(__file__).resolve().parents[1]


@pytest.mark.skipif(sys.platform!='win32',reason='Windows runtime bootstrap')
def test_prepare_vc_before_portable_probes_and_retry_cached_powershell(tmp_path):
    tools=tmp_path/'deployment-assets/tools/powershell'
    tools.mkdir(parents=True)
    (tools/'pwsh.exe').write_bytes(b'TEST_ONLY_NOT_EXECUTED')
    script=tmp_path/'bootstrap.ps1'
    script.write_text(r'''
$ErrorActionPreference='Stop'
$env:PSModulePath=(Join-Path $PSHOME 'Modules')+';'+$env:PSModulePath
$tokens=$null;$errors=$null
$ast=[System.Management.Automation.Language.Parser]::ParseFile($env:INSTALL_SOURCE,[ref]$tokens,[ref]$errors)
if ($errors) { throw ($errors|Out-String) }
foreach ($statement in $ast.EndBlock.Statements) {
 if ($statement -is [System.Management.Automation.Language.FunctionDefinitionAst]) { . ([scriptblock]::Create($statement.Extent.Text)) }
}
$root=$env:PROBE_ROOT
$script:vcReady=$false
$script:shellProbes=0
function Refresh-Path { }
function Has-VCRuntime { return $script:vcReady }
function Install-Package { param($Id) if ($Id -ne 'Microsoft.VCRedist.2015+.x64') { throw 'Unexpected network package' };$script:vcReady=$true }
function Find-Python312 { return 'TEST_ONLY_EXISTING_PYTHON' }
function Has-Node20 { if (-not $script:vcReady) { throw 'Portable Node probed before VC runtime' };return $true }
function Has-SystemPowerShell7 { $script:shellProbes++;return $script:shellProbes -gt 1 }
function Install-FixedRuntime { throw 'Must not download an already bundled portable runtime' }
Ensure-SystemTools
if ($script:shellProbes -lt 2) { throw 'Cold start was not retried' }
Write-Output 'OFFLINE_PORTABLE_BOOTSTRAP_OK'
''',encoding='utf-8-sig')
    result=subprocess.run(['powershell.exe','-NoProfile','-NonInteractive','-ExecutionPolicy','Bypass','-File',str(script)],
        env={**os.environ,'INSTALL_SOURCE':str(ROOT/'install.ps1'),'PROBE_ROOT':str(tmp_path)},capture_output=True,timeout=30)
    assert result.returncode==0,result.stderr
    assert b'OFFLINE_PORTABLE_BOOTSTRAP_OK' in result.stdout


@pytest.mark.skipif(sys.platform != 'win32', reason='Windows portable runtime')
def test_install_uses_bundled_tools_without_overwriting_loaded_dll(tmp_path):
    """A loaded PowerShell DLL must not be recopied after Python installation."""
    bundled = tmp_path / 'deployment-assets/tools/powershell'
    bundled.mkdir(parents=True)
    (bundled / 'pwsh.exe').write_bytes(b'TEST_ONLY_NOT_EXECUTED')
    dll_name = 'microsoft.management.infrastructure.native.unmanaged.dll'
    (bundled / dll_name).write_bytes(b'BUNDLED_DLL')
    runtime = tmp_path / 'runtime/tools/powershell'
    runtime.mkdir(parents=True)
    loaded_dll = runtime / dll_name
    loaded_dll.write_bytes(b'LOADED_DLL')
    shutil.copyfile(ROOT / 'local-runtime.ps1', tmp_path / 'local-runtime.ps1')
    install = (ROOT / 'install.ps1').read_text(encoding='utf-8-sig')
    after_python = install.split("print('Python dependencies loaded')\")", 1)[1]
    tool_setup = after_python.split('$bundledModules =', 1)[0]
    script = tmp_path / 'loaded-tool.ps1'
    script.write_text(r'''
$ErrorActionPreference = 'Stop'
$root = $env:PROBE_ROOT
$assetRoot = Join-Path $root 'deployment-assets'
$dll = Join-Path $root 'runtime\tools\powershell\microsoft.management.infrastructure.native.unmanaged.dll'
$loaded = [IO.File]::Open($dll, [IO.FileMode]::Open, [IO.FileAccess]::Read, [IO.FileShare]::Read)
try {
''' + tool_setup + r'''
    . (Join-Path $root 'local-runtime.ps1')
    $expected = Join-Path $assetRoot 'tools\powershell\pwsh.exe'
    if ((Get-Command pwsh.exe -CommandType Application | Select-Object -First 1).Source -ne $expected) {
        throw 'Bundled PowerShell is not available on the application PATH'
    }
    Write-Output 'LOADED_DLL_PRESERVED_AND_BUNDLED_TOOL_AVAILABLE'
} finally { $loaded.Dispose() }
''', encoding='utf-8-sig')
    result = subprocess.run(
        ['powershell.exe', '-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass', '-File', str(script)],
        env={**os.environ, 'PROBE_ROOT': str(tmp_path)}, capture_output=True, timeout=30)
    assert result.returncode == 0, result.stderr
    assert b'LOADED_DLL_PRESERVED_AND_BUNDLED_TOOL_AVAILABLE' in result.stdout
    assert loaded_dll.read_bytes() == b'LOADED_DLL'
