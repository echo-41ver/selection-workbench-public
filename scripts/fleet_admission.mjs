import fs from 'node:fs/promises';
import path from 'node:path';
import os from 'node:os';
import crypto from 'node:crypto';
import {atomicSnapshot} from './snapshot_atomic.mjs';
const sleep=ms=>new Promise(r=>setTimeout(r,ms));
const busyFile=e=>['EPERM','EACCES','EBUSY'].includes(e?.code);
async function sharedFile(operation){
 for(let attempt=0;;attempt++){
  try{return await operation();}
  catch(e){if(!busyFile(e)||attempt>=6)throw e;await sleep(Math.min(120,10*2**attempt));}
 }
}
const readShared=(...args)=>sharedFile(()=>fs.readFile(...args));
const removeShared=(...args)=>sharedFile(()=>fs.unlink(...args));
const alive=pid=>{try{process.kill(pid,0);return true;}catch(e){return e.code==='EPERM';}};
export function admissionCapacity(free,total){return total>0&&free/total<0.15?1:2;}
export function createFleetAdmission({directory,stopFile,onWait=async()=>{},onAdmit=async()=>{},spacingMs=5000,pollMs=300,memory=()=>[os.freemem(),os.totalmem()]}){
 const owner=process.pid;
 async function checkStop(){if(stopFile&&await fs.access(stopFile).then(()=>true,()=>false))throw new Error('FLEET_STOP_REQUESTED');}
 async function claim(file){
  try{const h=await fs.open(file,'wx');try{await h.writeFile(JSON.stringify({pid:owner,at:Date.now()}));}finally{await h.close();}return true;}
  catch(e){if(busyFile(e))return false;if(e.code!=='EEXIST')throw e;}
  try{const row=JSON.parse(await readShared(file,'utf8'));if(!alive(row.pid))await removeShared(file);}
  catch(e){if(e instanceof SyntaxError){const stat=await fs.stat(file).catch(()=>null);if(stat&&Date.now()-stat.mtimeMs>30000)await removeShared(file).catch(()=>{});}else if(e.code!=='ENOENT'&&!busyFile(e))throw e;}
  return false;
 }
 async function penalties(){
  let until=0,slow=false;
  for(const name of await fs.readdir(directory)){
   if(!name.startsWith('cooldown-'))continue;
   try{const p=JSON.parse(await readShared(path.join(directory,name),'utf8'));until=Math.max(until,Number(p.until)||0);slow ||= Number(p.observedAt)>Date.now()-900000;}catch(e){if(e.code!=='ENOENT')throw e;}
  }
  return {until,slow};
 }
 async function penalize(seconds=0){
  await fs.mkdir(directory,{recursive:true});
  const file=path.join(directory,`cooldown-${owner}.json`);
  const old=JSON.parse(await readShared(file,'utf8').catch(()=> '{}'));
  const count=old.observedAt>Date.now()-900000?Number(old.count||0)+1:1;
  const delay=Math.max(Math.min(900,60*2**Math.min(count-1,4)),Number.isFinite(Number(seconds))?Number(seconds):0);
  await atomicSnapshot(file,JSON.stringify({count,observedAt:Date.now(),until:Math.max(Number(old.until)||0,Date.now()+delay*1000)}));
 }
 async function run(operation,action){
  await fs.mkdir(directory,{recursive:true});let lastReport=0;
  // Existing healthy collection must not wait behind browser startup work.
  // Only an active server-rate-limit cooldown can defer the next page request.
  if(operation==='load_more'){
   while(true){
    await checkStop();const penalty=await penalties();
    if(penalty.until<=Date.now()){await onAdmit(operation);return await action();}
    if(Date.now()-lastReport>=5000){lastReport=Date.now();await onWait({operation,cooldown_until:penalty.until,reason:'HTTP_COOLDOWN'});}
    await sleep(pollMs);
   }
  }
  const ticketName=`ticket-${String(Date.now()).padStart(16,'0')}-${crypto.randomUUID()}.json`;
  const ticket=path.join(directory,ticketName);
  await atomicSnapshot(ticket,JSON.stringify({pid:owner}));
  try { while(true){
   await checkStop();const penalty=await penalties();const capacity=admissionCapacity(...memory());let slot=null;
   const queued=[];
   for(const name of (await fs.readdir(directory)).filter(n=>n.startsWith('ticket-')).sort()){
    try{const row=JSON.parse(await readShared(path.join(directory,name),'utf8'));
     if(alive(row.pid))queued.push(name);else await removeShared(path.join(directory,name)).catch(()=>{});
    }catch(e){if(e.code!=='ENOENT')throw e;}
   }
   let occupied=0;
   for(let i=0;i<2;i++){
    try{const row=JSON.parse(await readShared(path.join(directory,`slot-${i}.json`),'utf8'));if(alive(row.pid))occupied++;}
    catch(e){if(e instanceof SyntaxError||busyFile(e))occupied++;else if(e.code!=='ENOENT')throw e;}
   }
   if(penalty.until<=Date.now()&&queued[0]===ticketName&&occupied<capacity){
    for(let i=0;i<capacity;i++){const candidate=path.join(directory,`slot-${i}.json`);if(await claim(candidate)){slot=candidate;break;}}
   }
   if(slot){
    let admitted=false;const mutex=path.join(directory,'admission.lock');
    try{
     if(await claim(mutex)){
      try{
       const state=JSON.parse(await readShared(path.join(directory,'schedule.json'),'utf8').catch(()=> '{}'));
       const latest=await penalties();
       if(Math.max(Number(state.nextAt)||0,latest.until)<=Date.now()){
        await atomicSnapshot(path.join(directory,'schedule.json'),JSON.stringify({nextAt:Date.now()+spacingMs*(latest.slow?3:1)}));admitted=true;
       }
      }finally{await removeShared(mutex);}
     }
     if(admitted){await removeShared(ticket);await checkStop();await onAdmit(operation);return await action();}
    }finally{await removeShared(slot).catch(e=>{if(e.code!=='ENOENT')throw e;});}
   }
   if(Date.now()-lastReport>=5000){lastReport=Date.now();await onWait({operation,capacity,cooldown_until:penalty.until||null,reason:penalty.until>Date.now()?'HTTP_COOLDOWN':'CAPACITY_OR_SPACING'});}
   await sleep(pollMs);
  }} finally {await removeShared(ticket).catch(e=>{if(e.code!=='ENOENT')throw e;});}
 }
 return {run,penalize};
}
