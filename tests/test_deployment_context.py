"""Windows entry regressions use echo/exit fixtures, never the real installer."""
from __future__ import annotations

import json
import ctypes
import os
import shutil
import subprocess
import sys
import time
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PS = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32/WindowsPowerShell/v1.0/powershell.exe"


def fixture(tmp_path: Path, *, install_exit: int = 0, web_exit: int = 0, delay: int = 0, wait_for_release: bool = False) -> Path:
    root = tmp_path / "工具夹具 中文 空格"
    (root / "scripts").mkdir(parents=True)
    # Windows may supply an 8.3 TEMP alias, while PowerShell reports the same
    # existing directory by its long name. Keep the exact argument assertions.
    root = root.resolve()
    shutil.copy2(ROOT / "scripts/deployment_entry.ps1", root / "scripts/deployment_entry.ps1")
    for name in ("deployment_support.ps1", "export_deployment_diagnostics.ps1"):
        shutil.copy2(ROOT / "scripts" / name, root / "scripts" / name)
    shutil.copy2(ROOT / "deploy.cmd", root / "deploy.cmd")
    (root / "FIXTURE_ONLY.txt").write_text("Observer/coordinator fixture only; no dependencies or business validation.", encoding="utf-8")
    (root / "install.ps1").write_text(
        "param([string]$CompanyConfig,[switch]$AcceptCompanyConfig,[switch]$SkipAdsPower,[switch]$SkipModelDownload,[switch]$SkipProxyClient,[switch]$PreflightOnly)\n"
        "[Console]::OutputEncoding=New-Object Text.UTF8Encoding($false)\n"
        "[IO.File]::WriteAllText((Join-Path $PSScriptRoot 'received.json'),($PSBoundParameters|ConvertTo-Json),(New-Object Text.UTF8Encoding($true)))\n"
        "[Console]::Out.WriteLine('fixture stdout 中文')\n"
        "[Console]::Error.WriteLine('fixture stderr 中文 password=fixture-only-secret')\n"
        "[Console]::Out.Write(('尾部中文无换行'))\n"
        + ("$entered=[ordered]@{fixture_only=$true;pid=$PID;start_time_utc=[Diagnostics.Process]::GetCurrentProcess().StartTime.ToUniversalTime().ToString('o')}\n"
         "[IO.File]::WriteAllText((Join-Path $PSScriptRoot 'fixture-worker-entered.json'),($entered|ConvertTo-Json),(New-Object Text.UTF8Encoding($true)))\n"
         "$release=Join-Path $PSScriptRoot 'fixture-worker-release.txt'\n"
         "$deadline=[DateTime]::UtcNow.AddSeconds(45)\n"
         "while(-not [IO.File]::Exists($release)){\n"
         " if([DateTime]::UtcNow -ge $deadline){[Console]::Error.WriteLine('Fixture release barrier timed out');exit 86}\n"
         " Start-Sleep -Milliseconds 100\n}\n" if wait_for_release else f"Start-Sleep -Seconds {delay}\n")
        + f"exit {install_exit}\n",
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
        creationflags=subprocess.CREATE_NO_WINDOW,
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


def wait_fixture_entered(root: Path, folder: Path, timeout: float = 10):
    started = time.monotonic()
    entered_path = root / "fixture-worker-entered.json"
    phase_path = folder / "phase.json"
    while True:
        entered = phase = None
        if entered_path.exists() and phase_path.exists():
            try:
                entered, phase = read(entered_path), read(phase_path)
            except (FileNotFoundError, json.JSONDecodeError):
                pass  # The small marker write may still be completing.
        ready = entered is not None and phase is not None
        finished = (folder / "result.json").exists()
        expired = time.monotonic() - started >= timeout
        if ready or finished or expired:
            observation = {"fixture_only": True, "timeout_seconds": timeout,
                           "elapsed_seconds": time.monotonic() - started,
                           "entered_exists": entered_path.exists(), "phase_exists": phase_path.exists(),
                           "ready": ready, "worker_result_exists": finished}
            (root / "fixture-entry-wait.json").write_text(json.dumps(observation, indent=2), encoding="utf-8")
            assert ready, f"fixture worker did not enter the release barrier: {observation}; evidence: {folder}"
            return entered, phase
        time.sleep(0.1)


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
    p = subprocess.run([str(PS), "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(helper), str(folder / "request.json")], capture_output=True, timeout=15, creationflags=subprocess.CREATE_NO_WINDOW)
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
    root = fixture(tmp_path, wait_for_release=True)
    release = root / "fixture-worker-release.txt"
    folder = None
    try:
        p = invoke(root, wait_seconds=4)
        assert p.returncode == 124, p.stdout + p.stderr
        folder = attempt(root)
        timeout = read(folder / "parent-timeout.json")
        assert timeout["processes_killed"] is False
        entered, phase = wait_fixture_entered(root, folder)
        assert entered["fixture_only"] is True
        assert entered["pid"] == phase["process"]["pid"]
        assert entered["start_time_utc"] == phase["process"]["start_time_utc"]
        assert not (folder / "result.json").exists()
        assert "fixture stdout 中文" in (folder / "stdout.log").read_text("utf-8")
        again = invoke(root)
        assert again.returncode != 0 and "No duplicate" in again.stderr
        assert attempt(root) == folder
        assert not (folder / "result.json").exists()
        release.write_text("release this marked fixture only", encoding="utf-8")
        result = wait_finished(folder)
        assert result["native_exit_code"] == 0
        assert (root / "web-started.txt").exists()
    finally:
        # Release even after an assertion fails; the fixture also has its own
        # bounded failure exit, and no worker or scheduled task is killed here.
        release.write_text("release this marked fixture only", encoding="utf-8")
        if folder is None and (root / "runtime/deployment-entry").is_dir():
            folder = attempt(root)
        if folder is not None:
            wait_finished(folder)
            cleanup_finished_task(folder)


def check_short_path_alias_preflight(tmp_path):
    root = fixture(tmp_path)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    get_short = kernel.GetShortPathNameW
    get_short.argtypes = (ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_uint)
    get_short.restype = ctypes.c_uint
    needed = get_short(str(root), None, 0)
    assert needed, f"GetShortPathNameW failed: {ctypes.get_last_error()}"
    buffer = ctypes.create_unicode_buffer(needed)
    length = get_short(str(root), buffer, needed)
    assert 0 < length < needed, f"GetShortPathNameW failed: {ctypes.get_last_error()}"
    alias = Path(buffer.value)
    distinct = str(alias).casefold() != str(root).casefold()
    (root / "short-path-availability.json").write_text(json.dumps({
        "fixture_only": True, "api": "GetShortPathNameW", "canonical_root": str(root),
        "short_alias": str(alias), "distinct_alias_available": distinct,
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    if not distinct:
        raise unittest.SkipTest("8.3 alias unavailable: GetShortPathNameW returned the existing long path unchanged")
    assert alias.resolve() == root
    config = "配置 & 同事's 短路径.json"
    p = invoke(alias, "-CompanyConfig", config, "-PreflightOnly")
    assert p.returncode == 0, p.stdout + p.stderr
    folder = attempt(root)
    try:
        received = read(root / "received.json")
        assert received["CompanyConfig"] == str(root / config)
        assert received["PreflightOnly"] is True or received["PreflightOnly"] == {"IsPresent": True}
        assert read(folder / "result.json")["native_exit_code"] == 0
        assert not (root / "web-started.txt").exists()
    finally:
        cleanup_finished_task(folder)


def check_direct_worker_does_not_inherit_observer_pipes(tmp_path):
    root = fixture(tmp_path)
    probe = root / "direct-handle-probe"
    probe.mkdir()
    worker = probe / "worker.ps1"
    worker.write_text(r'''
param([string]$Root)
$ErrorActionPreference='Stop'
[Console]::OutputEncoding=[Text.UTF8Encoding]::new($false)
[IO.File]::WriteAllText((Join-Path $Root 'entered.json'),(@{fixture_only=$true;pid=$PID}|ConvertTo-Json))
$deadline=[DateTime]::UtcNow.AddSeconds(15)
while(-not [IO.File]::Exists((Join-Path $Root 'release.txt'))){
 if([DateTime]::UtcNow -ge $deadline){[IO.File]::WriteAllText((Join-Path $Root 'expired.txt'),'fixture deadline');exit 86}
 Start-Sleep -Milliseconds 50
}
[Console]::Out.WriteLine('worker stdout 中文 after observer exit')
[Console]::Error.WriteLine('worker stderr 中文 after observer exit')
[IO.File]::WriteAllText((Join-Path $Root 'ended.txt'),'fixture released')
''', encoding="utf-8-sig")
    observer = probe / "observer.ps1"
    observer.write_text(r'''
param([string]$Root,[string]$Worker,[string]$Entry)
$ErrorActionPreference='Stop'
$definition=[regex]::Match([IO.File]::ReadAllText($Entry),'(?s)Add-Type -TypeDefinition @''\r?\n(.*?)\r?\n''@')
if(-not $definition.Success){throw 'Actual deployment capture type was not found'}
Add-Type -TypeDefinition $definition.Groups[1].Value
$exe=Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'
$arguments='-NoProfile -NonInteractive -ExecutionPolicy Bypass -File "'+$Worker+'" -Root "'+$Root+'"'
$process=[SelectionEntryCapture]::StartDetached($exe,$arguments,$Root,(Join-Path $Root 'worker.stdin.txt'),(Join-Path $Root 'worker.stdout.log'),(Join-Path $Root 'worker.stderr.log'))
$deadline=[DateTime]::UtcNow.AddSeconds(8)
while(-not [IO.File]::Exists((Join-Path $Root 'entered.json'))){if([DateTime]::UtcNow -ge $deadline){exit 71};Start-Sleep -Milliseconds 50}
[Console]::Out.WriteLine('observer native timeout fixture')
exit 124
''', encoding="utf-8-sig")
    process = subprocess.Popen([str(PS), "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(observer),
                                "-Root", str(probe), "-Worker", str(worker), "-Entry", str(root / "scripts/deployment_entry.ps1")],
                               cwd=root, stdout=subprocess.PIPE, stderr=subprocess.PIPE, creationflags=subprocess.CREATE_NO_WINDOW)
    observation = {"fixture_only": True, "worker_killed": False}
    try:
        code = process.wait(timeout=15)
        observation["observer_native_exit_code"] = code
        assert code == 124
        assert read(probe / "entered.json")["fixture_only"] is True
        assert not (probe / "ended.txt").exists()
        # A native wait alone would miss inherited PIPE handles. EOF must arrive
        # while the marked worker is still waiting for this test to release it.
        stdout, stderr = process.communicate(timeout=2)
        observation["pipe_eof_before_worker_release"] = True
        assert b"observer native timeout fixture" in stdout
        assert b"worker stdout" not in stdout and not stderr
        assert not (probe / "ended.txt").exists()
    finally:
        (probe / "release.txt").write_text("release only this marked fixture", encoding="utf-8")
        stdout, stderr = process.communicate(timeout=20)
        (probe / "observer.stdout").write_bytes(stdout)
        (probe / "observer.stderr").write_bytes(stderr)
        deadline = time.monotonic() + 10
        while not (probe / "ended.txt").exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        observation["worker_released"] = (probe / "ended.txt").exists()
        observation["worker_expired"] = (probe / "expired.txt").exists()
        (probe / "observation.json").write_text(json.dumps(observation, indent=2), encoding="utf-8")
    assert observation["worker_released"] and not observation["worker_expired"]
    assert "worker stdout 中文 after observer exit" in (probe / "worker.stdout.log").read_text("utf-8-sig")
    assert "worker stderr 中文 after observer exit" in (probe / "worker.stderr.log").read_text("utf-8-sig")


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
$launcher=Join-Path $FixtureRoot 'hidden-deploy-fixture.py'
@'
import os, pathlib, subprocess, sys
root = pathlib.Path(sys.argv[1])
command = '"' + os.environ['ComSpec'] + '" /d /s /c ""' + str(root / 'deploy.cmd') + '" -PreflightOnly -SkipAdsPower -CompanyConfig "配置 & 同事\'s 文件.json""'
with (root / 'outer.stdout.log').open('wb') as output, (root / 'outer.stderr.log').open('wb') as errors:
    result = subprocess.run(command, cwd=root, stdout=output, stderr=errors,
                            creationflags=subprocess.CREATE_NO_WINDOW)
raise SystemExit(result.returncode)
'@ | Set-Content -LiteralPath $launcher -Encoding UTF8
$exe=$env:SELECTION_FIXTURE_PYTHONW
$arguments='"'+$launcher+'" "'+$FixtureRoot+'"'
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
    env = {**os.environ, "SELECTION_FIXTURE_PYTHONW": str(Path(sys.executable).with_name("pythonw.exe"))}
    p = subprocess.run([str(PS), "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(helper), "-FixtureRoot", str(root)], capture_output=True, timeout=60, creationflags=subprocess.CREATE_NO_WINDOW, env=env)
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

    def test_short_path_alias_preflight(self):
        check_short_path_alias_preflight(self.directory)

    def test_direct_worker_does_not_inherit_observer_pipes(self):
        check_direct_worker_does_not_inherit_observer_pipes(self.directory)


if __name__ == "__main__":
    unittest.main()
