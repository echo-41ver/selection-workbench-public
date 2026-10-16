import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import selection

@pytest.mark.parametrize('exists', [True, False])
def test_doctor_reports_browser_file_readiness(monkeypatch, exists):
    monkeypatch.setattr(shutil, 'which', lambda name: 'node.exe')
    def probe(args, **kwargs):
        assert kwargs['timeout'] == 10
        assert args[1] == '-e'
        assert 'executablePath' in args[2]
        return SimpleNamespace(returncode=0, stdout=json.dumps({'ready': exists, 'status': 'READY' if exists else 'BROWSER_MISSING'}))
    monkeypatch.setattr(subprocess, 'run', probe)
    assert selection.doctor()['Playwright浏览器']['ready'] is exists


def test_doctor_browser_probe_timeout_is_not_ready(monkeypatch):
    monkeypatch.setattr(shutil, 'which', lambda name: 'node.exe')
    def probe(*args, **kwargs):
        raise subprocess.TimeoutExpired('node', 10)
    monkeypatch.setattr(subprocess, 'run', probe)
    assert selection.doctor()['Playwright浏览器']['ready'] is False


@pytest.mark.skipif(sys.platform != 'win32', reason='Windows installer')
@pytest.mark.parametrize('complete', [False, True])
def test_node_alone_is_not_complete_toolchain(tmp_path, complete):
    node = shutil.which('node')
    assert node
    harness = r'''
$tokens=$null; $errors=$null
$ast=[System.Management.Automation.Language.Parser]::ParseFile($env:INSTALL_FILE,[ref]$tokens,[ref]$errors)
if ($errors) { throw ($errors | Out-String) }
foreach ($statement in $ast.EndBlock.Statements) {
 if ($statement -is [System.Management.Automation.Language.FunctionDefinitionAst]) { . ([scriptblock]::Create($statement.Extent.Text)) }
}
function Get-Command { param($Name,$CommandType,[switch]$All,$ErrorAction) return [pscustomobject]@{Source=$env:TEST_NODE} }
function Test-Path {
 param($LiteralPath,$PathType)
 if ((Split-Path -Parent $LiteralPath) -ne (Split-Path -Parent $env:TEST_NODE)) { return $false }
 if ($LiteralPath.EndsWith('node.exe')) { return $true }
 return $env:TEST_COMPLETE -eq '1'
}
$result=Find-NodeToolchain
[pscustomobject]@{ready=($null -ne $result); npm=$result.Npm} | ConvertTo-Json -Compress
'''
    script=tmp_path/'probe.ps1'; script.write_text(harness,encoding='utf-8-sig')
    env={**os.environ, 'INSTALL_FILE':str(ROOT/'install.ps1'),'TEST_NODE':node,'TEST_COMPLETE':'1' if complete else '0'}
    result=subprocess.run(['powershell.exe','-NoProfile','-NonInteractive','-ExecutionPolicy','Bypass','-File',str(script)],env=env,capture_output=True,timeout=30)
    assert result.returncode==0, result.stderr
    assert json.loads(result.stdout)['ready'] is complete


@pytest.mark.skipif(sys.platform != 'win32', reason='Windows installer')
@pytest.mark.parametrize('scenario, installs', [('existing', 0), ('missing', 1), ('direct', 0)])
def test_network_bootstrap_preserves_existing_environment(tmp_path, scenario, installs):
    harness = r'''
$ErrorActionPreference='Stop'
$tokens=$null; $errors=$null
$ast=[System.Management.Automation.Language.Parser]::ParseFile($env:INSTALL_FILE,[ref]$tokens,[ref]$errors)
if ($errors) { throw ($errors | Out-String) }
foreach ($statement in $ast.EndBlock.Statements) {
 if ($statement -is [System.Management.Automation.Language.FunctionDefinitionAst]) { . ([scriptblock]::Create($statement.Extent.Text)) }
}
$script:installs=@()
function Get-ExistingNetworkEnvironment {
 if ($env:NETWORK_SCENARIO -eq 'existing' -or $script:installs.Count) { return 'EXISTING_CLIENT' }
}
function Install-Package { param($Id) $script:installs += $Id }
$CompanyConfig=$null
if ($env:NETWORK_SCENARIO -eq 'direct') { $CompanyConfig=$env:DIRECT_CONFIG }
Ensure-NetworkClient
[pscustomobject]@{installs=@($script:installs)} | ConvertTo-Json -Compress
'''
    config = tmp_path / 'company-test.json'
    config.write_text('{"preproxy_mode":"DIRECT"}', encoding='utf-8')
    script = tmp_path / 'probe-network.ps1'
    script.write_text(harness, encoding='utf-8-sig')
    result = subprocess.run(['powershell.exe', '-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass', '-File', str(script)],
                            env={**os.environ, 'INSTALL_FILE': str(ROOT/'install.ps1'), 'NETWORK_SCENARIO': scenario,
                                 'DIRECT_CONFIG': str(config)}, capture_output=True, timeout=30)
    assert result.returncode == 0, result.stderr
    data = json.loads(result.stdout.splitlines()[-1])
    assert len(data['installs']) == installs
    if installs:
        assert data['installs'] == ['ClashVergeRev.ClashVergeRev']


@pytest.mark.skipif(sys.platform != 'win32', reason='Windows installer')
def test_fresh_package_preflight_does_not_require_machine_local_runtime(tmp_path):
    # Invoke the actual entry in a short isolated path with no private runtime file.
    import tempfile
    with tempfile.TemporaryDirectory(prefix='deploy-preflight-', dir=ROOT.parent) as directory:
        target = Path(directory)
        for relative in ('install.ps1', 'scripts/deployment_guard.ps1', 'requirements.lock.txt',
                         'package-lock.json', 'scripts/company_setup.py', 'web/index.html'):
            destination = target / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(ROOT / relative, destination)
        result = subprocess.run(['powershell.exe', '-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass',
                                 '-File', str(target/'install.ps1'), '-PreflightOnly'], capture_output=True, timeout=30)
        assert result.returncode == 0, result.stderr
        data = json.loads(result.stdout.splitlines()[-1])
        assert data['source_complete'] is True
        assert data['configuration_read'] is False
        assert not (target/'runtime').exists()
