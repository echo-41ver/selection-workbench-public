import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import {readFileSync} from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import vm from 'node:vm';
import {createOwnedLifecycle} from '../scripts/temu_bidi_lifecycle.mjs';

const source=readFileSync(new URL('../scripts/temu_flower_collect.mjs',import.meta.url),'utf8').replaceAll('\r\n','\n');
async function ledger(t,prior) {
 const directory=await fs.mkdtemp(path.join(os.tmpdir(),'selection-bidi-cleanup-'));
 const file=path.join(directory,'owned.json');
 if(prior)await fs.writeFile(file,JSON.stringify(prior));
 t.after(async()=>{await fs.unlink(file);await fs.rmdir(directory);});
 return file;
}

test('failed cleanup preserves both owned intercepts and unconfirmed blocked requests',async t=>{
 const file=await ledger(t),calls=[];
 const life=createOwnedLifecycle({sid:'fixture',context:'top',file,call:async(method,args)=>{
  calls.push([method,args]);assert.deepEqual(JSON.parse(await fs.readFile(file,'utf8')).blocked_requests,['held']);
  throw new Error('fixture control timeout');}});
 await life.added({intercept:'owned'});life.track('held');
 await assert.rejects(life.cleanup(),error=>error.code==='BIDI_CLEANUP_INCOMPLETE');
 const saved=JSON.parse(await fs.readFile(file,'utf8'));
 assert.deepEqual(saved.intercepts,['owned']);assert.deepEqual(saved.blocked_requests,['held']);
 assert.deepEqual(calls.map(([method])=>method),['network.removeIntercept','network.continueRequest']);
});

test('same-session recovery removes owned intercepts before releasing saved requests',async t=>{
 const prior={sid:'fixture',context:'top',intercepts:['owned'],blocked_requests:['held','gone']};
 const file=await ledger(t,prior),calls=[];
 const call=async(method,args)=>{calls.push([method,args]);if(args.request==='gone')throw new Error('no such request');};
 await createOwnedLifecycle({sid:'fixture',context:'top',file,call}).recover();
 assert.deepEqual(calls.map(([method])=>method),['network.removeIntercept','network.continueRequest','network.continueRequest']);
 const saved=JSON.parse(await fs.readFile(file,'utf8'));
 assert.deepEqual(saved.intercepts,[]);assert.deepEqual(saved.blocked_requests,[]);
 await fs.writeFile(file,JSON.stringify({...prior,sid:'someone-else'}));
 await createOwnedLifecycle({sid:'fixture',context:'top',file,call}).recover();
 assert.equal(calls.length,3);
});

test('recovery cannot declare success while an old request still cannot be released',async t=>{
 const file=await ledger(t,{sid:'fixture',context:'top',intercepts:['owned'],blocked_requests:['held']});
 const life=createOwnedLifecycle({sid:'fixture',context:'top',file,call:async method=>{
  if(method==='network.continueRequest')throw new Error('fixture release timeout');}});
 await assert.rejects(life.recover(),error=>error.code==='BIDI_CLEANUP_INCOMPLETE');
 const saved=JSON.parse(await fs.readFile(file,'utf8'));
 assert.deepEqual(saved.intercepts,[]);assert.deepEqual(saved.blocked_requests,['held']);
});

test('only a tagged non-image continueRequest timeout enters the existing bounded stop',async()=>{
 let expire;
 const callCode=source.slice(source.indexOf('    const call = (method, params = {})'),source.indexOf('    lifecycle = createOwnedLifecycle'));
 const callContext=vm.createContext({seq:0,pending:new Map(),ws:{send(){}},setTimeout:callback=>{expire=callback;return 1;},clearTimeout(){},Error});
 vm.runInContext(callCode+';globalThis.request=call;',callContext);
 const pending=callContext.request('network.continueRequest');expire();
 const timeout=await pending.catch(error=>error);
 assert.equal(timeout.code,'BIDI_COMMAND_TIMEOUT');assert.equal(timeout.method,'network.continueRequest');
 const begin=source.indexOf('        try {\n          await call(!lifecycle.closing');
 const end=source.indexOf("\n      }\n      if (m.method === 'network.responseCompleted')",begin);
 const handling=source.slice(begin,end);
 for(const [image,error,expected] of [[false,timeout,1],[true,timeout,0],
  [false,{code:'BIDI_COMMAND_TIMEOUT',method:'network.failRequest'},0],[false,new Error('fixture other failure'),0]]) {
  const stops=[];let calls=0;
  const context=vm.createContext({isImage:image,isNonEssential:false,request:{request:'held'},requestId:'held',
   lifecycle:{closing:false,released(){}},call:async()=>{calls++;throw error;},imageCancelRaces:0,
   appendEvent:async()=>{},recordBrowserControlTimeout:async details=>stops.push(details)});
  await vm.runInContext('(async()=>{'+handling+'})()',context);
  assert.equal(calls,1);assert.equal(stops.length,expected);
  if(expected){assert.equal(stops[0].method,'BIDI');assert.equal(stops[0].route,'network.continueRequest');}
 }
});

test('cleanup failures are recorded without private text and navigation carries its existing label',async()=>{
 const begin=source.indexOf('async function recordBrowserControlTimeout(');
 const end=source.indexOf('const wd=createBrowserControlClient',begin);
 const events=[],writes=[],exit=new Error('fixture exit');
 const context=vm.createContext({PROGRESS:'progress',EVENTS:'events',QUERY:'fixture',scrollingCollectionStarted:false,
  fs:{readFile:async()=>JSON.stringify({unique_goods:48}),appendFile:async(_file,text)=>events.push(JSON.parse(text))},
  atomicSnapshot:async(_file,text)=>writes.push(JSON.parse(text)),now:()=> 'fixture-time',setTimeout,clearTimeout,
  browserControlCleanup:async()=>{throw Object.assign(new Error('PRIVATE_PROVIDER_BODY'),
   {code:'BIDI_CLEANUP_INCOMPLETE',remaining_intercepts:1,remaining_requests:1});},
  appendEvent:async(type,data)=>events.push({type,...data}),process:{exit:()=>{throw exit;}}});
 vm.runInContext(source.slice(begin,end),context);
 await assert.rejects(vm.runInContext('recordBrowserControlTimeout({method:"BIDI",route:"network.continueRequest"})',context),error=>error===exit);
 const incomplete=events.find(event=>event.type==='BIDI_CLEANUP_INCOMPLETE');
 assert.ok(incomplete);assert.equal(incomplete.remaining_intercepts,1);assert.equal(incomplete.remaining_requests,1);
 assert.equal(writes[0].status,'STOPPED_BROWSER_COMMAND_TIMEOUT');assert.equal(writes[0].unique_goods,48);
 assert.ok(!JSON.stringify(events).includes('PRIVATE_PROVIDER_BODY'));
 const navigation=source.slice(source.indexOf('async function navigateConfirmed('),source.indexOf('\nasync function submitHomeSearch'));
 const commands=[],target='https://www.temu.com/search_result.html';
 const navContext=vm.createContext({wd:async(...args)=>commands.push(args),bootstrapPageState:async()=>({url:target}),
  guardInitialProducts:async()=>({manual:false}),sleep:async()=>{},appendEvent:async()=>{},writeSnapshot:async()=>{}});
 await vm.runInContext(navigation+';navigateConfirmed("'+target+'",/search_result/,"TEMU_EMPTY_SEARCH")',navContext);
 assert.equal(commands.length,1);assert.equal(commands[0][3].commandLabel,'temu_empty_search');
});
