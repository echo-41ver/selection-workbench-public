"""Exercise native exit evidence in Windows PS5 without running an installer."""
import json
from datetime import datetime
import os
from pathlib import Path
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
POWERSHELL = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32/WindowsPowerShell/v1.0/powershell.exe"


@unittest.skipUnless(os.name == "nt" and POWERSHELL.is_file(), "Windows PowerShell 5.1 required")
class InstallerFailureEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="installer-evidence-", dir=ROOT.parent)
        temporary_path = Path(self.temporary.name).resolve()
        self.assertTrue(temporary_path.is_relative_to(ROOT.parent.resolve()))
        self.addCleanup(self.temporary.cleanup)
        self.root = temporary_path / "中文 空格"
        self.root.mkdir()

    def run_harness(self, body, *, expected_code=0, result_file=None, **environment):
        harness = self.root / "evidence.ps1"
        harness.write_text(r'''
$ErrorActionPreference = 'Stop'
$env:PSModulePath = (Join-Path $PSHOME 'Modules') + ';' + $env:PSModulePath
[Console]::OutputEncoding = [Text.UTF8Encoding]::new($false)
$OutputEncoding = [Console]::OutputEncoding
$tokens = $null; $errors = $null
$ast = [System.Management.Automation.Language.Parser]::ParseFile($env:INSTALL_SOURCE, [ref]$tokens, [ref]$errors)
if ($errors) { throw ($errors | Out-String) }
foreach ($statement in $ast.EndBlock.Statements) {
    if ($statement -is [System.Management.Automation.Language.FunctionDefinitionAst]) {
        . ([scriptblock]::Create($statement.Extent.Text))
    }
}
$root = $env:EVIDENCE_ROOT
$script:DeploymentCommandFailure = $null
''' + body, encoding="utf-8-sig")
        result = subprocess.run(
            [str(POWERSHELL), "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(harness)],
            cwd=ROOT, env={**os.environ, "INSTALL_SOURCE": str(ROOT / "install.ps1"),
                           "EVIDENCE_ROOT": str(self.root), **environment},
            capture_output=True, encoding="utf-8", errors="replace", timeout=30,
        )
        self.assertEqual(result.returncode, expected_code, result.stderr)
        if result_file is not None:
            return json.loads(result_file.read_text(encoding="utf-8-sig")), result
        return json.loads(result.stdout.splitlines()[-1]), result

    def test_real_native_exit_three_survives_in_failure_record_without_arguments(self):
        fixture = self.root / "native fixture.ps1"
        fixture.write_text(r'''
param([string]$LogPath, [string]$PrivateArgument)
[IO.File]::WriteAllText($LogPath, 'TEST_ONLY_NATIVE_LOG')
exit 3
''', encoding="utf-8-sig")
        data, result = self.run_harness(r'''
$log = New-DeploymentInstallerLog 'python-3.12.10'
$caught = $false
try {
    Invoke-DeploymentInstaller -Exe $env:FIXTURE_POWERSHELL -Arguments @('-NoProfile', '-NonInteractive', '-File', ('"{0}"' -f $env:NATIVE_FIXTURE), '-LogPath', ('"{0}"' -f $log), '-PrivateArgument', 'TEST_ONLY_PRIVATE_ARGUMENT') -SuccessExitCodes @(0,3010) -InstallerLog $log
} catch { $caught = $true }
$records = @(Get-ChildItem -LiteralPath (Join-Path $root 'runtime\installer-logs') -Filter 'process-*.json' | ForEach-Object { Get-Content -LiteralPath $_.FullName -Raw -Encoding UTF8 | ConvertFrom-Json })
@{ caught = $caught; failure = $script:DeploymentCommandFailure; records = $records } | ConvertTo-Json -Depth 6 -Compress
''', FIXTURE_POWERSHELL=str(POWERSHELL), NATIVE_FIXTURE=str(fixture))
        self.assertTrue(data["caught"])
        failure = data["failure"]
        self.assertEqual(failure["exit_code"], 3)
        self.assertTrue(failure["exit_code_available"])
        self.assertGreater(failure["process_id"], 0)
        started = datetime.fromisoformat(failure["process_started_at"].replace("Z", "+00:00"))
        completed = datetime.fromisoformat(failure["completed_at"].replace("Z", "+00:00"))
        self.assertLessEqual(started, completed)
        self.assertFalse(failure["succeeded"])
        self.assertTrue(failure["installer_log_exists"])
        self.assertEqual(Path(failure["installer_log_path"]).read_text(), "TEST_ONLY_NATIVE_LOG")
        self.assertEqual(data["records"][0]["exit_code"], 3)
        self.assertEqual(data["records"][0]["installer_log_path"], failure["installer_log_path"])
        self.assertNotIn("TEST_ONLY_PRIVATE_ARGUMENT", result.stdout + result.stderr)
        record_text = Path(failure["process_record_path"]).read_text(encoding="utf-8-sig")
        self.assertNotIn("TEST_ONLY_PRIVATE_ARGUMENT", record_text)
        self.assertNotIn("arguments", {key.lower() for key in failure})

    def test_accepted_native_codes_remain_accepted_and_missing_log_is_not_invented(self):
        for code in (0, 3010, 1638):
            with self.subTest(code=code):
                data, _ = self.run_harness(r'''
$log = New-DeploymentInstallerLog 'not-created'
Invoke-DeploymentInstaller -Exe $env:ComSpec -Arguments @('/d','/c','exit',$env:NATIVE_CODE) -SuccessExitCodes @(0,3010,1638) -InstallerLog $log
$record = Get-ChildItem -LiteralPath (Join-Path $root 'runtime\installer-logs') -Filter 'process-*.json' | Sort-Object LastWriteTimeUtc -Descending | Select-Object -First 1 | ForEach-Object { Get-Content -LiteralPath $_.FullName -Raw -Encoding UTF8 | ConvertFrom-Json }
@{ failure = $script:DeploymentCommandFailure; record = $record } | ConvertTo-Json -Depth 5 -Compress
''', NATIVE_CODE=str(code))
                self.assertIsNone(data["failure"])
                self.assertEqual(data["record"]["exit_code"], code)
                self.assertTrue(data["record"]["succeeded"])
                self.assertEqual(data["record"]["reboot_required"], code == 3010)
                self.assertFalse(data["record"]["installer_log_exists"])
                self.assertFalse(Path(data["record"]["installer_log_path"]).exists())

    def test_missing_exit_code_fails_closed(self):
        data, _ = self.run_harness(r'''
function Start-Process {
    $fake = [pscustomobject]@{ Handle = [IntPtr]1; ExitCode = $null }
    $fake | Add-Member ScriptMethod WaitForExit { }
    $fake | Add-Member ScriptMethod Refresh { }
    $fake | Add-Member ScriptMethod Dispose { }
    return $fake
}
$caught = $false
try { Invoke-DeploymentInstaller -Exe 'TEST_ONLY_NOT_EXECUTED.exe' -Arguments @('/quiet') } catch { $caught = $true }
@{ caught = $caught; failure = $script:DeploymentCommandFailure } | ConvertTo-Json -Depth 5 -Compress
''')
        self.assertTrue(data["caught"])
        self.assertIsNone(data["failure"]["exit_code"])
        self.assertFalse(data["failure"]["exit_code_available"])
        self.assertFalse(data["failure"]["succeeded"])

    def test_original_installer_catch_persists_native_exit_three(self):
        data, _ = self.run_harness(r'''
. (Join-Path (Split-Path -Parent $env:INSTALL_SOURCE) 'scripts\deployment_guard.ps1')
$transaction = $null
$stage = 'TEST_ONLY_NATIVE_FIXTURE'
$installationTry = $ast.EndBlock.Statements | Where-Object { $_ -is [System.Management.Automation.Language.TryStatementAst] } | Select-Object -Last 1
$catchText = $installationTry.CatchClauses[0].Body.Extent.Text
$catchBody = [scriptblock]::Create($catchText.Substring(1, $catchText.Length - 2))
try { Invoke-DeploymentInstaller -Exe $env:ComSpec -Arguments @('/d','/c','exit','3') -SuccessExitCodes @(0,3010) }
catch { . $catchBody }
''', expected_code=1, result_file=self.root / "runtime/install-failure.json")
        self.assertEqual(data["command"]["exit_code"], 3)
        self.assertTrue(data["command"]["exit_code_available"])
        self.assertFalse(data["command"]["succeeded"])
        status = json.loads((self.root / "runtime/install-status.json").read_text(encoding="utf-8-sig"))
        self.assertFalse(status["succeeded"])
        self.assertEqual(status["stage"], "TEST_ONLY_NATIVE_FIXTURE")

    def test_python_log_argument_and_failure_are_wired_without_starting_installer(self):
        installer = self.root / "deployment-assets/installers/python-3.12.10-amd64.exe"
        installer.parent.mkdir(parents=True)
        installer.write_bytes(b"TEST_ONLY_NEVER_EXECUTED")
        data, _ = self.run_harness(r'''
function Get-AuthenticodeSignature { return [pscustomobject]@{ Status = 'Valid' } }
function Start-Process {
    param($FilePath, $ArgumentList, $WindowStyle, $PassThru, $ErrorAction)
    $script:started = $FilePath
    $script:logIndex = [array]::IndexOf($ArgumentList, '/log')
    if ($script:logIndex -lt 0) { throw 'Python /log not supplied' }
    $script:logArgument = $ArgumentList[$script:logIndex + 1].Trim('"')
    $fake = [pscustomobject]@{ Handle = [IntPtr]1; ExitCode = 3 }
    $fake | Add-Member ScriptMethod WaitForExit { }
    $fake | Add-Member ScriptMethod Refresh { }
    $fake | Add-Member ScriptMethod Dispose { }
    return $fake
}
$caught = $false
try { Install-FixedRuntime 'python' } catch { $caught = $true }
@{ caught = $caught; failure = $script:DeploymentCommandFailure; log_argument = $script:logArgument; started = $script:started } | ConvertTo-Json -Depth 5 -Compress
''')
        self.assertTrue(data["caught"])
        self.assertEqual(data["failure"]["exit_code"], 3)
        self.assertEqual(Path(data["started"]), installer)
        self.assertEqual(data["log_argument"], data["failure"]["installer_log_path"])
        self.assertFalse(data["failure"]["installer_log_exists"])
        self.assertTrue(Path(data["log_argument"]).is_relative_to(self.root / "runtime/installer-logs"))

    def test_untrusted_log_destination_is_rejected_before_launch(self):
        data, _ = self.run_harness(r'''
$script:launched = $false
function Start-Process { $script:launched = $true; throw 'Must not launch' }
$caught = $false
try { Invoke-DeploymentInstaller -Exe 'TEST_ONLY_NOT_EXECUTED.exe' -Arguments @('/quiet') -InstallerLog (Join-Path $root '..\outside.log') } catch { $caught = $true }
@{ caught = $caught; launched = $script:launched } | ConvertTo-Json -Compress
''')
        self.assertTrue(data["caught"])
        self.assertFalse(data["launched"])


if __name__ == "__main__":
    unittest.main()
