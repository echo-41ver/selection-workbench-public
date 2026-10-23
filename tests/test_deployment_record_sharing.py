"""Native Windows marker I/O only; never starts an installer or task."""
from pathlib import Path
import os
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / 'scripts/deployment_entry.ps1'
PS = Path(os.environ.get('SystemRoot', r'C:\Windows')) / 'System32/WindowsPowerShell/v1.0/powershell.exe'


@unittest.skipUnless(os.name == 'nt' and PS.exists(), 'Windows native file sharing')
class RecordSharingTests(unittest.TestCase):
    def invoke(self, mode):
        source = SOURCE.read_text('utf-8-sig')
        helper = source[source.index('function Read-Record('):source.index('function File-Sha(')]
        with tempfile.TemporaryDirectory(prefix='entry-sharing-') as folder:
            script = Path(folder) / 'sharing.ps1'
            script.write_text(r'''param([string]$Mode)
$ErrorActionPreference='Stop'
$utf8=[Text.UTF8Encoding]::new($false)
Add-Type @'
using System;using System.IO;using System.Threading;
public sealed class EntryMarkerHold:IDisposable{
 readonly FileStream stream;readonly Timer timer;
 public EntryMarkerHold(string path,bool exclusive,int releaseMs){
  stream=new FileStream(path,FileMode.Open,FileAccess.ReadWrite,exclusive?FileShare.None:FileShare.ReadWrite|FileShare.Delete);
  if(releaseMs>0)timer=new Timer(_=>stream.Dispose(),null,releaseMs,Timeout.Infinite);
 }
 public void Dispose(){if(timer!=null)timer.Dispose();stream.Dispose();}
}
'@
''' + helper + r'''
$path=Join-Path $PSScriptRoot 'marker.json'
[IO.File]::WriteAllText($path,'{"nonce":"TEST_ONLY_NONCE","value":"已发布"}',$utf8)
$hold=$null
try{
 if($Mode -eq 'shared_writer'){$hold=[EntryMarkerHold]::new($path,$false,0)}
 elseif($Mode -eq 'transient_exclusive'){$hold=[EntryMarkerHold]::new($path,$true,400)}
 elseif($Mode -eq 'persistent_exclusive'){$hold=[EntryMarkerHold]::new($path,$true,0)}
 else{throw 'Unexpected test mode'}
 $watch=[Diagnostics.Stopwatch]::StartNew()
 if($Mode -eq 'persistent_exclusive'){
  $refused=$false
  try{$null=Read-Record $path}catch{
   $base=$_.Exception.GetBaseException()
   if(-not ($base -is [IO.IOException]) -or ($base.HResult -band 65535) -notin @(32,33)){throw}
   $refused=$true
  }
  if(-not $refused -or $watch.Elapsed.TotalSeconds -lt 1 -or $watch.Elapsed.TotalSeconds -gt 7){throw 'Persistent sharing error was hidden or was not bounded'}
 }else{
  $record=Read-Record $path
  if(-not $record -or $record.nonce -cne 'TEST_ONLY_NONCE' -or $record.value -cne '已发布'){throw 'Published marker was lost or altered'}
 }
}finally{if($hold){$hold.Dispose()}}
if($null -ne (Read-Record (Join-Path $PSScriptRoot 'not-published.json'))){throw 'Unpublished marker was fabricated'}
''', encoding='utf-8-sig')
            result = subprocess.run([str(PS), '-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass',
                                     '-File', str(script), '-Mode', mode], capture_output=True,
                                    text=True, encoding='utf-8', errors='replace', timeout=25)
            self.assertEqual(result.returncode, 0, result.stderr)

    def test_published_record_is_readable_while_another_writer_holds_it(self):
        self.invoke('shared_writer')

    def test_transient_exclusive_lock_recovers_before_native_deadline(self):
        self.invoke('transient_exclusive')

    def test_persistent_lock_fails_bounded_without_a_fabricated_record(self):
        self.invoke('persistent_exclusive')


if __name__ == '__main__':
    unittest.main()
