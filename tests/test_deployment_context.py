"""Windows entry regressions use echo/exit fixtures, never the real installer."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PS = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32/WindowsPowerShell/v1.0/powershell.exe"


def fixture(tmp_path: Path, *, install_exit: int = 0, web_exit: int = 0, delay: int = 0) -> Path:
    root = tmp_path / "工具夹具 中文 空格"
    (root / "scripts").mkdir(parents=True)
    shutil.copy2(ROOT / "scripts/deployment_entry.ps1", root / "scripts/deployment_entry.ps1")
    shutil.copy2(ROOT / "deploy.cmd", root / "deploy.cmd")
    (root / "FIXTURE_ONLY.txt").write_text("Observer/coordinator fixture only; no dependencies or business validation.", encoding="utf-8")
    (root / "install.ps1").write_text(
        "param([string]$CompanyConfig,[switch]$AcceptCompanyConfig,[switch]$SkipAdsPower,[switch]$SkipModelDownload,[switch]$SkipProxyClient,[switch]$PreflightOnly)\n"
        "[Console]::OutputEncoding=New-Object Text.UTF8Encoding($false)\n"
        "[IO.File]::WriteAllText((Join-Path $PSScriptRoot 'received.json'),($PSBoundParameters|ConvertTo-Json),(New-Object Text.UTF8Encoding($true)))\n"
        "[Console]::Out.WriteLine('fixture stdout 中文')\n"
        "[Console]::Error.WriteLine('fixture stderr 中文 password=fixture-only-secret')\n"
        "[Console]::Out.Write(('尾部中文无换行'))\n"
        f"Start-Sleep -Seconds {delay}\nexit {install_exit}\n",
        encoding="utf-8-sig",
    )
    (root / "start-web.ps1").write_text(
        "[Console]::OutputEncoding=New-Object Text.UTF8Encoding($false)\n"
        "[IO.File]::WriteAllText((Join-Path $PSScriptRoot 'web-started.txt'),'fixture only')\n"
        "[Console]::Out.WriteLine('fixture web 中文')\n"
        f"exit {web_exit}\n",
        encoding="utf-8-sig",
    )
    return root


def invoke(root: Path, *args: str, wait_seconds: int = 30, env_overrides: dict | None = None) -> subprocess.CompletedProcess:
    env = os.environ.copy()
    env["SELECTION_DEPLOYMENT_WAIT_SECONDS"] = str(wait_seconds)
    env.update(env_overrides or {})
    return subprocess.run(
        [str(PS), "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(root / "scripts/deployment_entry.ps1"), *args],
        cwd=root, env=env, capture_output=True, encoding="utf-8", errors="replace", timeout=70,
    )


def attempt(root: Path) -> Path:
    folders = [p for p in (root / "runtime/deployment-entry").iterdir() if p.is_dir()]
    assert len(folders) == 1
    return folders[0]


def read(path: Path):
    return json.loads(path.read_text("utf-8-sig"))


def wait_finished(folder: Path, timeout: float = 15):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if (folder / "result.json").exists():
            return read(folder / "result.json")
        time.sleep(0.2)
    raise AssertionError(f"fixture worker did not finish: {folder}")


def cleanup_finished_task(folder: Path):
    """Only remove the exact fixture's task after verifying its nonce/action/user."""
    request = read(folder / "request.json")
    if request["mode"] != "scheduled":
        return
    # An entry retry performs its own identity-checked cleanup of a completed worker.
    # Usually the original observer already removed it; this check never stops a task.
    code = r"""
$ErrorActionPreference='Stop'
$r=Get-Content -LiteralPath $args[0] -Raw -Encoding UTF8|ConvertFrom-Json
$s=New-Object -ComObject 'Schedule.Service';$s.Connect();$f=$s.GetFolder('\')
try{$t=$f.GetTask($r.task_name)}catch{exit 0}
$d=$t.Definition;$u=$d.Principal.UserId
for($n=0;$n -lt 25 -and [int]$t.State -in @(2,4);$n++){Start-Sleep -Milliseconds 200;$t=$f.GetTask($r.task_name)}
if($u -notmatch '^S-1-'){$u=(New-Object Security.Principal.NTAccount($u)).Translate([Security.Principal.SecurityIdentifier]).Value}
if($t.Name -ne $r.task_name -or $d.RegistrationInfo.Description -ne ('Selection deployment entry '+$r.nonce) -or $u -ne $r.sid -or $d.Principal.RunLevel -ne 0 -or $d.Principal.LogonType -ne 3 -or $d.Triggers.Count -ne 0 -or $d.Actions.Count -ne 1 -or $d.Actions.Item(1).Arguments -notlike ('*'+$r.attempt_dir+'*') -or [int]$t.State -in @(2,4)){throw 'Fixture task identity/state mismatch'}
$f.DeleteTask($r.task_name,0)
"""
    helper = folder / "cleanup-fixture-task.ps1"
    helper.write_text(code, encoding="utf-8-sig")
    p = subprocess.run([str(PS), "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(helper), str(folder / "request.json")], capture_output=True, timeout=15)
    assert p.returncode == 0, p.stderr.decode("utf-8", "replace")


def check_parameters_preflight_streams_and_real_context(tmp_path):
    root = fixture(tmp_path)
    config = "配置 & 同事's 路径.json"
    p = invoke(root, "-CompanyConfig", config, "-AcceptCompanyConfig", "-SkipAdsPower", "-SkipModelDownload", "-SkipProxyClient", "-PreflightOnly")
    assert p.returncode == 0, p.stdout + p.stderr
    folder = attempt(root)
    try:
        received = read(root / "received.json")
        assert received["CompanyConfig"] == str(root / config)
        for flag in ("AcceptCompanyConfig", "SkipAdsPower", "SkipModelDownload", "SkipProxyClient", "PreflightOnly"):
            assert received[flag] is True or received[flag] == {"IsPresent": True}
        assert not (root / "web-started.txt").exists()
        assert "fixture stdout 中文" in p.stdout and "尾部中文无换行" in p.stdout
        assert "fixture stderr 中文" in p.stderr and "fixture-only-secret" not in p.stderr
        result = read(folder / "result.json")
        req = read(folder / "request.json")
        hello = read(folder / "worker-started.json")
        assert result["native_exit_code"] == 0 and result["preflight_only"]
        assert hello["identity"]["sid"] == req["sid"] and hello["identity"]["session_id"] == req["session_id"]
        assert hello["view"]["mapped_to_msix_localcache"] is False
        if req["parent_view"]["mapped_to_msix_localcache"]:
            assert req["mode"] == "scheduled"  # Real MSIX→normal Scheduler contrast, not a mocked flag.
            assert read(folder / "dispatch.json")["logon_type"] == 3
    finally:
        cleanup_finished_task(folder)


def check_native_failures_are_preserved_and_install_gates_web(tmp_path, install_exit, web_exit, expected):
    root = fixture(tmp_path, install_exit=install_exit, web_exit=web_exit)
    p = invoke(root)
    assert p.returncode == expected, p.stdout + p.stderr
    folder = attempt(root)
    try:
        result = read(folder / "result.json")
        assert result["install"]["native_exit_code"] == install_exit
        assert (root / "web-started.txt").exists() is (install_exit == 0)
        assert result["native_exit_code"] == expected
    finally:
        cleanup_finished_task(folder)


def check_parent_timeout_keeps_worker_and_rejects_duplicate(tmp_path):
    root = fixture(tmp_path, delay=10)
    p = invoke(root, wait_seconds=4)
    assert p.returncode == 124, p.stdout + p.stderr
    folder = attempt(root)
    try:
        timeout = read(folder / "parent-timeout.json")
        assert timeout["processes_killed"] is False
        assert not (folder / "result.json").exists()
        assert "fixture stdout 中文" in (folder / "stdout.log").read_text("utf-8")
        again = invoke(root)
        assert again.returncode != 0 and "No duplicate" in again.stderr
        assert attempt(root) == folder
        result = wait_finished(folder)
        assert result["native_exit_code"] == 0
        assert (root / "web-started.txt").exists()
    finally:
        wait_finished(folder)
        cleanup_finished_task(folder)


def check_deploy_wrapper_preserves_bom_and_crlf_contract():
    data = (ROOT / "deploy.cmd").read_bytes()
    assert not data.startswith(b"\xef\xbb\xbf")
    assert b"\r\n" in data and b"deployment_entry.ps1" in data
    assert b"exit /b %selection_result%" in data


def check_before_dispatch_failure_releases_active(tmp_path):
    root = fixture(tmp_path)
    p = invoke(root, "-PreflightOnly", env_overrides={"HTTP_PROXY": "http://127.0.0.1:9"})
    folder = attempt(root)
    request = read(folder / "request.json")
    if not request["parent_view"]["mapped_to_msix_localcache"]:
        assert p.returncode == 0  # Ordinary contexts do not need to transfer process state.
        return
    assert p.returncode != 0 and "proxy" in p.stderr.lower()
    assert read(folder / "parent-error.json")["active_released_before_dispatch"] is True
    assert not (root / "runtime/deployment-entry/active.json").exists()
    assert not (root / "received.json").exists()
    retry = invoke(root, "-PreflightOnly")
    assert retry.returncode == 0, retry.stdout + retry.stderr
    folders = [p for p in (root / "runtime/deployment-entry").iterdir() if p.is_dir()]
    assert len(folders) == 2
    for completed in folders:
        if (completed / "result.json").exists():
            cleanup_finished_task(completed)


def check_normal_desktop_deploy_cmd_preflight(tmp_path):
    root = fixture(tmp_path)
    helper = root / "outer-current-user-fixture.ps1"
    helper.write_text(r'''
param([string]$FixtureRoot)
$ErrorActionPreference='Stop'
$name='SelectionDeploymentOuterFixture-'+[Guid]::NewGuid().ToString('N')
$description='read-only deploy.cmd outer fixture '+$name
$sid=[Security.Principal.WindowsIdentity]::GetCurrent().User.Value
$session=[Diagnostics.Process]::GetCurrentProcess().SessionId
$exe=Join-Path $env:SystemRoot 'System32\cmd.exe'
$arguments='/d /s /c ""'+(Join-Path $FixtureRoot 'deploy.cmd')+'" -PreflightOnly -SkipAdsPower -CompanyConfig "配置 & 同事''s 文件.json""'
$record=[ordered]@{task_name=$name;fixture_only=$true;installer_executed=$false;session_id=$session;sid=$sid;cleaned=$false}
$created=$false
try {
 $s=New-Object -ComObject 'Schedule.Service';$s.Connect();$f=$s.GetFolder('\');$d=$s.NewTask(0)
 $d.RegistrationInfo.Description=$description;$d.Principal.UserId=$sid;$d.Principal.LogonType=3;$d.Principal.RunLevel=0
 $d.Settings.AllowDemandStart=$true;$d.Settings.Enabled=$true;$d.Settings.ExecutionTimeLimit='PT1M';$d.Settings.DisallowStartIfOnBatteries=$false;$d.Settings.StopIfGoingOnBatteries=$false
 $a=$d.Actions.Create(0);$a.Path=$exe;$a.Arguments=$arguments;$a.WorkingDirectory=$FixtureRoot
 $t=$f.RegisterTaskDefinition($name,$d,2,$sid,$null,3,$null);$created=$true;$null=$t.RunEx($null,4,$session,$null)
 $deadline=[DateTime]::UtcNow.AddSeconds(45)
 do{Start-Sleep -Milliseconds 200;$t=$f.GetTask($name)}while([DateTime]::UtcNow -lt $deadline -and ([int]$t.State -in @(2,4) -or $t.LastRunTime.Year -lt 2026))
 $record.last_task_result=$t.LastTaskResult;$record.final_state=[int]$t.State
 if([int]$t.State -in @(2,4)){throw 'Outer fixture is still running; no task was stopped.'}
} finally {
 if($created){$t=$f.GetTask($name);$d=$t.Definition;$u=$d.Principal.UserId;if($u -notmatch '^S-1-'){$u=(New-Object Security.Principal.NTAccount($u)).Translate([Security.Principal.SecurityIdentifier]).Value}
  if($t.Name -ne $name -or $d.RegistrationInfo.Description -ne $description -or $u -ne $sid -or $d.Principal.LogonType -ne 3 -or $d.Principal.RunLevel -ne 0 -or $d.Triggers.Count -ne 0 -or $d.Actions.Count -ne 1 -or $d.Actions.Item(1).Path -ne $exe -or $d.Actions.Item(1).Arguments -cne $arguments -or [int]$t.State -in @(2,4)){throw 'Outer fixture cleanup identity/state mismatch'}
  $f.DeleteTask($name,0);$record.cleaned=$true
 }
 [IO.File]::WriteAllText((Join-Path $FixtureRoot 'outer-fixture-result.json'),($record|ConvertTo-Json),(New-Object Text.UTF8Encoding($true)))
}
''', encoding="utf-8-sig")
    p = subprocess.run([str(PS), "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(helper), "-FixtureRoot", str(root)], capture_output=True, timeout=60)
    assert p.returncode == 0, p.stderr.decode("utf-8", "replace")
    outer = read(root / "outer-fixture-result.json")
    assert outer["cleaned"] is True and outer["last_task_result"] == 0
    folder = attempt(root)
    request = read(folder / "request.json")
    assert request["mode"] == "direct" and request["parent_view"]["mapped_to_msix_localcache"] is False
    assert read(folder / "result.json")["native_exit_code"] == 0
    received = read(root / "received.json")
    assert received["CompanyConfig"] == str(root / "配置 & 同事's 文件.json")
    assert received["PreflightOnly"] is True or received["PreflightOnly"] == {"IsPresent": True}
    assert not (root / "web-started.txt").exists()


@unittest.skipUnless(os.name == "nt", "real Windows PowerShell / current-user Scheduler fixture")
class DeploymentContextTests(unittest.TestCase):
    def setUp(self):
        base = os.environ.get("SELECTION_DEPLOYMENT_TEST_ROOT")
        if base:
            Path(base).mkdir(parents=True, exist_ok=True)
        # Keep marked fixtures and their real worker evidence, even after a failing assertion.
        # Removing a live fixture would obscure a pending worker; no process is killed here.
        self.directory = Path(tempfile.mkdtemp(prefix="selection-entry-fixture-", dir=base))

    def test_parameters_preflight_streams_and_real_context(self):
        check_parameters_preflight_streams_and_real_context(self.directory)

    def test_native_failures(self):
        for values in ((7, 0, 7), (0, 9, 9)):
            with self.subTest(values=values):
                check_native_failures_are_preserved_and_install_gates_web(self.directory / str(values[0]), *values)

    def test_timeout(self):
        check_parent_timeout_keeps_worker_and_rejects_duplicate(self.directory)

    def test_wrapper_encoding(self):
        check_deploy_wrapper_preserves_bom_and_crlf_contract()

    def test_before_dispatch_failure_releases_active(self):
        check_before_dispatch_failure_releases_active(self.directory)

    def test_normal_desktop_deploy_cmd_preflight(self):
        check_normal_desktop_deploy_cmd_preflight(self.directory)


if __name__ == "__main__":
    unittest.main()
