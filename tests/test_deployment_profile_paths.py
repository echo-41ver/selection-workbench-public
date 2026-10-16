"""Run the real PowerShell resolver with new-machine Windows user path fixtures."""
import os
from pathlib import Path
import subprocess
import sys

import pytest

ROOT=Path(__file__).resolve().parents[1]


@pytest.mark.skipif(sys.platform!='win32',reason='Windows browser binding')
@pytest.mark.parametrize('option,folder',[
    ('--user-data-dir=',r'C:\Users\Test User\AppData\Roaming\adspower\TEST_ONLY_PROFILE'),
    ('--profile ',r'C:\Users\张三 用户\AppData\Roaming\adspower\TEST_ONLY_PROFILE'),
    ('--profile-directory=',r'C:\Users\Test User\Profiles\TEST_ONLY_PROFILE'),
    ('--user-data-dir=',r'C:\Users\Tester\Profiles\TEST_ONLY_PROFILE'),
])
def test_resolve_complete_profile_path_including_spaces_and_chinese(tmp_path,option,folder):
    script=tmp_path/'resolve.ps1'
    script.write_text(r'''
$ErrorActionPreference='Stop'
$env:PSModulePath=(Join-Path $PSHOME 'Modules')+';'+$env:PSModulePath
$tokens=$null; $errors=$null
$ast=[System.Management.Automation.Language.Parser]::ParseFile($env:BINDING_SCRIPT,[ref]$tokens,[ref]$errors)
if ($errors) { throw ($errors|Out-String) }
$function=$ast.Find({param($node) $node -is [System.Management.Automation.Language.FunctionDefinitionAst] -and $node.Name -eq 'Resolve-ProfileMarionettePort'},$true)
. ([scriptblock]::Create($function.Extent.Text))
$script:created=(Get-Date).ToUniversalTime()
function Get-CimInstance { param($ClassName,$Filter) [pscustomobject]@{ProcessId=2018;ParentProcessId=1;CreationDate=$script:created;CommandLine=$env:PROFILE_COMMAND} }
function Get-ChildItem {
 param($LiteralPath,$Filter,[switch]$File,[switch]$Recurse,$ErrorAction)
 if ($LiteralPath -cne $env:PROFILE_FOLDER) { throw 'Profile path truncated or changed' }
 [pscustomobject]@{FullName=(Join-Path $LiteralPath 'MarionetteActivePort');LastWriteTimeUtc=$script:created.AddSeconds(1)}
}
function Get-Content { param($LiteralPath,[switch]$Raw) '39912' }
$port=Resolve-ProfileMarionettePort 'TEST_ONLY_PROFILE'
if ($port -ne 39912) { throw 'Incorrect port' }
Write-Output 'COMPLETE_PROFILE_PATH_OK'
''',encoding='utf-8-sig')
    quoted='"'+folder+'"' if ' ' in folder else folder
    result=subprocess.run(['powershell.exe','-NoProfile','-NonInteractive','-ExecutionPolicy','Bypass','-File',str(script)],
        env={**os.environ,'BINDING_SCRIPT':str(ROOT/'scripts/bind_flower_worker.ps1'),
             'PROFILE_COMMAND':'FlowerBrowser.exe --marionette '+option+quoted,'PROFILE_FOLDER':folder},capture_output=True,timeout=30)
    assert result.returncode==0,result.stderr
    assert b'COMPLETE_PROFILE_PATH_OK' in result.stdout
