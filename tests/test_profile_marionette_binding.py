from pathlib import Path
import subprocess
import json
ROOT=Path(__file__).resolve().parents[1]


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
    assert source.index('if ($ProfileId)')<source.index('$listener = Get-NetTCPConnection')


def test_binding_is_profile_scoped_and_requires_verified_desktop_activation():
    source=(ROOT/'scripts/bind_flower_worker.ps1').read_text('utf-8-sig')
    assert 'Wait-ProfileMarionettePort $ProfileId' in source
    assert "'--connect-existing', '--marionette-port', $MarionettePort" in source
    assert 'SetForegroundWindow' in source
    assert 'AttachThreadInput' in source
    assert 'started_ticks' in source


def test_binding_writes_session_after_verified_foreground_activation(tmp_path):
    source=(ROOT/'scripts/bind_flower_worker.ps1').read_text('utf-8-sig')
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
function Get-NetTCPConnection { param($LocalPort,$State,$ErrorAction) if ($LocalPort -eq 58549) { [pscustomobject]@{OwningProcess=7} } }
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
