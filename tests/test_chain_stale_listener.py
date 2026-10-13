import pathlib,shutil,subprocess
import pytest

@pytest.mark.parametrize('listener_script,expected', [('owned', '202'), ('foreign', '')])
def test_stale_pid_reconciles_only_owned_lane_listener(tmp_path,listener_script,expected):
    shell=shutil.which('pwsh')
    if not shell:pytest.skip('PowerShell 7 unavailable')
    root=pathlib.Path(__file__).resolve().parents[1]
    source=(root/'scripts/proxyshare_chain_manager.ps1').read_text('utf-8')
    function=source[source.index('function Stop-WorkerChain'):source.index('$settings = if')]
    fixture=tmp_path/'stop.ps1'
    fixture.write_text('''$ErrorActionPreference='Stop'
$chainScript='C:\\owned\\scripts\\proxyshare_chain_proxy.mjs'
$script:stopped=@()
function Get-NetTCPConnection { param($LocalPort,$State,$ErrorAction) if($LocalPort -ne 17893){throw 'wrong lane'}; [pscustomobject]@{OwningProcess=202} }
function Get-CimInstance { param($ClassName,$Filter,$ErrorAction)
if($Filter -eq 'ProcessId=101'){return $null}
[pscustomobject]@{Name='node.exe';CommandLine='node "SCRIPT"'} }
function Stop-Process { param($Id,[switch]$Force,$ErrorAction) $script:stopped+= $Id }
function Wait-Process { param($Id,$Timeout,$ErrorAction) }
'''.replace('SCRIPT',r'C:\owned\scripts\proxyshare_chain_proxy.mjs' if listener_script=='owned' else r'C:\other\scripts\proxyshare_chain_proxy.mjs')+function+'''
Stop-WorkerChain @{pid=101;port=17893}
[Console]::Write(($script:stopped -join ','))
''',encoding='utf-8')
    result=subprocess.run([shell,'-NoProfile','-File',str(fixture)],capture_output=True,text=True,timeout=10)
    assert result.returncode==0,result.stderr
    assert result.stdout.strip()==expected
