import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';
import {endpoint} from '../scripts/temu_network_evidence.mjs';

const source=fs.readFileSync(new URL('../scripts/temu_flower_collect.mjs',import.meta.url),'utf8');
const begin=source.indexOf('async function navigateConfirmed(');
const end=source.indexOf('\nasync function submitHomeSearch',begin);
const functionSource=source.slice(begin,end);
const target='https://www.temu.com/search_result.html';
const challenge='https://www.temu.com/bgn_verification.html';

async function run({readyAfter=null,verification=false,commandError=false,commandTimeout=false}={}){
  let time=0,navigations=0,started=0;
  const events=[],snapshots=[];
  const context=vm.createContext({endpoint,finalStatus:null,
    sleep:async ms=>{time+=ms},
    wd:async method=>{if(method==='POST'){navigations++;started=time;if(commandTimeout){const e=new Error('queued');e.kind='TIMEOUT';throw e;}if(commandError)throw new Error('navigation timeout');}},
    bootstrapPageState:async()=>({url:verification?challenge:readyAfter!==null&&time-started>=readyAfter?target:'newtab'}),
    guardInitialProducts:async state=>({manual:state.url===challenge}),
    appendEvent:async(type,data)=>events.push({type,...data}),
    writeSnapshot:async(status,data)=>snapshots.push({status,...data})});
  let value,error;
  try {value=await vm.runInContext('class InitialProductsStopped extends Error {}\n'+functionSource+
    '\nnavigateConfirmed('+JSON.stringify(target)+',/^https:\\/\\/www\\.temu\\.com\\/search_result\\.html$/,"TEMU_EMPTY_SEARCH")',context);}
  catch(e){error=e;}
  return {value,error,time,navigations,events,snapshots,status:context.finalStatus};
}

test('initial verification navigation is handed off without refreshing its page',async()=>{
  for(const commandError of [false,true]){
    const result=await run({verification:true,commandError});
    assert.equal(result.value,challenge);assert.equal(result.navigations,1);
    assert.equal(result.status,null);assert.equal(result.error,undefined);
    assert.ok(result.events.some(e=>e.type==='BOOTSTRAP_VERIFICATION_HANDOFF'));
    assert.ok(!result.events.some(e=>e.type==='BOOTSTRAP_NAVIGATION_RETRY'));
  }
});

test('first navigation taking seven seconds is observed without a duplicate navigation',async()=>{
  const result=await run({readyAfter:7000});
  assert.equal(result.value,target);assert.equal(result.navigations,1);
  assert.equal(result.time,7000);assert.equal(result.error,undefined);
});

test('persistently unchanged new tab records bounded recoverable failure',async()=>{
  const result=await run();
  assert.equal(result.navigations,2);assert.equal(result.time,40000);
  assert.equal(result.status,'STOPPED_BOOTSTRAP_NAVIGATION');assert.ok(result.error);
  const state=result.snapshots.at(-1);
  assert.equal(state.keyword_complete,false);assert.equal(state.automatic_recovery_eligible,true);
  assert.equal(state.resume_requires_session_rotation,true);assert.equal(state.last_page,'newtab');
});


test('uncertain command timeout never inspects or resends its queued navigation',async()=>{
 const result=await run({commandTimeout:true,readyAfter:0});
 assert.equal(result.navigations,1);assert.equal(result.time,0);assert.equal(result.error.kind,'TIMEOUT');
 assert.deepEqual(result.events,[]);assert.deepEqual(result.snapshots,[]);
});
