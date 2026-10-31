import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import {readFileSync} from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import vm from 'node:vm';
import {createOwnedLifecycle} from '../scripts/temu_bidi_lifecycle.mjs';
import {classifyPageState} from '../scripts/temu_flower_recovery_policy.mjs';
import {decideInitialProductsWait,decideEmptyQueryResults} from '../scripts/temu_initial_wait_policy.mjs';
import {terminalCollectionStop} from '../scripts/collection_checkpoint.mjs';
import {supersededByVerification} from '../scripts/temu_network_evidence.mjs';
import {captchaNeedsManualVerification,sessionRecoveryRequest} from '../scripts/temu_session_recovery.mjs';

const source=readFileSync(new URL('../scripts/temu_flower_collect.mjs',import.meta.url),'utf8').replaceAll('\r\n','\n');
const timeoutCode=source.slice(source.indexOf('async function recordBrowserControlTimeout('),source.indexOf('const wd=createBrowserControlClient'));
const guardCode=source.slice(source.indexOf('async function guardInitialProducts('),source.indexOf('\nasync function navigateConfirmed('));
const handlingStart=source.indexOf('        try {\n          await call(!lifecycle.closing');
const handling=source.slice(handlingStart,source.indexOf("\n      }\n      if (m.method === 'network.responseCompleted')",handlingStart));

async function fixture(t,{human=true,scrolling=false,cleanupFails=false}={}) {
 const directory=await fs.mkdtemp(path.join(os.tmpdir(),'selection-captcha-handoff-'));
 const file=path.join(directory,'owned.json'),events=[],writes=[],actions=[];
 t.after(async()=>{await fs.unlink(file);await fs.rmdir(directory);});
 const life=createOwnedLifecycle({sid:'fixture',context:'top',file,call:async(method)=>{
  actions.push(method);
  if(cleanupFails)throw new Error('PRIVATE_PROVIDER_DETAIL');
 }});
 await life.added({intercept:'owned'});
 for(let i=0;i<6;i++)life.track('held-'+i);
 const exit=Object.assign(new Error('fixture collector exit'),{exitCode:2});
 const state={url:human?'https://www.temu.com/bgn_verification.html?verifyCode=fixture':'https://www.temu.com/search_result.html',
  text:human?'Security Verification':'',visibleGoodsCount:0,loading:false};
 const context=vm.createContext({URL,QUERY:'fixture',PROGRESS:'progress',EVENTS:'events',
  humanVerificationActive:human,scrollingCollectionStarted:scrolling,httpBlock:null,products:new Map(),
  initialProductsWait:null,emptyResultsWait:null,INITIAL_PRODUCTS_TIMEOUT_SECONDS:30,lastInitialWaitingSnapshot:0,finalStatus:null,
  classify:classifyPageState,decideInitialProductsWait,decideEmptyQueryResults,terminalCollectionStop,supersededByVerification,
  confirmedLoginRedirect:()=>false,InitialProductsStopped:Error,bootstrapPageState:async()=>state,
  fs:{readFile:async()=>JSON.stringify({unique_goods:48}),appendFile:async(_file,text)=>events.push(JSON.parse(text))},
  atomicSnapshot:async(_file,text)=>writes.push(JSON.parse(text)),now:()=> 'fixture-time',setTimeout,clearTimeout,
  appendEvent:async(type,data)=>{if(type==='MANUAL_GATE_OBSERVED')actions.push('manual-gate');events.push({type,...data});},
  writeSnapshot:async(status,extra)=>writes.push({status,...extra}),
  browserControlCleanup:async()=>{actions.push('cleanup');await life.cleanup();},
  process:{exit:()=>{throw exit;}},
  lifecycle:life,isImage:false,isNonEssential:false,imageBlocked:0,nonEssentialBlocked:0,imageCancelRaces:0,
  call:async()=>{throw Object.assign(new Error('BiDi timeout: network.continueRequest'),
   {code:'BIDI_COMMAND_TIMEOUT',method:'network.continueRequest'});},state});
 vm.runInContext(timeoutCode+guardCode,context);
 const run=async code=>{
  try{return await vm.runInContext(code,context);}
  catch(error){if(error!==exit)throw error;return {exited:true};}
 };
 return {events,writes,actions,file,context,guard:()=>run('guardInitialProducts(state,"empty_search_stability")'),
  intercept:async i=>{context.requestId='held-'+i;context.request={request:'held-'+i};return run('(async()=>{'+handling+'})()');}};
}

test('confirmed first-screen CAPTCHA releases owned interception and six late timeouts keep the manual handoff',async t=>{
 const h=await fixture(t);
 await Promise.all(Array.from({length:6},(_,i)=>h.intercept(i)));
 assert.equal(h.writes.filter(row=>row.status==='STOPPED_BROWSER_COMMAND_TIMEOUT').length,0);
 const result=await h.guard();
 assert.equal(h.events.filter(row=>row.type==='NETWORK_INTERCEPT_ERROR').length,6);
 assert.equal(h.writes.length,1);assert.equal(h.writes[0].status,'WAITING_HUMAN_VERIFICATION');
 assert.equal(h.writes[0].collector_paused,true);assert.equal(result.exited,true);
 assert.equal(captchaNeedsManualVerification(h.writes[0].status,{scroll_collection_started:false}),true);
 assert.equal(sessionRecoveryRequest(h.writes[0].status,{scroll_collection_started:false},true),null);
 assert.equal(h.actions.filter(action=>action==='cleanup').length,1);
 assert.ok(h.actions.indexOf('cleanup')<h.actions.indexOf('manual-gate'));
 assert.ok(h.actions.indexOf('network.removeIntercept')<h.actions.indexOf('network.continueRequest'));
 const saved=JSON.parse(await fs.readFile(h.file,'utf8'));
 assert.deepEqual(saved.intercepts,[]);assert.deepEqual(saved.blocked_requests,[]);
});

test('failed CAPTCHA cleanup stays manual and preserves unconfirmed requests without reporting success',async t=>{
 const h=await fixture(t,{cleanupFails:true});
 assert.equal((await h.guard()).exited,true);
 const incomplete=h.events.find(row=>row.type==='BIDI_CLEANUP_INCOMPLETE');
 assert.ok(incomplete);assert.equal(incomplete.remaining_intercepts,1);assert.equal(incomplete.remaining_requests,6);
 assert.equal(h.writes[0].status,'WAITING_HUMAN_VERIFICATION');
 assert.equal(h.writes.filter(row=>row.status==='STOPPED_BROWSER_COMMAND_TIMEOUT').length,0);
 assert.equal(JSON.stringify(incomplete).includes('PRIVATE_PROVIDER_DETAIL'),false);
 const saved=JSON.parse(await fs.readFile(h.file,'utf8'));
 assert.deepEqual(saved.intercepts,['owned']);assert.equal(saved.blocked_requests.length,6);
});

test('ordinary and post-scroll continue timeouts retain the existing control-stop behavior',async t=>{
 for(const options of [{human:false},{human:true,scrolling:true}]){
  const h=await fixture(t,options);
  await h.intercept(0);
  assert.equal(h.writes.length,1);assert.equal(h.writes[0].status,'STOPPED_BROWSER_COMMAND_TIMEOUT');
  assert.equal(h.writes[0].unique_goods,48);assert.equal(h.writes[0].manual_action_required,false);
  assert.equal(h.writes[0].automatic_retry,true);assert.equal(h.actions[0],'cleanup');
 }
});
