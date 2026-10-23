import json, shutil, subprocess, threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
import pytest
ROOT=Path(__file__).resolve().parents[1]
def test_incomplete_binding_reclaims_only_matching_profile_and_browser(tmp_path):
    shell=shutil.which('pwsh') or shutil.which('powershell')
    if not shell:pytest.skip('PowerShell unavailable')
    source=(ROOT/'scripts/bind_flower_worker.ps1').read_text('utf-8')
    decision_start=source.index('$sameOwnedProfile =')
    decision=source[decision_start:source.index('if (-not $incompleteOwnedBinding',decision_start)]
    script=tmp_path/'binding-ownership.ps1'
    script.write_text('''$ProfileId='selected'
$script:expectedProfileBrowserPid=100
$script:expectedProfileBrowserTicks=200
$previous=[pscustomobject]@{profile_id='selected';browser_pid=100;browser_started_ticks=200}
$owned=[pscustomobject]@{Id=300}
'''+''.join('''$bindingRecord=[pscustomobject]@{geckodriver_pid=%d;phase='%s'}
%s
if([bool]$incompleteOwnedBinding -ne $%s){throw 'wrong ownership decision'}
'''%(pid,phase,decision,str(expected).lower()) for pid,phase,expected in [
        (299,'BOUND',True),(300,'REQUESTING_SESSION',True),
        (300,'SESSION_BIND_FAILED',True),(300,'BOUND',False)])+
'''$previous.profile_id='another'
$bindingRecord=$null
'''+decision+'''
if($incompleteOwnedBinding){throw 'another profile was reclaimed'}
$previous.profile_id='selected';$previous.browser_started_ticks=201
'''+decision+'''
if($incompleteOwnedBinding){throw 'restarted browser identity was ignored'}
''',encoding='utf-8-sig')
    result=subprocess.run([shell,'-NoProfile','-File',str(script)],capture_output=True,text=True,timeout=15)
    assert result.returncode==0,result.stderr

def test_binding_rest_is_loopback_only_and_bypasses_proxy(tmp_path):
    shell=shutil.which('pwsh') or shutil.which('powershell')
    if not shell:pytest.skip('PowerShell unavailable')
    source=(ROOT/'scripts/bind_flower_worker.ps1').read_text('utf-8')
    helper=source[source.index('function Invoke-LocalDriverRest'):source.index('function Show-VerifiedProfileWindow')]
    received=[]
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            received.append(json.loads(self.rfile.read(int(self.headers['Content-Length']))))
            self.send_response(200);self.end_headers();self.wfile.write(b'{"value":{"ready":true}}')
        def log_message(self,*args):pass
    server=HTTPServer(('127.0.0.1',0),Handler)
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    try:
        script=tmp_path/'rest.ps1'
        script.write_text(helper+f'''\n$env:HTTP_PROXY='http://127.0.0.1:1'
$env:HTTPS_PROXY='http://127.0.0.1:1'
$v=Invoke-LocalDriverRest 'http://127.0.0.1:{server.server_port}/session' -Method POST -Body '{{"probe":true}}'
if(-not $v.value.ready){{throw 'response failed'}}
$refused=$false
try{{Invoke-LocalDriverRest 'http://example.invalid/'}}catch{{$refused=$true}}
if(-not $refused){{throw 'non-loopback request allowed'}}
''',encoding='utf-8-sig')
        result=subprocess.run([shell,'-NoProfile','-File',str(script)],capture_output=True,text=True,timeout=20)
        assert result.returncode==0,result.stderr
        assert received==[{'probe':True}]
    finally:server.shutdown();server.server_close()
def test_binding_foreground_and_logs_remain_owned_by_selected_profile():
    source=(ROOT/'scripts/bind_flower_worker.ps1').read_text('utf-8')
    assert 'Get-ProfileBrowserTree $RootPid' in source
    assert 'if ($windows.Count -ne 1)' in source
    assert 'Show-VerifiedProfileWindow $script:expectedProfileBrowserPid' in source
    assert source.index('$foreground = if ($ProfileId)') < source.index('$session = Invoke-LocalDriverRest')
    assert 'retained rather than creating a second session' in source
    assert 'RedirectStandardError' in source and 'local_rest_proxy' in source

def test_foreground_retries_detach_input_queues_and_stop_on_owner_change(tmp_path):
    shell=shutil.which('pwsh') or shutil.which('powershell')
    if not shell:pytest.skip('PowerShell unavailable')
    source=(ROOT/'scripts/bind_flower_worker.ps1').read_text('utf-8')
    helper=source[source.index('function Show-VerifiedProfileWindow'):source.index('function Resolve-ProfileMarionettePort')]
    script=tmp_path/'focus.ps1'
    script.write_text('''Add-Type @"
using System;
public static class OwnedFlowerForeground {
 public static int Sets=0, Attached=0, Detached=0;
 public static bool Lost=false, Blocked=false;
 public static bool SetForegroundWindow(IntPtr h){Sets++;return !Blocked && Sets>=3;}
 public static bool ShowWindowAsync(IntPtr h,int c){return true;}
 public static bool IsIconic(IntPtr h){return false;}
 public static IntPtr GetForegroundWindow(){return new IntPtr(!Blocked && Sets>=3?401288:123);}
 public static bool BringWindowToTop(IntPtr h){return true;}
 public static uint GetWindowThreadProcessId(IntPtr h,out uint pid){pid=h.ToInt64()==401288?(Lost?999u:62788u):100u;return h.ToInt64()==401288?22u:11u;}
 public static bool AttachThreadInput(uint a,uint b,bool attach){if(attach)Attached++;else Detached++;return true;}
 public static uint GetCurrentThreadId(){return 44u;}
}
"@
function Get-ProfileBrowserTree { return @(62788) }
function Get-Process { return [pscustomobject]@{Id=62788;MainWindowHandle=[IntPtr]401288;StartTime=[DateTime]'2026-01-01'} }
'''+helper+'''
$r=Show-VerifiedProfileWindow 52248
if($r.attempts -ne 3 -or $r.window_pid -ne 62788){throw 'wrong target or missing bounded retry'}
if([OwnedFlowerForeground]::Attached -ne 6 -or [OwnedFlowerForeground]::Detached -ne 6){throw 'input queues not detached'}
[OwnedFlowerForeground]::Lost=$true
$refused=$false
try{Show-VerifiedProfileWindow 52248}catch{$refused=$true}
if(-not $refused){throw 'ownership change was ignored'}
[OwnedFlowerForeground]::Lost=$false
[OwnedFlowerForeground]::Blocked=$true
[OwnedFlowerForeground]::Sets=0
$script:prompts=0
function Request-BrowserDisplayConfirmation {
    $script:prompts++
    [OwnedFlowerForeground]::Blocked=$false
    return 0.25
}
$r=Show-VerifiedProfileWindow 52248
if($script:prompts -ne 1 -or [OwnedFlowerForeground]::Sets -ne 13 -or $r.confirmation_seconds -ne 0.25 -or -not $r.activated){throw 'confirmation did not return to actual foreground verification'}
[OwnedFlowerForeground]::Blocked=$true
[OwnedFlowerForeground]::Sets=0
function Request-BrowserDisplayConfirmation {throw 'TEST_ONLY_CANCELLED'}
$refused=$false
try{Show-VerifiedProfileWindow 52248}catch{if($_.Exception.Message -ne 'TEST_ONLY_CANCELLED'){throw};$refused=$true}
if(-not $refused -or [OwnedFlowerForeground]::Sets -ne 12){throw 'cancelled confirmation was ignored or retried'}
[OwnedFlowerForeground]::Sets=0
function Request-BrowserDisplayConfirmation {[OwnedFlowerForeground]::Lost=$true;return 0.1}
$refused=$false
try{Show-VerifiedProfileWindow 52248}catch{if($_.Exception.Message -notmatch 'ownership changed'){throw};$refused=$true}
if(-not $refused -or [OwnedFlowerForeground]::Sets -ne 12){throw 'window changed during confirmation was activated'}
''',encoding='utf-8-sig')
    result=subprocess.run([shell,'-NoProfile','-File',str(script)],capture_output=True,text=True,timeout=15)
    assert result.returncode==0,result.stderr
