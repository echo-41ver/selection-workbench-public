from pathlib import Path
import subprocess
import json
import pytest
ROOT=Path(__file__).resolve().parents[1]


@pytest.mark.parametrize('recover,matching,owner_change,expected',[
    (True,True,None,True),(False,True,None,False),(True,False,None,False),
    (True,True,'listener_pid',False),(True,True,'driver_start',False),
    (True,True,'driver_path',False),(True,True,'browser_start',False),
    (True,True,'browser_pid',False),
    (True,True,'listener_missing',True),(True,True,'mixed_listener',False),
])
def test_hung_driver_recovery_requires_explicit_fenced_mode_and_same_browser_owner(tmp_path,recover,matching,owner_change,expected):
    source=(ROOT/'scripts/bind_flower_worker.ps1').read_text('utf-8-sig')
    block=source[source.index('$listener = @(Get-LocalTcpListener'):source.index('\nNew-Item -ItemType Directory -Force -Path $runtime')]
    state=tmp_path/'driver.json';binding=tmp_path/'binding.json'
    state.write_text(json.dumps({'pid':123,'started_ticks':638000000000000000,'marionette_port':58549,
        'profile_id':'selected' if matching else 'foreign','browser_pid':7,'browser_started_ticks':638000000000000000}))
    binding.write_text('{"phase":"BOUND","geckodriver_pid":123}')
    harness=tmp_path/'recover.ps1'
    setup=r"""
$ErrorActionPreference='Stop'
function Write-BindingTiming {param($Stage)}
$driverState='DRIVER';$bindingState='BINDING';$sidFile='DRIVER';$bidiFile='BINDING'
$WebDriverPort=9222;$MarionettePort=58549;$ProfileId='selected';$GeckoDriverPath='fixture-driver.exe'
$script:expectedProfileBrowserPid=7;$script:expectedProfileBrowserTicks=638000000000000000
$RecoverOwnedDriver=RECOVER;$script:terminated=$false
$script:owned=[pscustomobject]@{Id=123;Path='fixture-driver.exe';StartTime=[datetime]::new(638000000000000000,[DateTimeKind]::Utc)}
$script:owned | Add-Member ScriptMethod WaitForExit {param($Timeout) return $true}
function Get-LocalTcpListener {param($LocalPort) return @{OwningProcess=123}}
function Get-Process {param($Id,$ErrorAction) return $script:owned}
function Invoke-LocalDriverRest {param($Uri) throw 'fixture driver timed out'}
function Stop-Process {param($Id,[switch]$Force) if($Id -ne 123){throw 'foreign driver'};$script:terminated=$true}
try {
BLOCK
} catch { }
@{terminated=$script:terminated}|ConvertTo-Json -Compress
""".replace('DRIVER',str(state)).replace('BINDING',str(binding)).replace('RECOVER','$true' if recover else '$false').replace('BLOCK',block)
    if owner_change=='listener_pid':setup=setup.replace('return @{OwningProcess=123}','return @{OwningProcess=456}')
    if owner_change=='listener_missing':setup=setup.replace('return @{OwningProcess=123}','return')
    if owner_change=='mixed_listener':setup=setup.replace('return @{OwningProcess=123}','return @(@{OwningProcess=123},@{OwningProcess=456})')
    if owner_change=='driver_path':setup=setup.replace(";Path='fixture-driver.exe'",";Path='foreign-driver.exe'")
    if owner_change in {'driver_start','browser_start','browser_pid'}:
        previous=json.loads(state.read_text())
        field={'driver_start':'started_ticks','browser_start':'browser_started_ticks','browser_pid':'browser_pid'}[owner_change]
        previous[field]+=1
        state.write_text(json.dumps(previous))
    harness.write_text(setup,encoding='utf-8-sig')
    result=subprocess.run(['powershell.exe','-NoProfile','-ExecutionPolicy','Bypass','-File',str(harness)],capture_output=True)
    assert result.returncode==0,result.stderr
    assert json.loads(result.stdout)['terminated'] is expected


def test_native_listener_owner_table_reads_own_ipv4_and_ipv6_listeners(tmp_path):
    harness=tmp_path/'native-listeners.ps1'
    harness.write_text("$ErrorActionPreference='Stop'\n. '"+str(ROOT/'scripts/windows_tcp_listener.ps1')+"'\n"+r"""
$listeners=@(
 [Net.Sockets.TcpListener]::new([Net.IPAddress]::Loopback,0),
 [Net.Sockets.TcpListener]::new([Net.IPAddress]::IPv6Loopback,0)
)
try {
 foreach($server in $listeners) {
  $server.Start()
  $port=$server.LocalEndpoint.Port
  $owners=@(Get-LocalTcpListener -LocalPort $port)
  if($owners.Count -ne 1 -or $owners[0].OwningProcess -ne $PID) {throw 'listener ownership mismatch'}
  $server.Stop()
  if(@(Get-LocalTcpListener -LocalPort $port).Count) {throw 'closed listener still returned'}
 }
} finally {foreach($server in $listeners){$server.Stop()}}
""",encoding='utf-8-sig')
    result=subprocess.run(['powershell.exe','-NoProfile','-ExecutionPolicy','Bypass','-File',str(harness)],capture_output=True)
    assert result.returncode==0,result.stderr


def test_profile_port_resolution_and_ps5_parser(tmp_path):
    script=ROOT/'scripts/bind_flower_worker.ps1';source=script.read_text('utf-8-sig')
    function=source[source.index('function Resolve-ProfileMarionettePort'):source.index('if ($ProfileId)')]
    port=tmp_path/'MarionetteActivePort';port.write_text('58549')
    harness=tmp_path/'test.ps1'
    harness.write_text("$ErrorActionPreference='Stop'\n"+function+r"""
function Get-CimInstance { param($ClassName,$Filter)
 [pscustomobject]@{ProcessId=1;ParentProcessId=0;CreationDate=$script:fixtureCreated;CommandLine='FlowerBrowser --user-data-dir=C:\profiles\abc123 --marionette'}
 [pscustomobject]@{ProcessId=2;ParentProcessId=1;CreationDate=$script:fixtureCreated;CommandLine='FlowerBrowser --user-data-dir=C:\profiles\abc123 --marionette'}
}
function Get-ChildItem { param($LiteralPath,$Filter,[switch]$File,[switch]$Recurse,$ErrorAction)
 [pscustomobject]@{FullName='PORT';LastWriteTimeUtc=$script:fixtureCreated.AddSeconds(1)}
}
$script:fixtureCreated=(Get-Date).ToUniversalTime()
if (@(Get-ProfileBrowserTree 1).Count -ne 2) { throw 'descendant missing' }
if (@(Get-ProfileBrowserTree 7).Count -ne 1) { throw 'foreign tree included' }
if ((Resolve-ProfileMarionettePort 'abc123') -ne 58549) { throw 'wrong port' }
try {Resolve-ProfileMarionettePort 'foreign';throw 'unexpected success'} catch {if($_ -match 'unexpected success'){throw}}
$tokens=$null;$errors=$null
[System.Management.Automation.Language.Parser]::ParseFile('SOURCE',[ref]$tokens,[ref]$errors)|Out-Null
if($errors.Count){throw ($errors|Out-String)}
""".replace('PORT',str(port)).replace('SOURCE',str(script)),encoding='utf-8-sig')
    result=subprocess.run(['powershell.exe','-NoProfile','-ExecutionPolicy','Bypass','-File',str(harness)],capture_output=True)
    assert result.returncode==0,result.stderr
    assert source.index('if ($ProfileId)')<source.index('$listener = @(Get-LocalTcpListener')


def test_binding_is_profile_scoped_and_requires_verified_desktop_activation():
    source=(ROOT/'scripts/bind_flower_worker.ps1').read_text('utf-8-sig')
    assert 'Wait-ProfileMarionettePort $ProfileId' in source
    assert "'--connect-existing', '--marionette-port', $MarionettePort" in source
    assert 'SetForegroundWindow' in source
    assert 'AttachThreadInput' in source
    assert 'started_ticks' in source


def test_binding_writes_session_after_verified_foreground_activation(tmp_path):
    source=(ROOT/'scripts/bind_flower_worker.ps1').read_text('utf-8-sig')
    source=source.replace(". (Join-Path $PSScriptRoot 'windows_tcp_listener.ps1')", ". '"+str(ROOT/'scripts/windows_tcp_listener.ps1')+"'")
    source=source.replace('$root = Split-Path -Parent $PSScriptRoot', "$root = '"+str(tmp_path)+"'")
    driver=tmp_path/'fake-driver.exe';driver.write_bytes(b'not executed')
    port=tmp_path/'MarionetteActivePort';port.write_text('58549')
    mocks=r"""
$script:fixtureCreated=(Get-Date).ToUniversalTime()
function Get-CimInstance { param($ClassName,$Filter)
 if ($Filter -like '*geckodriver.exe*') { return }
 [pscustomobject]@{ProcessId=7;ParentProcessId=0;CreationDate=$script:fixtureCreated;CommandLine='FlowerBrowser --user-data-dir=C:\profiles\abc123 --marionette'}
}
function Get-ChildItem { param($LiteralPath,$Filter,[switch]$File,[switch]$Recurse,$ErrorAction)
 [pscustomobject]@{FullName='PORT';LastWriteTimeUtc=$script:fixtureCreated.AddSeconds(1)}
}
function Get-LocalTcpListener { param($LocalPort) if ($LocalPort -eq 58549) { [pscustomobject]@{OwningProcess=7} } }
function Start-Process { param($FilePath,$ArgumentList,[switch]$PassThru,$WindowStyle,$RedirectStandardOutput,$RedirectStandardError)
 if($ArgumentList[0] -ne '--connect-existing' -or $ArgumentList[2] -ne 58549){throw 'wrong browser target'}
 [pscustomobject]@{Id=123;StartTime=$script:fixtureCreated;HasExited=$false}
}
function Start-Sleep { param($Milliseconds) }
function Show-VerifiedProfileWindow { param($RootPid) if ($RootPid -ne 7) { throw 'wrong foreground owner' }; return @{window_pid=7;activated=$true} }
function Stop-Process { throw 'test must never terminate a real process' }
function Invoke-LocalDriverRest { param($Uri,$Method,$Body,$TimeoutSeconds)
 if($Uri -like '*/status'){return @{value=@{ready=$true}}}
 if($Uri -like '*/session' -and $Method -eq 'Post'){
  return @{value=@{sessionId='offline-session';capabilities=@{webSocketUrl='ws://127.0.0.1:9222/offline'}}}
 }
 throw 'unexpected endpoint'
}
""".replace('PORT',str(port))
    anchor='$bindingDeadline = (Get-Date).AddSeconds($WaitSeconds)'
    assert source.count(anchor)==1
    source=source.replace(anchor,mocks+'\n'+anchor)
    harness=tmp_path/'binding-harness.ps1'
    harness.write_text(mocks+'\n& {\n'+source+'\n} -WorkerName fixture -ProfileId abc123 -WebDriverPort 9222 -GeckoDriverPath "'+str(driver)+'"',encoding='utf-8-sig')
    result=subprocess.run(['powershell.exe','-NoProfile','-ExecutionPolicy','Bypass','-File',str(harness)],capture_output=True)
    assert result.returncode==0,result.stderr
    assert (tmp_path/'runtime/fixture/flower_session_id.txt').read_text()=='offline-session'
    assert (tmp_path/'runtime/fixture/flower_bidi_url.txt').read_text()=='ws://127.0.0.1:9222/offline'
    assert json.loads((tmp_path/'runtime/fixture/binding.json').read_text('utf-8-sig'))['foreground']['activated'] is True
