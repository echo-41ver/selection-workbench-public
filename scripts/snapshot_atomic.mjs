import fs from 'node:fs/promises';
import {randomUUID} from 'node:crypto';
import {execFile} from 'node:child_process';
import path from 'node:path';
import {promisify} from 'node:util';

const executeFile=promisify(execFile);
export async function replaceWindowsSnapshot(temp,file,{execute=executeFile}={}) {
  // MoveFileExW (used by fs.rename) cannot replace an open Windows destination,
  // even when its reader grants FILE_SHARE_DELETE. ReplaceFileW can; .NET's
  // File.Replace exposes that atomic primitive without a new runtime dependency.
  const command="$ErrorActionPreference='Stop';try{[IO.File]::Replace($env:SWB_ATOMIC_REPLACEMENT,$env:SWB_ATOMIC_DESTINATION,[NullString]::Value)}catch{"+
    "$failure=$_.Exception;while($failure.InnerException){$failure=$failure.InnerException};"+
    "[Console]::Error.WriteLine(($failure.GetType().FullName+':'+$failure.HResult));exit 1}";
  const powershell=path.join(process.env.SystemRoot||'C:\\Windows','System32','WindowsPowerShell','v1.0','powershell.exe');
  await execute(powershell,['-NoLogo','-NoProfile','-NonInteractive','-EncodedCommand',Buffer.from(command,'utf16le').toString('base64')],
    {windowsHide:true,shell:false,timeout:10000,maxBuffer:64*1024,
      env:{...process.env,SWB_ATOMIC_REPLACEMENT:String(temp),SWB_ATOMIC_DESTINATION:String(file)}});
}

export async function atomicSnapshot(file, text, {filesystem=fs,
  sleep=ms=>new Promise(resolve=>setTimeout(resolve,ms)), attempts=20,
  replaceSnapshot=replaceWindowsSnapshot,platform=process.platform}={}) {
  const temp=`${file}.${process.pid}.${randomUUID()}.tmp`;
  await filesystem.writeFile(temp,text,'utf8');
  for(let attempt=1;attempt<=attempts;attempt++) {
    try { await filesystem.rename(temp,file);return; }
    catch(error) {
      // Windows readers (Excel export / union watcher) briefly deny replace.
      // Keep the previous complete file intact until the atomic rename works.
      if(!['EPERM','EACCES','EBUSY'].includes(error.code))throw error;
      if(attempt===attempts) {
        if(platform==='win32') {
          try { await replaceSnapshot(temp,file);return; }
          catch(replaceError) { error.cause=replaceError; }
        }
        // A reader that denies delete-sharing (or a genuine permissions error)
        // must still fail. Never unlink or truncate the previous full snapshot.
        throw error;
      }
      await sleep(Math.min(100*attempt,500));
    }
  }
}
