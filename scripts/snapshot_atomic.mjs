import fs from 'node:fs/promises';
import {randomUUID} from 'node:crypto';
import {execFile} from 'node:child_process';
import path from 'node:path';
import {promisify} from 'node:util';

const executeFile=promisify(execFile);
export function windowsSnapshotFailure(error,{elapsedMs=null}={}) {
  // Keep only bounded native codes. Never log command, path, env, or raw stderr.
  const native=typeof error?.code==='number'&&Number.isSafeInteger(error.code)?error.code:null;
  const code=native??(['EPERM','EACCES','EBUSY','ENOENT','ETIMEDOUT','UNKNOWN'].includes(error?.code)?error.code:null);
  const signal=typeof error?.signal==='string'&&/^SIG[A-Z0-9]{1,12}$/.test(error.signal)?error.signal:null;
  const match=String(error?.stderr||'').match(/\b(System\.[A-Za-z.]{1,100}):(-?\d{1,12})\b/);
  const hresult=match&&Number(match[2])>=-2147483648&&Number(match[2])<=2147483647?Number(match[2]):null;
  return {phase:'WINDOWS_ATOMIC_REPLACE',code,killed:error?.killed===true,signal,
    native_type:hresult===null?null:match[1],hresult,timeout_ms:30000,
    elapsed_ms:Number.isFinite(elapsedMs)?Math.round(elapsedMs):null};
}
export async function replaceWindowsSnapshot(temp,file,{execute=executeFile}={}) {
  // MoveFileExW (used by fs.rename) cannot replace an open Windows destination,
  // even when its reader grants FILE_SHARE_DELETE. ReplaceFileW can; .NET's
  // File.Replace exposes that atomic primitive without a new runtime dependency.
  const command="$ErrorActionPreference='Stop';try{[IO.File]::Replace($env:SWB_ATOMIC_REPLACEMENT,$env:SWB_ATOMIC_DESTINATION,[NullString]::Value)}catch{"+
    "$failure=$_.Exception;while($failure.InnerException){$failure=$failure.InnerException};"+
    "[Console]::Error.WriteLine(($failure.GetType().FullName+':'+$failure.HResult));exit 1}";
  const powershell=path.join(process.env.SystemRoot||'C:\\Windows','System32','WindowsPowerShell','v1.0','powershell.exe');
  // Cold PowerShell startup is part of this bounded native replacement budget.
  // Concurrent Windows execution exhausted the previous10s helper budget.
  const started=performance.now();
  try {
    await execute(powershell,['-NoLogo','-NoProfile','-NonInteractive','-EncodedCommand',Buffer.from(command,'utf16le').toString('base64')],
      {windowsHide:true,shell:false,timeout:30000,maxBuffer:64*1024,
        env:{...process.env,SWB_ATOMIC_REPLACEMENT:String(temp),SWB_ATOMIC_DESTINATION:String(file)}});
  } catch(error) {
    error.snapshot_replacement=windowsSnapshotFailure(error,{elapsedMs:performance.now()-started});
    console.error(JSON.stringify({event:'WINDOWS_SNAPSHOT_REPLACEMENT_FAILED',diagnostic:error.snapshot_replacement}));
    throw error;
  }
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
          catch(replaceError) { error.cause=replaceError;error.snapshot_replacement=replaceError?.snapshot_replacement??windowsSnapshotFailure(replaceError); }
        }
        // A reader that denies delete-sharing (or a genuine permissions error)
        // must still fail. Never unlink or truncate the previous full snapshot.
        throw error;
      }
      await sleep(Math.min(100*attempt,500));
    }
  }
}
