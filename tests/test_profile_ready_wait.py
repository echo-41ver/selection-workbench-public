"""Execute real binding functions with local process/port snapshots; no browser."""
import subprocess,shutil
from pathlib import Path
import pytest

ROOT=Path(__file__).resolve().parents[1]

def test_selected_profile_readiness_and_ownership_are_bounded(tmp_path):
    modes=['delayed','missing','bad_port','exited','pid_reused','ambiguous','foreign_port','stale_file']
    shell=shutil.which('pwsh') or shutil.which('powershell')
    if not shell:pytest.skip('PowerShell required for real function execution')
    script=tmp_path/'test.ps1'
    script.write_text(r'''
$ErrorActionPreference='Stop'
$errors=$null;$tokens=$null
$ast=[Management.Automation.Language.Parser]::ParseFile($args[0],[ref]$tokens,[ref]$errors)
if($errors.Count){throw 'binding parse failed'}
foreach($name in @('Resolve-ProfileMarionettePort','Wait-ProfileMarionettePort')) {
 $node=$ast.Find({param($x) $x -is [Management.Automation.Language.FunctionDefinitionAst] -and $x.Name -eq $name},$true)
 . ([scriptblock]::Create($node.Extent.Text))
}
foreach($case in $args[1].Split(',')) {
$script:mode=$case;$script:calls=0;$script:files=0
$script:created=[datetime]::UtcNow
function Get-CimInstance {
 param($ClassName,$Filter)
 $script:calls++
 if($script:mode -eq 'exited' -and $script:calls -gt 1){return @()}
 $birth=if($script:mode -eq 'pid_reused' -and $script:calls -gt 1){$script:created.AddSeconds(1)}else{$script:created}
 $row=[pscustomobject]@{ProcessId=101;ParentProcessId=90;CreationDate=$birth;CommandLine='FlowerBrowser --user-data-dir=C:/fixture/profile-TEST-flower'}

 if($script:mode -eq 'ambiguous'){return @($row,$row)}
 return @($row)
}
function Get-ChildItem {
 $script:files++
 if($script:mode -eq 'missing' -or $script:files -lt 2){return @()}
 $stamp=if($script:mode -eq 'stale_file'){$script:created.AddDays(-1)}else{$script:created.AddSeconds(1)}
 return [pscustomobject]@{FullName='fixture-port';LastWriteTimeUtc=$stamp;LastWriteTime=$stamp}
}
function Get-Content {if($script:mode -eq 'bad_port'){'99999'}else{'58791'}}
function Get-NetTCPConnection { [pscustomobject]@{OwningProcess=$(if($script:mode -eq 'foreign_port'){202}else{101})} }
function Get-ProfileBrowserTree {return @(101)}
$clock=[Diagnostics.Stopwatch]::StartNew()
try {
 $port=Wait-ProfileMarionettePort 'profile-TEST' 2
 if($script:mode -ne 'delayed' -or $port -ne 58791){throw 'unexpected binding accepted'}
 Write-Output 'READY_CORRECT_OWNER'
} catch {
 if($script:mode -eq 'delayed'){Write-Output "FIXTURE_COUNTS cim=$script:calls files=$script:files";throw}
 if($_.Exception.Message -eq 'unexpected binding accepted'){throw}
 $expected=@{missing='bounded deadline';stale_file='bounded deadline';bad_port='Invalid profile';exited='exited while waiting';pid_reused='ownership changed';ambiguous='one unique';foreign_port='belongs to another process'}
 if($_.Exception.Message -notmatch $expected[$script:mode]){throw}
 Write-Output ('REJECTED_'+$script:mode)
}
if($clock.Elapsed.TotalSeconds -gt 3){throw 'deadline exceeded'}
}
''',encoding='utf-8')
    result=subprocess.run([shell,'-NoProfile','-File',str(script),str(ROOT/'scripts/bind_flower_worker.ps1'),','.join(modes)],capture_output=True,text=True,timeout=25)
    assert result.returncode==0,result.stdout+'\n'+result.stderr
    for mode in modes:
        assert ('READY_CORRECT_OWNER' if mode=='delayed' else 'REJECTED_'+mode) in result.stdout
