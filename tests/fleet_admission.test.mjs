import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import {spawn} from 'node:child_process';
import {pathToFileURL} from 'node:url';
import {createFleetAdmission,admissionCapacity} from '../scripts/fleet_admission.mjs';
const moduleUrl=pathToFileURL(path.resolve('scripts/fleet_admission.mjs')).href;
const delay=ms=>new Promise(r=>setTimeout(r,ms));
test('memory pressure reduces admission capacity',()=>{
 assert.equal(admissionCapacity(14,100),1);assert.equal(admissionCapacity(25,100),2);
});
test('six real processes share capacity and start spacing',async()=>{
 const directory=await fs.mkdtemp(path.join(os.tmpdir(),'fleet-test-'));
 const source=`import {createFleetAdmission} from ${JSON.stringify(moduleUrl)};const g=createFleetAdmission({directory:${JSON.stringify(directory)},spacingMs:100,pollMs:10,memory:()=>[50,100]});await g.run('test',async()=>{console.log(JSON.stringify({event:'start',at:Date.now()}));await new Promise(r=>setTimeout(r,250));console.log(JSON.stringify({event:'end',at:Date.now()}));});`;
 const results=await Promise.all(Array.from({length:6},()=>new Promise((resolve,reject)=>{
  const p=spawn(process.execPath,['--input-type=module','-e',source]);let out='',err='';p.stdout.on('data',d=>out+=d);p.stderr.on('data',d=>err+=d);p.on('error',reject);p.on('exit',c=>c?reject(Error(err)):resolve(out.trim().split('\n').map(JSON.parse)));
 })));
 const events=results.flat().sort((a,b)=>a.at-b.at||(a.event==='end'?-1:1));let running=0,max=0;
 for(const e of events){running+=e.event==='start'?1:-1;max=Math.max(max,running);}
 assert.ok(max<=2);assert.equal(running,0);
 const starts=events.filter(e=>e.event==='start');assert.equal(starts.length,6);
 for(let i=1;i<starts.length;i++)assert.ok(starts[i].at-starts[i-1].at>=90);
});
test('429 pauses other instances and stop interrupts the queue',async()=>{
 const directory=await fs.mkdtemp(path.join(os.tmpdir(),'fleet-stop-'));const stopFile=path.join(directory,'STOP');
 const first=createFleetAdmission({directory});await first.penalize(120);
 const row=JSON.parse(await fs.readFile(path.join(directory,`cooldown-${process.pid}.json`),'utf8'));assert.ok(row.until>Date.now()+119000);
 const second=createFleetAdmission({directory,stopFile,pollMs:10});let ran=false;
 const queued=second.run('search',()=>{ran=true;});const rejected=assert.rejects(queued,/FLEET_STOP_REQUESTED/);
 await delay(40);await fs.writeFile(stopFile,'stop');await rejected;assert.equal(ran,false);
});
test('failed actions release their slot',async()=>{
 const directory=await fs.mkdtemp(path.join(os.tmpdir(),'fleet-release-'));const g=createFleetAdmission({directory,spacingMs:0,pollMs:10});
 await assert.rejects(g.run('test',()=>{throw Error('expected');}),/expected/);
 assert.equal(await g.run('test',()=>42),42);
 assert.equal((await fs.readdir(directory)).filter(n=>n.startsWith('slot-')).length,0);
});
test('dead process slot is reclaimed',async()=>{
 const directory=await fs.mkdtemp(path.join(os.tmpdir(),'fleet-dead-'));
 await fs.writeFile(path.join(directory,'slot-0.json'),JSON.stringify({pid:2147483647}));
 const g=createFleetAdmission({directory,spacingMs:0,pollMs:10,memory:()=>[1,100]});assert.equal(await g.run('test',()=>42),42);
});
test('memory pressure counts an already occupied second slot',async()=>{
 const directory=await fs.mkdtemp(path.join(os.tmpdir(),'fleet-memory-'));const stopFile=path.join(directory,'STOP');
 await fs.writeFile(path.join(directory,'slot-1.json'),JSON.stringify({pid:process.pid}));
 const g=createFleetAdmission({directory,stopFile,spacingMs:0,pollMs:10,memory:()=>[1,100]});let ran=false;
 const rejected=assert.rejects(g.run('test',()=>{ran=true;}),/FLEET_STOP_REQUESTED/);
 await delay(40);await fs.writeFile(stopFile,'stop');await rejected;assert.equal(ran,false);
});
test('waiting operations run in ticket order without starvation',async()=>{
 const directory=await fs.mkdtemp(path.join(os.tmpdir(),'fleet-fifo-'));const order=[];
 const g=createFleetAdmission({directory,spacingMs:20,pollMs:5,memory:()=>[1,100]});
 let release;let started;
 const ready=new Promise(r=>started=r);
 const first=g.run('first',async()=>{started();await new Promise(r=>release=r);order.push(1);});await ready;
 const second=g.run('second',()=>order.push(2));
 while((await fs.readdir(directory)).filter(n=>n.startsWith('ticket-')).length<1)await delay(5);
 await delay(5);const third=g.run('third',()=>order.push(3));await delay(20);release();
 await Promise.all([first,second,third]);assert.deepEqual(order,[1,2,3]);
});
test('healthy load more bypasses startup queue and spacing',async()=>{
 const directory=await fs.mkdtemp(path.join(os.tmpdir(),'fleet-healthy-'));
 await fs.writeFile(path.join(directory,'schedule.json'),JSON.stringify({nextAt:Date.now()+900000}));
 await fs.writeFile(path.join(directory,'slot-0.json'),JSON.stringify({pid:process.pid}));
 await fs.writeFile(path.join(directory,'slot-1.json'),JSON.stringify({pid:process.pid}));
 const g=createFleetAdmission({directory,pollMs:10});
 assert.equal(await g.run('load_more',()=>42),42);
});

test('Windows sharing violations retry without abandoning the startup queue',async()=>{
 const directory=await fs.mkdtemp(path.join(os.tmpdir(),'fleet-sharing-'));
 const original=fs.readFile;let conflicts=0;
 fs.readFile=async(file,...args)=>{
  if(path.basename(String(file))==='slot-0.json'&&conflicts++<2){const error=new Error('Windows sharing violation');error.code='EPERM';throw error;}
  return original.call(fs,file,...args);
 };
 try{
  const g=createFleetAdmission({directory,spacingMs:0,pollMs:5,memory:()=>[1,100]});
  assert.equal(await g.run('startup',()=>42),42);assert.ok(conflicts>=2);
  assert.ok(!(await fs.readdir(directory)).some(name=>name.startsWith('slot-')||name.startsWith('ticket-')));
 }finally{fs.readFile=original;}
});
test('load more still honors server cooldown and can be stopped',async()=>{
 const directory=await fs.mkdtemp(path.join(os.tmpdir(),'fleet-pages-'));const stopFile=path.join(directory,'STOP');
 const g=createFleetAdmission({directory,stopFile,pollMs:10});await g.penalize(120);
 let ran=false;const rejected=assert.rejects(g.run('load_more',()=>{ran=true;}),/FLEET_STOP_REQUESTED/);
 await delay(30);await fs.writeFile(stopFile,'stop');await rejected;assert.equal(ran,false);
});


test('uncommitted atomic snapshots are not admission records',async t=>{
 for(const kind of ['ticket','cooldown'])await t.test(kind,async()=>{
  const directory=await fs.mkdtemp(path.join(os.tmpdir(),'fleet-publication-'));
  const finalName=kind==='ticket'?'ticket-0000000000000000-in-progress.json':'cooldown-2147483647.json';
  const snapshotUrl=pathToFileURL(path.resolve('scripts/snapshot_atomic.mjs')).href;
  const source=`import fs from 'node:fs/promises';import path from 'node:path';import {atomicSnapshot} from ${JSON.stringify(snapshotUrl)};
   let release;const released=new Promise(resolve=>release=resolve);process.stdin.once('data',()=>release());
   const timer=setTimeout(()=>{throw Error('snapshot fixture was not released');},10000);
   await atomicSnapshot(path.join(${JSON.stringify(directory)},${JSON.stringify(finalName)}),JSON.stringify({pid:process.pid,until:0,observedAt:0}),{filesystem:{...fs,writeFile:async(file,text,encoding)=>{
    await fs.writeFile(file,'',encoding);console.log(JSON.stringify({temporary:file}));await released;await fs.writeFile(file,text,encoding);
   }}});clearTimeout(timer);process.stdin.destroy();`;
  const child=spawn(process.execPath,['--input-type=module','-e',source]);let out='',err='';
  let staged;const ready=new Promise(resolve=>staged=resolve);
  child.stdout.on('data',data=>{out+=data;const end=out.indexOf('\n');if(end!==-1)staged(JSON.parse(out.slice(0,end)));});
  child.stderr.on('data',data=>err+=data);
  const completed=new Promise((resolve,reject)=>{child.once('error',reject);child.once('close',code=>code===0?resolve():reject(Error(`snapshot writer exited ${code}: ${err}`)));});
  // Observe rejection now, while still awaiting the publication barrier.
  completed.catch(()=>{});
  try{
   const row=await Promise.race([ready,completed.then(()=>{throw Error('snapshot writer exited before its barrier');})]);
   assert.ok(path.basename(row.temporary).startsWith(finalName+'.'));
   assert.ok(row.temporary.endsWith('.tmp'));
   assert.equal(await fs.readFile(row.temporary,'utf8'),'');
   await assert.rejects(fs.access(path.join(directory,finalName)),{code:'ENOENT'});
   const gate=createFleetAdmission({directory,spacingMs:0,pollMs:5,memory:()=>[50,100]});
   assert.equal(await gate.run('startup',()=>42),42);
   if(kind==='cooldown')assert.equal(await gate.run('load_more',()=>43),43);
   assert.equal(await fs.readFile(row.temporary,'utf8'),'');
  }finally{
   child.stdin.end('release\n');await completed;
   assert.ok(JSON.parse(await fs.readFile(path.join(directory,finalName),'utf8')).pid>0);
   await fs.rm(directory,{recursive:true,force:true});
  }
 });
});

test('malformed committed admission records fail closed',async t=>{
 for(const kind of ['ticket','cooldown'])await t.test(kind,async()=>{
  const directory=await fs.mkdtemp(path.join(os.tmpdir(),'fleet-invalid-'));
  try{
   const name=kind==='ticket'?'ticket-0000000000000000-invalid.json':'cooldown-2147483647.json';
   await fs.writeFile(path.join(directory,name),'{');
   const gate=createFleetAdmission({directory,spacingMs:0,pollMs:5,memory:()=>[50,100]});let ran=false;
   await assert.rejects(gate.run('startup',()=>{ran=true;}),SyntaxError);assert.equal(ran,false);
   if(kind==='cooldown')await assert.rejects(gate.run('load_more',()=>{ran=true;}),SyntaxError);
   assert.equal(ran,false);
  }finally{await fs.rm(directory,{recursive:true,force:true});}
 });
});


test('actual dispatch spacing includes admission preparation while actions still overlap',{timeout:10000},async t=>{
 const directory=await fs.mkdtemp(path.join(os.tmpdir(),'fleet-dispatch-barrier-')),stopFile=path.join(directory,'STOP');
 let releaseHook,enteredHook,signalSecond,expire;
 const hookEntered=new Promise(r=>enteredHook=r),hookRelease=new Promise(r=>releaseHook=r),secondStarted=new Promise(r=>signalSecond=r);
 const expired=new Promise((_,reject)=>expire=reject);expired.catch(()=>{});
 let first,second,running=0,max=0,watchdogFired=false,failure;const starts=[];
 const watchdog=setTimeout(()=>{watchdogFired=true;expire(Error('Admission fixture setup/dispatch exceeded 5 seconds'));},5000);
 try{
  const options={directory,stopFile,spacingMs:100,pollMs:5,memory:()=>[50,100]};
  const a=createFleetAdmission({...options,onAdmit:async()=>{enteredHook();await hookRelease;}}),b=createFleetAdmission(options);
  first=a.run('first',async()=>{starts.push(Date.now());max=Math.max(max,++running);await secondStarted;running--;});first.catch(()=>{});
  await Promise.race([hookEntered,expired,first.then(()=>{throw Error('First admission completed before its hook');},error=>{throw Error('First admission failed before its hook',{cause:error});})]);
  // A real preparation barrier must still own the dispatch mutex. The old
  // reservation-only gate had already removed it before entering this hook.
  const mutex=JSON.parse(await fs.readFile(path.join(directory,'admission.lock'),'utf8'));assert.equal(mutex.pid,process.pid);
  second=b.run('second',async()=>{starts.push(Date.now());max=Math.max(max,++running);signalSecond();running--;});second.catch(()=>{});
  releaseHook();await Promise.race([Promise.all([first,second]),expired]);
  assert.equal(watchdogFired,false);assert.equal(starts.length,2);assert.ok(starts[1]-starts[0]>=90,JSON.stringify({actual_callback_entries:starts}));
  assert.equal(max,2);assert.equal(running,0);
  assert.ok(!(await fs.readdir(directory)).some(name=>name.startsWith('slot-')||name.startsWith('ticket-')||name==='admission.lock'));
  t.diagnostic(JSON.stringify({actual_callback_entries:starts,actual_spacing_ms:starts[1]-starts[0],maximum_running:max}));
 }catch(error){failure=error;throw error;}
 finally{
  clearTimeout(watchdog);releaseHook();signalSecond();let settleTimer;
  try{
   await fs.writeFile(stopFile,'stop');
   const outcomes=await Promise.race([Promise.allSettled([first,second].filter(Boolean)),new Promise((_,reject)=>{settleTimer=setTimeout(()=>reject(Error('Owned admission promises did not settle within 1 second after STOP')),1000);})]);
   const unexpected=outcomes.filter(row=>row.status==='rejected'&&row.reason!==failure&&row.reason!==failure?.cause&&row.reason?.message!=='FLEET_STOP_REQUESTED').map(row=>row.reason);
   if(unexpected.length)throw new AggregateError(unexpected,'Unexpected owned admission cleanup failures');
   await fs.rm(directory,{recursive:true,force:true});
  }catch(error){if(failure)throw new AggregateError([failure,error],'Admission fixture/cleanup failed');throw error;}
  finally{clearTimeout(settleTimer);}
 }
});

test('admission rejection and stop clean owned records before any dispatch',async t=>{
 for(const kind of ['reject','stop'])await t.test(kind,async()=>{
  const directory=await fs.mkdtemp(path.join(os.tmpdir(),'fleet-dispatch-cancel-')),stopFile=path.join(directory,'STOP');
  const expected=new Error('admission rejected');let hookFails=true,ran=false;
  const gate=createFleetAdmission({directory,stopFile,spacingMs:0,pollMs:5,onAdmit:async()=>{
   if(!hookFails)return;
   if(kind==='reject')throw expected;
   await fs.writeFile(stopFile,'stop');
  }});
  try{
   await assert.rejects(gate.run('startup',()=>{ran=true;}),error=>kind==='reject'?error===expected:error.message==='FLEET_STOP_REQUESTED');
   assert.equal(ran,false);assert.ok(!(await fs.readdir(directory)).some(name=>name.startsWith('slot-')||name.startsWith('ticket-')||name==='admission.lock'));
   hookFails=false;if(kind==='stop')await fs.unlink(stopFile);
   assert.equal(await gate.run('startup',()=>42),42);
  }finally{await fs.rm(directory,{recursive:true,force:true});}
 });
});

test('mutex cleanup failure retains capacity until the action settles and preserves both errors',{timeout:10000},async()=>{
 const directory=await fs.mkdtemp(path.join(os.tmpdir(),'fleet-dispatch-cleanup-'));
 const original=fs.unlink,cleanupError=Object.assign(new Error('mutex release failed'),{code:'EIO'}),actionError=new Error('action failed after release error');
 let releaseAction,enteredAction,failedRelease,work,rejected,watchdogFired=false;
 const actionEntered=new Promise(r=>enteredAction=r),actionRelease=new Promise(r=>releaseAction=r),releaseFailed=new Promise(r=>failedRelease=r);
 let actionFinished=false;const watchdog=setTimeout(()=>{watchdogFired=true;releaseAction();},5000);
 fs.unlink=async(file,...args)=>{if(path.basename(String(file))==='admission.lock'){failedRelease();throw cleanupError;}return original.call(fs,file,...args);};
 try{
  const gate=createFleetAdmission({directory,spacingMs:0,pollMs:5});
  work=gate.run('startup',async()=>{enteredAction();await actionRelease;actionFinished=true;throw actionError;});
  rejected=assert.rejects(work,error=>error instanceof AggregateError&&error.errors.includes(cleanupError)&&error.errors.includes(actionError));rejected.catch(()=>{});
  await Promise.race([actionEntered,work.then(()=>{throw Error('Action completed before entry');},error=>{throw Error('Admission failed before entering its action',{cause:error});})]);
  await Promise.race([releaseFailed,work.then(()=>{throw Error('Action completed before mutex cleanup');},error=>{throw Error('Action failed before mutex cleanup',{cause:error});})]);
  assert.ok((await fs.readdir(directory)).some(name=>name.startsWith('slot-')));assert.equal(actionFinished,false);
  releaseAction();await rejected;assert.equal(actionFinished,true);assert.equal(watchdogFired,false);
  const remaining=await fs.readdir(directory);assert.ok(remaining.includes('admission.lock'));
  assert.ok(!remaining.some(name=>name.startsWith('slot-')||name.startsWith('ticket-')));
 }finally{
  fs.unlink=original;releaseAction();await Promise.allSettled([work,rejected].filter(Boolean));clearTimeout(watchdog);await fs.rm(directory,{recursive:true,force:true});
 }
});
