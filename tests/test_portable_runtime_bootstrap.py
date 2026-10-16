"""A slow first portable start must not download a duplicate system installer."""
import os
from pathlib import Path
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
