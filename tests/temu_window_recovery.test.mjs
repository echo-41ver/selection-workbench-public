import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';
const source=fs.readFileSync(new URL('../scripts/temu_flower_collect.mjs',import.meta.url),'utf8');
const helper=source.slice(source.indexOf('// BEGIN_INITIAL_WINDOW_SELECTION'),source.indexOf('// END_INITIAL_WINDOW_SELECTION'));
function fixture(tabs){
  const calls=[];let current=Object.keys(tabs)[0];
  const context=vm.createContext({URL,Object,Array,String,Boolean,Error,Set});
  vm.runInContext(helper,context);
  const request=async(method,route,body)=>{
    calls.push({method,route,body});
    if(method==='POST'&&route==='/window'){assert.ok(Object.hasOwn(tabs,body.handle));current=body.handle;return null;}
    if(method==='GET'&&route==='/url')return tabs[current];
    throw Error('Unexpected control operation');
  };
  return {select:(query='curtain',handles=Object.keys(tabs),options={})=>context.selectInitialWindow(handles,request,query,options),calls,current:()=>current};
}
for(const reverse of [false,true])test(`two tabs: original empty search is reused, auxiliary order ${reverse}`,async()=>{
  const tabs=reverse?{aux:'https://passport.geekbi.com/login',temu:'https://www.temu.com/search_result.html'}:{temu:'https://www.temu.com/search_result.html',aux:'https://passport.geekbi.com/login'};
  const f=fixture(tabs),selected=await f.select();
  assert.equal(selected.chosen,'temu');assert.equal(selected.bootstrapFromSafeTab,false);assert.equal(selected.recoveredExistingTemuTab,true);assert.equal(f.current(),'temu');
  assert.ok(f.calls.every(c=>c.route==='/window'||c.route==='/url'));
  assert.ok(f.calls.filter(c=>c.method==='POST').every(c=>c.route==='/window'&&Object.keys(c.body).join()==='handle'));
});
for(const second of ['https://www.temu.com/search_result.html','https://temu.com/login.html','https://www.temu.com/','http://www.temu.com/search_result.html'])test(`ambiguous TEMU context refuses ${new URL(second).pathname}`,async()=>{
  const f=fixture({one:'https://www.temu.com/search_result.html',two:second});
  await assert.rejects(f.select(),e=>e.code==='WINDOW_SELECTION_NOT_FOUND');
});
for(const rejected of ['https://passport.geekbi.com/login','https://evil.temu.com/search_result.html','http://www.temu.com/search_result.html','https://www.temu.com/bgn_verification.html','https://www.temu.com/search_result.html/extra','https://user:secret@www.temu.com/search_result.html','https://www.temu.com:444/search_result.html','https://www.temu.com/search_result.html?search_key=&search_key=other','https://www.temu.com/search_result.html?search_key=&search_key='])test(`auxiliary tab cannot turn invalid page into recovered empty search ${new URL(rejected).hostname+new URL(rejected).pathname}`,async()=>{
  const f=fixture({one:rejected,two:'https://passport.geekbi.com/login'});
  await assert.rejects(f.select(),e=>e.code==='WINDOW_SELECTION_NOT_FOUND');
});
for(const initial of ['about:blank','about:newtab','http://127.0.0.1:17892/','https://example.com/','https://www.temu.com/','https://www.temu.com/search_result.html'])test(`original single-tab bootstrap preserved ${initial}`,async()=>{
  const f=fixture({one:initial}),selected=await f.select();assert.equal(selected.chosen,'one');assert.equal(selected.bootstrapFromSafeTab,true);assert.equal(selected.recoveredExistingTemuTab,false);
});
test('explicit HTTPS default port remains a valid empty recovery page',async()=>{
  const f=fixture({temu:'https://www.temu.com:443/search_result.html',aux:'https://passport.geekbi.com/login'}),selected=await f.select();
  assert.equal(selected.chosen,'temu');assert.equal(selected.bootstrapFromSafeTab,false);assert.equal(selected.recoveredExistingTemuTab,true);
});
test('original matching-query selection remains a reused current document',async()=>{
  const f=fixture({aux:'https://passport.geekbi.com/login',target:'https://www.temu.com/search_result.html?search_key=CURTAIN'}),selected=await f.select();
  assert.equal(selected.chosen,'target');assert.equal(selected.bootstrapFromSafeTab,false);assert.equal(selected.recoveredExistingTemuTab,false);
});
for(const bad of [[],null,[''],['one',null],['one','one'],['x'.repeat(513)],Array.from({length:9},(_,i)=>`h${i}`)])test(`malformed or excessive handle list fails before browser operations ${JSON.stringify(bad)}`,async()=>{
  const f=fixture({one:'https://www.temu.com/search_result.html'});await assert.rejects(f.select('curtain',bad),e=>e.code==='WINDOW_SELECTION_HANDLES');assert.equal(f.calls.length,0);
});
test('selected handle closure/driver error is propagated without fallback or new session',async()=>{
  const context=vm.createContext({URL,Object,Array,String,Boolean,Error,Set});vm.runInContext(helper,context);let calls=0;
  await assert.rejects(context.selectInitialWindow(['temu','aux'],async()=>{calls++;throw Error('closed');},'curtain'),/closed/);assert.equal(calls,1);
});
test('initial request budget refuses another command after deadline and keeps normal client timeout separate',async()=>{
  const code=source.slice(source.indexOf('const initialWindowClient='),source.indexOf('async function execute('));
  let now=0,calls=0,options;
  const context=vm.createContext({Date:{now:()=>now},WD:'http://127.0.0.1:4444',sid:'test-only',recordBrowserControlTimeout:()=>{},Object,Error,
    createBrowserControlClient:o=>{options=o;return async()=>{calls++;now+=5000;return null;};}});
  vm.runInContext(code+';globalThis.request=initialWd;',context);
  for(let i=0;i<3;i++)await context.request('GET','/url');
  await assert.rejects(context.request('GET','/url'),e=>e.code==='WINDOW_SELECTION_DEADLINE');assert.equal(calls,3);assert.equal(options.commandTimeoutMs,5000);
  assert.ok(source.includes('const wd=createBrowserControlClient({base:WD,session:sid,onTimeout:recordBrowserControlTimeout});'));
  assert.ok(source.includes('const handles = await readInitialWindowHandles(initialWd,'));
});
test('recovered empty document takes existing strict guard then navigate:false path',async()=>{
  const code=source.slice(source.indexOf('async function waitForInitialProducts()'),source.indexOf('async function prepareSalesSort()'));
  for(const gate of [null,'HUMAN_VERIFICATION','LOGIN_REQUIRED','ACCESS_DENIED','PROXY_OR_NETWORK_FAILURE']){
    const actions=[],events=[];const page={url:'https://www.temu.com/search_result.html',gate};
    const context=vm.createContext({URL,Date,Number,Boolean,QUERY:'curtain',ENTRY_MODE:'empty_search',bootstrapFromSafeTab:false,recoveredPreviousSearchTab:false,bidiStatus:'ACTIVE',
      bootstrapPageState:async()=>page,classify:s=>s.gate,guardInitialProducts:async s=>{actions.push('guard');if(s.gate)throw Error('gate');return{manual:false};},
      isPostVerificationEmptySearchSample:s=>s.url==='https://www.temu.com/search_result.html',execute:async()=>actions.push('scroll'),
      bootstrapThroughEmptySearch:async opts=>actions.push(opts),appendEvent:async type=>events.push(type),
      readLocalWarehouse:async()=>({}),ensurePageGuards:async()=>{},scan:async()=>({url:'https://www.temu.com/search_result.html?search_key=curtain',visibleGoodsCount:1}),
      sleep:async()=>{},rand:()=>0});
    if(gate){await assert.rejects(vm.runInContext(code+';waitForInitialProducts()',context),/gate/);assert.deepEqual(actions,['guard']);}
    else{await vm.runInContext(code+';waitForInitialProducts()',context);assert.equal(actions[0],'guard');assert.equal(actions[2].navigate,false);assert.equal(actions[2].reason,'POST_VERIFICATION_EMPTY_SEARCH');assert.ok(events.includes('RESTORED_EMPTY_SEARCH_RESUBMIT'));assert.ok(!events.includes('BOOTSTRAP_ENTRY_SELECTED'));}
  }
});

// The following fixtures exercise only pure extracted functions; the collector
// CLI, browser transport, filesystem checkpoints and paid providers never run.
const policySource=fs.readFileSync(new URL('../scripts/temu_search_bootstrap_policy.mjs',import.meta.url),'utf8');
function policyFunction(name){
  const begin=policySource.indexOf(`export function ${name}(`);
  const end=policySource.indexOf('\nexport function ',begin+1);
  return policySource.slice(begin,end<0?undefined:end).replace('export function ','function ');
}
const entryPolicy=policyFunction('normalizeSearchText')+'\n'+policyFunction('bootstrapEntryPlan');
const previousUrl='https://www.temu.com/search_result.html?search_key=clear%20shower%20curtain';
const targetUrl='https://www.temu.com/search_result.html?search_key=shower%20curtain';
for(const reverse of [false,true])test(`unique previous-query search plus auxiliary uses existing document ${reverse}`,async()=>{
  const tabs=reverse?{aux:'https://passport.geekbi.com/login',temu:previousUrl}:{temu:previousUrl,aux:'https://passport.geekbi.com/login'};
  const f=fixture(tabs),diagnostics=[],selected=await f.select('shower curtain',Object.keys(tabs),{onDiagnostic:d=>diagnostics.push(d)});
  assert.equal(selected.chosen,'temu');assert.equal(selected.bootstrapFromSafeTab,false);assert.equal(selected.recoveredPreviousSearchTab,true);
  assert.equal(selected.recoveredExistingTemuTab,false);assert.equal(f.current(),'temu');assert.equal(f.calls.length,5);
  assert.ok(f.calls.every(c=>c.route==='/window'||c.route==='/url'));
  assert.equal(diagnostics.length,1);assert.equal(diagnostics[0].mode,'PREVIOUS_SEARCH');assert.equal(diagnostics[0].temu_tab_count,1);
  assert.equal(diagnostics[0].previous_search_count,1);
  assert.deepEqual(Object.keys(diagnostics[0]).sort(),['empty_search_count','handle_count','mode','previous_search_count','stage','temu_tab_count']);
  assert.ok(!JSON.stringify(diagnostics).includes('clear'));assert.ok(!JSON.stringify(diagnostics).includes('https'));assert.ok(!JSON.stringify(diagnostics).includes('temu:'));
});
for(const other of ['https://www.temu.com/search_result.html?search_key=another','https://temu.com/login.html','http://www.temu.com/search_result.html?search_key=other','https://www.temu.com/bgn_verification.html','https://www.temu.com/'])test(`previous-query fallback rejects another TEMU document ${new URL(other).protocol} ${new URL(other).pathname}`,async()=>{
  const f=fixture({previous:previousUrl,other,aux:'https://passport.geekbi.com/login'}),diagnostics=[];
  await assert.rejects(f.select('shower curtain',['previous','other','aux'],{onDiagnostic:d=>diagnostics.push(d)}),e=>e.code==='WINDOW_SELECTION_NOT_FOUND');
  assert.equal(diagnostics[0].mode,'REJECTED');assert.equal(diagnostics[0].temu_tab_count,2);assert.equal(f.calls.length,6);
});
for(const bad of ['http://www.temu.com/search_result.html?search_key=old','https://evil.temu.com/search_result.html?search_key=old','https://www.temu.com:444/search_result.html?search_key=old','https://user:secret@www.temu.com/search_result.html?search_key=old','https://www.temu.com/search_result.html/extra?search_key=old','https://www.temu.com/search_result.html?search_key=old&search_key=second','https://www.temu.com/login.html?search_key=old','https://www.temu.com/bgn_verification.html?search_key=old'])test(`previous-query fallback refuses non-strict search ${new URL(bad).hostname} ${new URL(bad).pathname}`,async()=>{
  const f=fixture({old:bad,aux:'https://passport.geekbi.com/login'});await assert.rejects(f.select('shower curtain'),e=>e.code==='WINDOW_SELECTION_NOT_FOUND');
});
test('previous-query scan stops after a command failure without another switch',async()=>{
  const context=vm.createContext({URL,Object,Array,String,Boolean,Error,Set});vm.runInContext(helper,context);const calls=[];
  await assert.rejects(context.selectInitialWindow(['previous','aux'],async(method,route)=>{calls.push([method,route]);if(calls.length===2)throw Object.assign(Error('control timeout'),{code:'TIMEOUT'});},'shower curtain'),e=>e.code==='TIMEOUT');
  assert.deepEqual(calls,[['POST','/window'],['GET','/url']]);
});
function recoveredContext({url=previousUrl,gate=null,bootstrap=null,plan=null}={}){
  const actions=[],events=[];let page={url,gate,visibleGoodsCount:5};
  const context=vm.createContext({URL,Date,Number,Boolean,Object,Array,String,Error,Set,
    QUERY:'shower curtain',ENTRY_MODE:'direct_search',bootstrapFromSafeTab:false,recoveredPreviousSearchTab:true,bidiStatus:'ACTIVE',
    bootstrapPageState:async()=>page,classify:s=>s.gate,
    guardInitialProducts:async(s,phase)=>{actions.push(['guard',phase]);if(s.gate)throw Error('gate');return{manual:false};},
    isPostVerificationEmptySearchSample:()=>false,
    execute:async()=>{actions.push(['scroll']);},
    bootstrapThroughEmptySearch:async opts=>{actions.push(['submit',opts]);if(bootstrap)await bootstrap(opts);page={url:targetUrl,visibleGoodsCount:1};},
    appendEvent:async(type,data)=>events.push({type,data}),readLocalWarehouse:async()=>({}),ensurePageGuards:async()=>{},
    scan:async()=>{actions.push(['scan']);assert.equal(page.url,targetUrl,'old-query goods must not reach scan');return page;},
    sleep:async()=>{},rand:()=>0});
  vm.runInContext(helper+'\n'+entryPolicy,context);
  if(plan)context.bootstrapEntryPlan=()=>plan;
  const code=source.slice(source.indexOf('async function waitForInitialProducts()'),source.indexOf('async function prepareSalesSort()'));
  return{run:()=>vm.runInContext(code+';waitForInitialProducts()',context),actions,events,context};
}
test('previous-query recovery awaits existing CURRENT_SEARCH_INPUT before any collection scan',async()=>{
  let release,submitted;const submittedPromise=new Promise(resolve=>submitted=resolve);
  const f=recoveredContext({bootstrap:async()=>{submitted();await new Promise(resolve=>release=resolve);}});
  const done=f.run();await Promise.race([submittedPromise,done]);
  assert.equal(f.actions[0][0],'guard');assert.ok(!f.actions.some(a=>a[0]==='scan'));
  const opts=f.actions.find(a=>a[0]==='submit')[1];assert.equal(opts.navigate,false);assert.equal(opts.reuseSearchPage,true);assert.equal(opts.reason,'NEXT_KEYWORD_CURRENT_PAGE');
  release();await done;assert.equal(f.actions.at(-1)[1],'initial_load');
  const entry=f.events.find(e=>e.type==='NEXT_KEYWORD_CURRENT_SEARCH_ENTRY');
  assert.deepEqual(Object.keys(entry.data).sort(),['navigation_performed','plan','stage']);assert.equal(entry.data.plan,'CURRENT_SEARCH_INPUT');
  assert.ok(!JSON.stringify(entry).includes('clear'));assert.ok(!JSON.stringify(entry).includes('https:'));
});
for(const gate of ['HUMAN_VERIFICATION','LOGIN_REQUIRED','ACCESS_DENIED','PROXY_OR_NETWORK_FAILURE'])test(`previous-query recovery keeps original guard for ${gate}`,async()=>{
  const f=recoveredContext({gate});await assert.rejects(f.run(),/gate/);assert.ok(f.actions.every(a=>a[0]==='guard'));
});
for(const changed of ['https://passport.geekbi.com/login','https://www.temu.com/login.html','https://www.temu.com/search_result.html','http://www.temu.com/search_result.html?search_key=old','https://www.temu.com/search_result.html?search_key=old&search_key=other','https://user:secret@www.temu.com/search_result.html?search_key=old'])test(`changed previous-query context rejects before submission ${new URL(changed).hostname} ${new URL(changed).pathname}`,async()=>{
  const f=recoveredContext({url:changed});await assert.rejects(f.run(),e=>e.code==='WINDOW_SELECTION_CONTEXT_CHANGED');assert.ok(f.actions.every(a=>a[0]==='guard'));
});
test('previous-query mode cannot fall through to direct/home navigation even if plan changes',async()=>{
  const f=recoveredContext({plan:'direct_search'});await assert.rejects(f.run(),e=>e.code==='WINDOW_SELECTION_CONTEXT_CHANGED');assert.ok(f.actions.every(a=>a[0]==='guard'));
});
test('submission timeout propagates and no product scan follows it',async()=>{
  const f=recoveredContext({bootstrap:async()=>{throw Object.assign(Error('timeout'),{code:'TIMEOUT'});}});
  await assert.rejects(f.run(),e=>e.code==='TIMEOUT');assert.ok(!f.actions.some(a=>a[0]==='scan'));
});
test('actual existing bootstrap requires new-query goods to stabilize before scan',async()=>{
  const f=recoveredContext(),calls=[];let submitted=0,readsAfterSubmission=0,targetGoodsChecks=0;
  f.context.bootstrapPageState=async()=>{
    calls.push('read');if(!submitted)return{url:previousUrl,visibleGoodsCount:8};
    readsAfterSubmission++;
    if(readsAfterSubmission===1)return{url:previousUrl,visibleGoodsCount:8};
    if(readsAfterSubmission===2)return{url:targetUrl,visibleGoodsCount:0};
    targetGoodsChecks++;return{url:targetUrl,visibleGoodsCount:2};
  };
  f.context.decideSearchSubmission=()=> 'REPLACE_AND_ENTER';
  f.context.waitForStableEmptySearch=async(_reason,reuse)=>{assert.equal(reuse,true);calls.push('stable-input');return{url:previousUrl};};
  f.context.submitHomeSearch=async()=>{calls.push('submit');submitted++;return{submitted:true};};
  f.context.scan=async()=>{assert.ok(targetGoodsChecks>=3);calls.push('scan');return{url:targetUrl,visibleGoodsCount:2};};
  f.context.sleep=async()=>{if(readsAfterSubmission>10)throw Error('fixture read bound');};
  const bootstrapCode=source.slice(source.indexOf('async function bootstrapThroughEmptySearch('),source.indexOf('async function bootstrapThroughHome()'));
  vm.runInContext(bootstrapCode,f.context);await f.run();
  assert.ok(submitted>=1&&submitted<=2);assert.equal(targetGoodsChecks,3);assert.equal(calls.at(-1),'scan');
  assert.ok(f.events.some(e=>e.type==='TARGET_SEARCH_STABLE'&&e.data.stable_checks===3));
});
