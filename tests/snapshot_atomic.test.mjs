import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import {spawn} from 'node:child_process';
import {atomicSnapshot,replaceWindowsSnapshot} from '../scripts/snapshot_atomic.mjs';

test('temporary reader locks retry without exposing partial or missing snapshots',async()=>{
  const dir=await fs.mkdtemp(path.join(os.tmpdir(),'snapshot-lock-'));
  try {
    const file=path.join(dir,'products.jsonl');await fs.writeFile(file,'old\n');let calls=0;
    await atomicSnapshot(file,'new\n',{sleep:async()=>{},filesystem:{...fs,rename:async(...args)=>{
      assert.equal(await fs.readFile(file,'utf8'),'old\n');
      if(++calls<4)throw Object.assign(new Error('reader holds file'),{code:['EPERM','EACCES','EBUSY'][calls-1]});
      return fs.rename(...args);
    }}});
    assert.equal(calls,4);assert.equal(await fs.readFile(file,'utf8'),'new\n');
  }finally{await fs.rm(dir,{recursive:true,force:true});}
});

test('permanent errors and exhausted lock retries preserve the last complete snapshot',async()=>{
  for(const [code,expected] of [['ENOENT',1],['EPERM',3]]){
    const dir=await fs.mkdtemp(path.join(os.tmpdir(),'snapshot-error-'));
    try {
      const file=path.join(dir,'products.jsonl');await fs.writeFile(file,'old\n');let calls=0;
      await assert.rejects(()=>atomicSnapshot(file,'new\n',{attempts:3,sleep:async()=>{},
        replaceSnapshot:async()=>{throw Object.assign(new Error('replacement also denied'),{code});},
        filesystem:{...fs,rename:async()=>{calls++;throw Object.assign(new Error('failure'),{code});}}}),{code});
      assert.equal(calls,expected);assert.equal(await fs.readFile(file,'utf8'),'old\n');
    }finally{await fs.rm(dir,{recursive:true,force:true});}
  }
});

async function holdWindowsReader(file,shareDelete) {
  const quote=value=>`'${value.replaceAll("'","''")}'`;
  const script=`$ErrorActionPreference='Stop';$share=[IO.FileShare]::ReadWrite${shareDelete?' -bor [IO.FileShare]::Delete':''};`+
    `$stream=[IO.File]::Open(${quote(file)},[IO.FileMode]::Open,[IO.FileAccess]::Read,$share);`+
    `try{[Console]::Out.WriteLine('ENTERED');[Console]::Out.Flush();$null=[Console]::In.ReadLine();`+
    `$stream.Position=0;$reader=[IO.StreamReader]::new($stream);[Console]::Out.WriteLine($reader.ReadToEnd())}`+
    `finally{$stream.Dispose()}`;
  const executable=path.join(process.env.SystemRoot||'C:\\Windows','System32','WindowsPowerShell','v1.0','powershell.exe');
  const child=spawn(executable,['-NoLogo','-NoProfile','-NonInteractive','-EncodedCommand',Buffer.from(script,'utf16le').toString('base64')],
    {windowsHide:true,stdio:['pipe','pipe','pipe']});
  let stdout='',stderr='',entered;
  const ready=new Promise(resolve=>entered=resolve);
  child.stdout.on('data',data=>{stdout+=data;if(stdout.includes('ENTERED'))entered();});
  child.stderr.on('data',data=>stderr+=data);
  const done=new Promise((resolve,reject)=>{
    child.once('error',reject);
    child.once('close',code=>code===0?resolve(stdout):reject(Error(`Windows reader exited ${code}: ${stderr}`)));
  });
  done.catch(()=>{});
  let timer;
  try {
    await Promise.race([ready,done.then(()=>{throw Error('Windows reader exited before opening its file');}),
      new Promise((_,reject)=>{timer=setTimeout(()=>reject(Error('Windows reader did not open within 15 seconds')),15000);})]);
  } catch(error) { child.stdin.end('release\n');await done;throw error; }
  finally { clearTimeout(timer); }
  return {release:()=>child.stdin.end('release\n'),done};
}

test('Windows atomic replacement works while a delete-sharing reader retains the old snapshot',
  {skip:process.platform!=='win32',timeout:30000},async()=>{
    const dir=await fs.mkdtemp(path.join(os.tmpdir(),"snapshot-中文 & 'reader-"));
    let reader;
    try {
      const file=path.join(dir,'progress.json');await fs.writeFile(file,'old-complete\n');
      reader=await holdWindowsReader(file,true);let attempts=0;
      await atomicSnapshot(file,'new-complete\n',{sleep:async()=>{},filesystem:{...fs,rename:async(...args)=>{
        attempts++;return fs.rename(...args);
      }}});
      assert.equal(attempts,20);
      assert.equal(await fs.readFile(file,'utf8'),'new-complete\n');
      assert.deepEqual((await fs.readdir(dir)).filter(name=>name.endsWith('.tmp')),[]);
      reader.release();assert.equal(await reader.done,'ENTERED\r\nold-complete\n\r\n');reader=null;
    } finally { if(reader){reader.release();await reader.done;}await fs.rm(dir,{recursive:true,force:true}); }
  });

test('Windows readers denying delete-sharing remain real bounded failures with both complete snapshots preserved',
  {skip:process.platform!=='win32',timeout:30000},async()=>{
    const dir=await fs.mkdtemp(path.join(os.tmpdir(),'snapshot-no-delete-'));let reader;
    try {
      const file=path.join(dir,'progress.json');await fs.writeFile(file,'old-complete\n');
      reader=await holdWindowsReader(file,false);let attempts=0;
      await assert.rejects(()=>atomicSnapshot(file,'new-complete\n',{attempts:3,sleep:async()=>{},
        filesystem:{...fs,rename:async(...args)=>{attempts++;return fs.rename(...args);}}}),
      error=>error.code==='EPERM'&&error.cause?.code===1&&error.cause.stderr.trim()==='System.IO.IOException:-2147024864');
      assert.equal(attempts,3);assert.equal(await fs.readFile(file,'utf8'),'old-complete\n');
      const temporary=(await fs.readdir(dir)).filter(name=>name.endsWith('.tmp'));assert.equal(temporary.length,1);
      assert.equal(await fs.readFile(path.join(dir,temporary[0]),'utf8'),'new-complete\n');
    } finally { if(reader){reader.release();await reader.done;}await fs.rm(dir,{recursive:true,force:true}); }
  });

test('Windows brief reader conflicts still recover through the original rename retries',
  {skip:process.platform!=='win32',timeout:30000},async()=>{
    const dir=await fs.mkdtemp(path.join(os.tmpdir(),'snapshot-short-reader-'));let reader;
    try {
      const file=path.join(dir,'progress.json');await fs.writeFile(file,'old-complete\n');
      reader=await holdWindowsReader(file,false);let attempts=0,waits=0;
      await atomicSnapshot(file,'new-complete\n',{filesystem:{...fs,rename:async(...args)=>{attempts++;return fs.rename(...args);}},
        replaceSnapshot:async()=>assert.fail('Brief conflicts must not reach native replacement'),
        sleep:async()=>{if(++waits===2){reader.release();await reader.done;reader=null;}}});
      assert.equal(attempts,3);assert.equal(waits,2);assert.equal(await fs.readFile(file,'utf8'),'new-complete\n');
    } finally { if(reader){reader.release();await reader.done;}await fs.rm(dir,{recursive:true,force:true}); }
  });

test('Windows native replacement has a bounded hidden shell-free process and literal path environment',async()=>{
  const source="D:\\中文 & 'literal\\progress.json.tmp",destination="D:\\中文 & 'literal\\progress.json";
  let calls=0;
  await replaceWindowsSnapshot(source,destination,{execute:async(executable,args,options)=>{
    calls++;assert.equal(executable,path.join(process.env.SystemRoot||'C:\\Windows','System32','WindowsPowerShell','v1.0','powershell.exe'));
    assert.deepEqual(args.slice(0,4),['-NoLogo','-NoProfile','-NonInteractive','-EncodedCommand']);
    const script=Buffer.from(args[4],'base64').toString('utf16le');
    assert.ok(!script.includes(source)&&!script.includes(destination));assert.match(script,/\[IO\.File\]::Replace/);
    assert.equal(options.env.SWB_ATOMIC_REPLACEMENT,source);assert.equal(options.env.SWB_ATOMIC_DESTINATION,destination);
    assert.equal(options.timeout,10000);assert.equal(options.shell,false);assert.equal(options.windowsHide,true);
  }});
  assert.equal(calls,1);
});

test('failed Windows fallback preserves the original error and full old snapshot without extra retries',async()=>{
  const dir=await fs.mkdtemp(path.join(os.tmpdir(),'snapshot-fallback-failure-'));
  try {
    const file=path.join(dir,'progress.json');await fs.writeFile(file,'old\n');
    const original=Object.assign(new Error('original rename failure'),{code:'EPERM'});
    const timeout=Object.assign(new Error('replacement timed out'),{code:'ETIMEDOUT',killed:true,signal:'SIGTERM'});
    let attempts=0,replacements=0;
    await assert.rejects(()=>atomicSnapshot(file,'new\n',{platform:'win32',attempts:3,sleep:async()=>{},
      filesystem:{...fs,rename:async()=>{attempts++;throw original;}},
      replaceSnapshot:async()=>{replacements++;throw timeout;}}),error=>error===original&&error.cause===timeout);
    assert.equal(attempts,3);assert.equal(replacements,1);assert.equal(await fs.readFile(file,'utf8'),'old\n');
    const temporary=(await fs.readdir(dir)).filter(name=>name.endsWith('.tmp'));assert.equal(temporary.length,1);
    assert.equal(await fs.readFile(path.join(dir,temporary[0]),'utf8'),'new\n');
  } finally { await fs.rm(dir,{recursive:true,force:true}); }
});

test('non-Windows rename failures never invoke the Windows native replacement',async()=>{
  const dir=await fs.mkdtemp(path.join(os.tmpdir(),'snapshot-posix-failure-'));
  try {
    const file=path.join(dir,'progress.json');await fs.writeFile(file,'old\n');let replacements=0,attempts=0;
    await assert.rejects(()=>atomicSnapshot(file,'new\n',{platform:'linux',attempts:3,sleep:async()=>{},
      filesystem:{...fs,rename:async()=>{attempts++;throw Object.assign(new Error('denied'),{code:'EPERM'});}},
      replaceSnapshot:async()=>{replacements++;}}),{code:'EPERM'});
    assert.equal(replacements,0);assert.equal(attempts,3);assert.equal(await fs.readFile(file,'utf8'),'old\n');
  } finally { await fs.rm(dir,{recursive:true,force:true}); }
});
