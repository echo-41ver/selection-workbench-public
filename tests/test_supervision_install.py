import json
import os
from pathlib import Path
import subprocess
import sys

import pytest


ROOT = Path(__file__).resolve().parents[1]
pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Windows PowerShell installer")


def run_installer_functions(tmp_path, scenario, elevated=False, twice=False):
    # Parse without dot-sourcing the installer: its top-level installation never runs.
    # Every network/install boundary in these functions is replaced before invocation.
    harness = r"""
$ErrorActionPreference = 'Stop'
$root = $env:INSTALL_TEST_ROOT
$tokens = $null; $errors = $null
$ast = [System.Management.Automation.Language.Parser]::ParseFile((Join-Path $env:INSTALL_TEST_ROOT 'install.ps1'), [ref]$tokens, [ref]$errors)
if ($errors) { throw ($errors | Out-String) }
foreach ($statement in $ast.EndBlock.Statements) {
    if ($statement -is [System.Management.Automation.Language.FunctionDefinitionAst]) {
        . ([scriptblock]::Create($statement.Extent.Text))
    }
}
$script:calls = @()
$script:ready = $env:INSTALL_TEST_SCENARIO -eq 'existing'
function Get-Command {
    param($Name, $CommandType, $ErrorAction)
    if ($script:ready -and $env:INSTALL_TEST_SCENARIO -ne 'alias') {
        return [pscustomobject]@{ Source = 'C:\mock\winget.exe' }
    }
}
function Test-Path {
    param($LiteralPath, $PathType)
    return $script:ready -and $env:INSTALL_TEST_SCENARIO -eq 'alias' -and $LiteralPath.EndsWith('\Microsoft\WindowsApps\winget.exe')
}
function Install-PackageProvider {
    param($Name, [switch]$Force, $Scope, $ErrorAction)
    if ($Name -ne 'NuGet' -or -not $Force -or $Scope -ne 'CurrentUser' -or $ErrorAction -ne 'Stop') { throw 'incorrect provider arguments' }
    $script:calls += 'provider'
}
function Install-Module {
    param($Name, [switch]$Force, $Repository, $Scope, $ErrorAction)
    if ($Name -ne 'Microsoft.WinGet.Client' -or -not $Force -or $Repository -ne 'PSGallery' -or $Scope -ne 'CurrentUser' -or $ErrorAction -ne 'Stop') { throw 'incorrect module arguments' }
    $script:calls += 'module'
    if ($env:INSTALL_TEST_SCENARIO -eq 'module_failure') { throw 'simulated download failure' }
}
function Import-Module {
    param($Name, $ErrorAction)
    if ($Name -ne 'Microsoft.WinGet.Client' -or $ErrorAction -ne 'Stop') { throw 'incorrect import arguments' }
    $script:calls += 'import'
}
function Test-Administrator { return $env:INSTALL_TEST_ELEVATED -eq '1' }
function Repair-WinGetPackageManager {
    param([switch]$AllUsers, $ErrorAction)
    if ($AllUsers.IsPresent -ne ($env:INSTALL_TEST_ELEVATED -eq '1') -or $ErrorAction -ne 'Stop') { throw 'incorrect repair arguments' }
    $script:repairAllUsers = $AllUsers.IsPresent
    $script:calls += 'repair'
    if ($env:INSTALL_TEST_SCENARIO -eq 'repair_failure') { throw 'simulated repair failure' }
    $script:ready = $env:INSTALL_TEST_SCENARIO -ne 'unavailable'
}
function Refresh-Path { $script:calls += 'refresh' }
function Checked {
    param($Exe, [string[]]$Arguments)
    if ($Arguments[0] -eq 'source') {
        if (($Arguments -join ' ') -ne 'source update --name winget --disable-interactivity') { throw 'incorrect source update arguments' }
        $script:calls += 'source_update'
        if ($env:INSTALL_TEST_SCENARIO -eq 'source_failure') { throw 'simulated source update failure' }
        return
    }
    $script:calls += 'install'
    $script:exe = $Exe
    $script:arguments = $Arguments
}
try {
    Install-Package 'Python.Python.3.12'
    if ($env:INSTALL_TEST_TWICE -eq '1') { Install-Package 'OpenJS.NodeJS.LTS' }
} catch {
    Write-Output ('FAILURE:' + $_.Exception.Message)
    Write-Output ('CALLS:' + ($script:calls -join ','))
    exit 7
}
[pscustomobject]@{ calls = $script:calls; exe = $script:exe; arguments = $script:arguments; repair_all_users = $script:repairAllUsers } | ConvertTo-Json -Compress
"""
    script = tmp_path / "installer-mock.ps1"
    script.write_text(harness, encoding="utf-8-sig")
    shell = Path(os.environ["SystemRoot"]) / "System32/WindowsPowerShell/v1.0/powershell.exe"
    env = {**os.environ, "INSTALL_TEST_ROOT": str(ROOT), "INSTALL_TEST_SCENARIO": scenario,
           "INSTALL_TEST_ELEVATED": "1" if elevated else "0", "INSTALL_TEST_TWICE": "1" if twice else "0"}
    return subprocess.run(
        [str(shell), "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(script)],
        env=env, capture_output=True, timeout=30,
    )


def result_data(result):
    assert result.returncode == 0, result.stdout + result.stderr
    return json.loads(result.stdout.splitlines()[-1])


def test_existing_winget_skips_bootstrap(tmp_path):
    data = result_data(run_installer_functions(tmp_path, "existing"))
    assert data["calls"] == ["source_update", "install", "refresh"]
    assert data["exe"] == r"C:\mock\winget.exe"
    assert data["arguments"][:3] == ["install", "--id", "Python.Python.3.12"]


@pytest.mark.parametrize("scenario", ["missing", "alias"])
@pytest.mark.parametrize("elevated", [False, True], ids=["current_user", "administrator"])
def test_missing_winget_bootstraps_and_rediscovers_executable(tmp_path, scenario, elevated):
    data = result_data(run_installer_functions(tmp_path, scenario, elevated=elevated))
    assert data["calls"] == ["provider", "module", "import", "repair", "refresh", "source_update", "install", "refresh"]
    assert data["exe"].endswith(r"\Microsoft\WindowsApps\winget.exe" if scenario == "alias" else r"\mock\winget.exe")
    assert data["arguments"][:3] == ["install", "--id", "Python.Python.3.12"]
    assert data["repair_all_users"] is elevated


@pytest.mark.parametrize("scenario, expected", [
    ("module_failure", b"CALLS:provider,module"),
    ("repair_failure", b"CALLS:provider,module,import,repair"),
    ("unavailable", b"CALLS:provider,module,import,repair,refresh"),
    ("source_failure", b"CALLS:provider,module,import,repair,refresh,source_update"),
])
def test_bootstrap_failure_stops_before_package_install(tmp_path, scenario, expected):
    result = run_installer_functions(tmp_path, scenario)
    assert result.returncode != 0
    assert expected in result.stdout
    assert b",install" not in result.stdout


def test_winget_source_is_prepared_once_for_multiple_missing_dependencies(tmp_path):
    data = result_data(run_installer_functions(tmp_path, "existing", twice=True))
    assert data['calls'] == ['source_update', 'install', 'refresh', 'install', 'refresh']
    assert data['arguments'][:3] == ['install', '--id', 'OpenJS.NodeJS.LTS']


def test_installer_keeps_bom_and_generated_skill_output():
    content = (ROOT / "install.ps1").read_bytes()
    assert content.startswith(b"\xef\xbb\xbf")
    assert "scripts/generate_agent_config.py" in content.decode("utf-8-sig")
    generator = (ROOT / "scripts/generate_agent_config.py").read_text("utf-8")
    assert "amazon-temu-automation" in generator and "agent-config" in generator
