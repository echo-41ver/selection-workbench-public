import fs from 'node:fs/promises';
import {randomUUID} from 'node:crypto';

export async function atomicSnapshot(file, text, {filesystem=fs,
  sleep=ms=>new Promise(resolve=>setTimeout(resolve,ms)), attempts=20}={}) {
  const temp=`${file}.${process.pid}.${randomUUID()}.tmp`;
  await filesystem.writeFile(temp,text,'utf8');
  for(let attempt=1;attempt<=attempts;attempt++) {
    try { await filesystem.rename(temp,file);return; }
    catch(error) {
      // Windows readers (Excel export / union watcher) briefly deny replace.
      // Keep the previous complete file intact until the atomic rename works.
      if(!['EPERM','EACCES','EBUSY'].includes(error.code)||attempt===attempts)throw error;
      await sleep(Math.min(100*attempt,500));
    }
  }
}
