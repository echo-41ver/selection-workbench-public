import test from 'node:test';
import assert from 'node:assert/strict';
import http from 'node:http';
import fs from 'node:fs';
import vm from 'node:vm';
import {browserControlRequest,BrowserControlError,createBrowserControlClient} from '../scripts/browser_control_request.mjs';

test('a stalled local driver is aborted rather than holding the collector indefinitely',async()=>{
  const server=http.createServer(()=>{});
  await new Promise(resolve=>server.listen(0,'127.0.0.1',resolve));
  try{
    await assert.rejects(browserControlRequest({base:`http://127.0.0.1:${server.address().port}`,session:'test',
      method:'GET',suffix:'/url',timeoutMs:100}),error=>error instanceof BrowserControlError&&error.kind==='TIMEOUT');
  }finally{server.closeAllConnections();await new Promise(resolve=>server.close(resolve));}
});
test('driver response parsing and connection failures remain distinguishable without exposing URLs',async()=>{
  const request={base:'http://private.invalid',session:'private-session',method:'POST',suffix:'/element/private-id/click'};
  await assert.rejects(browserControlRequest({...request,fetchImpl:async()=>{throw TypeError('credential=private');}}),
    error=>error.kind==='CONNECTION_FAILED'&&!error.message.includes('private')&&!error.message.includes('credential'));
  await assert.rejects(browserControlRequest({...request,fetchImpl:async()=>({json:async()=>{throw SyntaxError('bad');}})}),
    error=>error.kind==='INVALID_RESPONSE');
  assert.deepEqual(await browserControlRequest({...request,fetchImpl:async()=>({ok:true,json:async()=>({value:{ready:true}})})}),{ready:true});
});


test('deadline covers fetch implementations and body readers that ignore abort',async()=>{
 for(const fetchImpl of [()=>new Promise(()=>{}),async()=>({ok:true,json:()=>new Promise(()=>{})})]){
  const started=Date.now();
  await assert.rejects(browserControlRequest({base:'http://local',session:'s',method:'POST',suffix:'/url',timeoutMs:25,fetchImpl}),e=>e.kind==='TIMEOUT');
  assert.ok(Date.now()-started<1000);
 }
});

test('uncertain navigation poisons further driver commands and invokes pause exactly once',async()=>{
 const calls=[],stops=[];
 const request=createBrowserControlClient({base:'http://local',session:'s',navigationTimeoutMs:25,commandTimeoutMs:25,
  fetchImpl:async(url)=>{calls.push(url);return new Promise(()=>{});},onTimeout:async e=>stops.push(e.kind)});
 await assert.rejects(request('POST','/url',{url:'https://www.temu.com/'}),e=>e.kind==='TIMEOUT');
 for(const [method,suffix] of [['GET','/url'],['POST','/execute/sync'],['POST','/url']])
  await assert.rejects(request(method,suffix,{}),e=>e.kind==='TIMEOUT');
 assert.equal(calls.length,1);assert.deepEqual(stops,['TIMEOUT']);
});

test('an explicit completed driver error does not poison later safe observations',async()=>{
 let calls=0;const request=createBrowserControlClient({base:'http://local',session:'s',fetchImpl:async()=>({ok:true,json:async()=>++calls===1?{value:{error:'no such element'}}:{value:'ready'}})});
 await assert.rejects(request('POST','/element/x/click'),/no such element/);
 assert.equal(await request('GET','/url'),'ready');
});


test('collector timeout preserves prior checkpoint and cleans owned interception without closing browser',async()=>{
 const source=fs.readFileSync(new URL('../scripts/temu_flower_collect.mjs',import.meta.url),'utf8');
 const code=source.slice(source.indexOf('let browserControlCleanup=null;'),source.indexOf('async function execute('));
 for(const afterImageGate of [false,true]){
  let pause;const writes=[],events=[],actions=[];
  const context=vm.createContext({WD:'http://local',sid:'existing',QUERY:'knife set',PROGRESS:'progress',EVENTS:'events',scrollingCollectionStarted:afterImageGate,
   createBrowserControlClient:config=>{pause=config.onTimeout;return()=>{};},
   fs:{readFile:async()=>JSON.stringify({unique_goods:289,round:7,status:'KEYWORD_COMPLETE',sales_sort:{state:'UI_CONFIRMED'},completion_basis:'OLD_COMPLETE',completion_scope:'KEYWORD',coverage_claim:'OLD_COMPLETE',stop_reason:'NO_MORE_RESULTS',updated_at:'old'}),appendFile:async(file,text)=>events.push(JSON.parse(text))},
   atomicSnapshot:async(file,text)=>writes.push({file,value:JSON.parse(text)}),now:()=> 'now',setTimeout,clearTimeout,
   process:{exit:code=>{actions.push(['exit',code]);throw new Error('test exit');}},cleanup:async()=>actions.push(['ownedInterceptCleanup'])});
  vm.runInContext(code,context);if(afterImageGate)vm.runInContext('browserControlCleanup=cleanup',context);
  await assert.rejects(pause(new BrowserControlError('TIMEOUT','POST','/url')),/test exit/);
  assert.equal(writes.length,1);assert.equal(writes[0].file,'progress');
  const row=writes[0].value;assert.equal(row.unique_goods,289);assert.equal(row.round,7);assert.equal(row.sales_sort.state,'UI_CONFIRMED');
  assert.equal(row.status,'STOPPED_BROWSER_COMMAND_TIMEOUT');assert.equal(row.command_execution_unknown,true);
  assert.equal(row.updated_at,'now');assert.equal(row.stop_reason,'BROWSER_COMMAND_TIMEOUT');
  assert.equal(row.completion_basis,null);assert.equal(row.completion_scope,null);assert.equal(row.coverage_claim,null);
  assert.equal(row.keyword_complete,false);assert.equal(row.platform_complete,false);
  assert.equal(row.scroll_collection_started,afterImageGate);assert.equal(row.collection_phase,afterImageGate?'SCROLLING':'BEFORE_SCROLL');
  assert.equal(row.manual_action_required,true);assert.equal(row.automatic_retry,false);assert.equal(row.automatic_recovery_eligible,false);assert.equal(row.resume_requires_session_rotation,false);
  assert.equal(row.browser_preserved,true);assert.equal(row.proxy_preserved,true);
  assert.deepEqual(actions,afterImageGate?[['ownedInterceptCleanup'],['exit',2]]:[['exit',2]]);assert.equal(events[0].type,row.status);
 }
});

test('timeout labels identify a fixed step without storing scripts or private text',async()=>{
 for(const [label,expected] of [['initial_scan','initial_scan'],['script with credential=private',null]]){
  let observed;
  const request=createBrowserControlClient({base:'http://local',session:'s',commandTimeoutMs:5,
   fetchImpl:()=>new Promise(()=>{}),onTimeout:async error=>{observed=error.commandLabel;}});
  await assert.rejects(request('POST','/execute/sync',{script:'private script'},{commandLabel:label}),e=>e.kind==='TIMEOUT');
  assert.equal(observed,expected);
 }
});
