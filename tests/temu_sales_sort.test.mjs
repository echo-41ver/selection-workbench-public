import test from 'node:test';
import assert from 'node:assert/strict';
import {inspectSalesSort, confirmsSalesSort, selectSalesSort, prepareSalesCheckpoint, clickSalesSortControl} from '../scripts/temu_sales_sort.mjs';
import {classifyPageState} from '../scripts/temu_flower_recovery_policy.mjs';
import fs from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import vm from 'node:vm';

const url='https://www.temu.com/search_result.html?search_key=power%20drill';
const selected={url,selected_label:'Top sales',selected_by:'combobox_value',goods_ids:['12','34'],loading:false};
const run=(inspect, click=async()=>{}, guard=async()=>{}) => selectSalesSort({
  inspect,click,guard,query:'power drill',sleep:async()=>{},maxChecks:8});

test('verification return waits for selected sort to render without clicking again',async()=>{
  let reads=0;
  const result=await run(async()=>++reads<3 ? {...selected,loading:true,selected_label:null} : selected,
    async()=>assert.fail('restored selected sort must not be clicked'));
  assert.equal(result.state,'UI_CONFIRMED');assert.ok(reads>=5);
});

test('verification reset to relevance reselects the requested sort before confirmation',async()=>{
  let page={url,menu:'menu'},actions=[];
  const result=await run(async()=>page,async el=>{
    actions.push(el);page=el==='menu'?{url,option:'sales'}:selected;
  });
  assert.equal(result.state,'UI_CONFIRMED');assert.deepEqual(actions,['menu','sales']);
});

test('reconfirmation propagates a login or verification guard stop without interaction',async()=>{
  await assert.rejects(run(async()=>selected,async()=>assert.fail('guarded page click'),
    async()=>{throw new Error('LOGIN_REQUIRED');}),/LOGIN_REQUIRED/);
});

test('open once, select once, require stable selected control and goods',async()=>{
  let page={url,menu:'menu'}, clicks=[], reads=0;
  const result=await run(async()=>{reads++;return page;},async(el)=>{
    clicks.push(el); page=el==='menu'?{url,option:'sales'}:{...selected};
  });
  assert.deepEqual(clicks,['menu','sales']);assert.equal(result.state,'UI_CONFIRMED');
  assert.ok(reads>=5);assert.equal(result.confirmed_by,'combobox_value');
});
test('selected state without goods or with loading is not success',async()=>{
  for(const delta of [{goods_ids:[]},{loading:true},{selected_by:null}]){
    assert.equal((await run(async()=>({...selected,...delta}))).state,'UNCONFIRMED');
  }
});
test('never interact with another query, login page or another host',async()=>{
  for(const wrong of ['https://www.temu.com/login.html',url.replace('power%20drill','curtain'),url.replace('temu.com','example.invalid')]){
    const state={...selected,url:wrong,selected_label:null,option:'sales'};
    assert.equal(confirmsSalesSort(state,'power drill'),false);
    assert.equal((await run(async()=>state,async()=>assert.fail('wrong-page click'))).state,'UNCONFIRMED');
  }
});
test('loading results never trigger another sort action',async()=>{
  let reads=0,clicks=0;const events=[];
  const result=await selectSalesSort({query:'power drill',maxChecks:12,sleep:async()=>{},guard:async()=>{},
    inspect:async()=>{reads++;return clicks?{...selected}: {url,option:'sales',loading:reads<=4};},
    click:async()=>{assert.ok(reads>4,'wait for loading to finish');clicks++;},
    onAction:async action=>events.push(action)});
  assert.equal(result.state,'UI_CONFIRMED');assert.equal(clicks,1);
  assert.equal(events.length,1);assert.equal(events[0].loading,false);
});

test('missing or ambiguous controls are not clicked; ineffective clicks are retried with a limit',async()=>{
  for(const delta of [{},{ambiguous:true,option:'sales'}]){
    assert.equal((await run(async()=>({url,...delta}),async()=>assert.fail('no unambiguous control'))).state,'UNCONFIRMED');
  }
  let clicks=0;
  assert.equal((await run(async()=>({url,option:'sales'}),async()=>clicks++)).state,'UNCONFIRMED');
  assert.equal(clicks,3);
});

test('stale first click is relocated and retried before collection is allowed',async()=>{
  let attempts=0,reads=0,events=[];
  const result=await selectSalesSort({query:'power drill',maxChecks:12,sleep:async()=>{},guard:async()=>{},
    inspect:async()=>{reads++;return attempts<2?{url,option:{fresh:reads}}:{...selected};},
    click:async element=>{assert.equal(element.fresh,reads);if(++attempts===1)throw Error('stale element reference');},
    onRetry:async event=>events.push(event)});
  assert.equal(result.state,'UI_CONFIRMED');assert.equal(attempts,2);
  assert.ok(events.some(event=>event.attempt===2));
});

test('a click that silently does nothing is retried and verified',async()=>{
  let attempts=0;
  const result=await run(async()=>attempts<2?{url,option:'sales'}:{...selected},async()=>{attempts++;});
  assert.equal(attempts,2);assert.equal(result.state,'UI_CONFIRMED');
});

test('transient inspection errors recover but a verification guard is never swallowed',async()=>{
  let reads=0;
  const result=await run(async()=>{if(++reads===1)throw Error('navigation in progress');return {...selected};});
  assert.equal(result.state,'UI_CONFIRMED');
  const stop=Error('WAITING_CAPTCHA');let guarded=0,clicks=0;
  await assert.rejects(()=>run(async()=>({url,option:'sales'}),async()=>{clicks++;},async()=>{if(++guarded===2)throw stop;}),error=>error===stop);
  assert.equal(clicks,1);
});

test('an expanded empty menu is not toggled closed during recovery',async()=>{
  const result=await run(async()=>({url,menu:'open-menu',menu_expanded:true}),async()=>assert.fail('must wait for the menu options'));
  assert.equal(result.state,'UNCONFIRMED');
});
test('unstable result lists do not pass and existing stable sorting is not clicked again',async()=>{
  let index=0;
  assert.equal((await run(async()=>({...selected,goods_ids:[String(index++)]}))).state,'UNCONFIRMED');
  assert.equal((await run(async()=>selected,async()=>assert.fail('already selected'))).state,'UI_CONFIRMED');
});
test('guard stops interactions and interrupted CAPTCHA can resume same control',async()=>{
  const stop=new Error('LOGIN_REQUIRED');
  await assert.rejects(()=>run(async()=>assert.fail('must guard first'),undefined,async()=>{throw stop;}),err=>err===stop);
  let guarded=0;
  const result=await run(async()=>selected,undefined,async()=>{guarded++;await Promise.resolve();});
  assert.equal(guarded,3);assert.equal(result.state,'UI_CONFIRMED');
});

// DOM adapter fixtures exercise semantic recognition without a real browser,
// website, proxy or guessed class names. They are not live TEMU acceptance.
function node(text,attrs={},extra={}){
  return {tagName:'BUTTON',innerText:text,textContent:text,disabled:false,
    getAttribute:key=>attrs[key]??null,hasAttribute:key=>key in attrs,
    closest:()=>null,getBoundingClientRect:()=>({width:100,height:30}),...extra};
}
function inspect(nodes,sort='sales'){
  globalThis.location={href:url};globalThis.getComputedStyle=()=>({display:'block',visibility:'visible'});
  globalThis.document={querySelectorAll:selector=>selector==='a[href]'?[{href:'https://www.temu.com/item-g-12.html'}]:selector.includes('aria-busy')?[]:nodes};
  return inspectSalesSort(sort);
}

test('localized sort controls include Simplified and Traditional Chinese',()=>{
 for(const label of ['排序：销量最高','排序：銷量最高','排序：销量从高到低'])
  assert.equal(inspect([node(label)]).selected_label,label);
 for(const label of ['排序：最近上架','排序：最新到貨','排序：上新時間'])
  assert.equal(inspect([node(label)],'recent').selected_label,label);
 for(const label of ['排序：默认','排序：默認','排序：綜合']){
  const value=inspect([node(label)]);assert.ok(value.menu);assert.equal(value.selected_label,null);
 }
});

test('recent pass switches from Top sales using the real Most recent label',async()=>{
  let page=0;const clicks=[];
  const salesTrigger=node('Sort by: Top sales');
  const recentOption=node('Most recent',{'role':'option'});
  const recentTrigger=node('Sort by: Most recent');
  const result=await selectSalesSort({query:'power drill',sort:'recent',guard:async()=>{},sleep:async()=>{},
    inspect:async()=>inspect(page===0?[salesTrigger]:page===1?[salesTrigger,recentOption]:[recentTrigger],'recent'),
    click:async element=>{clicks.push(element);page++;}});
  assert.equal(result.requested,'recent');assert.equal(result.state,'UI_CONFIRMED');
  assert.deepEqual(clicks,[salesTrigger,recentOption]);
  assert.equal(inspect([recentTrigger],'sales').selected_label,null);
  assert.equal(inspect([salesTrigger],'recent').selected_label,null);
  assert.equal(inspect([recentTrigger],'sales').menu,recentTrigger);
});

test('sort click positions the control before a normal WebDriver click',async()=>{
  const calls=[];let positioned=false;
  const element={isConnected:true,scrollIntoView:options=>{assert.equal(options.behavior,'instant');positioned=true;calls.push('position');}};
  await clickSalesSortControl({element,
    execute:async (source,args)=>vm.runInNewContext(`(function(){${source}})(...args)`,{args}),
    sleep:async()=>calls.push('settle'),
    click:async current=>{assert.equal(current,element);assert.equal(positioned,true);calls.push('click');}});
  assert.deepEqual(calls,['position','settle','click']);
  await assert.rejects(()=>clickSalesSortControl({element:{isConnected:false},
    execute:async(source,args)=>vm.runInNewContext(`(function(){${source}})(...args)`,{args}),
    sleep:async()=>{},click:async()=>assert.fail('detached control')}),/SORT_CONTROL_DETACHED/);
});

test('collector retries unconfirmed sorting from the top before requiring intervention',async()=>{
  const source=await fs.readFile(new URL('../scripts/temu_flower_collect.mjs',import.meta.url),'utf8');
  const preparation=source.slice(source.indexOf('async function prepareSalesSort()'),source.indexOf('try { await waitForInitialProducts(); }'));
  let tries=0,repositions=0;const snapshots=[];
  const context=vm.createContext({pendingRecovery:null,samplingState:()=>({reduced:false}),QUERY:'power drill',SORT_REQUEST:'recent',salesSort:{},salesSortEstablished:false,
    finalStatus:null,sleep:async()=>{},bootstrapPageState:async()=>({}),guardInitialProducts:async()=>{},classify:()=>null,
    readSalesSort:async()=>{},selectSalesSort:async()=>({state:++tries===2?'UI_CONFIRMED':'UNCONFIRMED'}),
    writeSnapshot:async status=>snapshots.push(status),appendEvent:async()=>{},
    execute:async source=>{assert.ok(source.includes('window.scrollTo'));repositions++;}});
  vm.runInContext('class InitialProductsStopped extends Error {}\n'+preparation,context);
  await vm.runInContext('prepareSalesSort()',context);
  assert.equal(tries,2);assert.equal(repositions,1);assert.equal(context.salesSortEstablished,true);
  assert.ok(snapshots.includes('RETRYING_SALES_SORT'));
  assert.ok(!snapshots.includes('STOPPED_SALES_SORT_UNCONFIRMED'));
});

test('recent checkpoint cannot inherit sales quota or discard confirmed recent rows',async()=>{
  const dir=await fs.mkdtemp(path.join(os.tmpdir(),'temu-recent-'));
  try {
    const evidence=sort=>({requested:sort,state:'UI_CONFIRMED',query:'power drill'});
    await fs.writeFile(path.join(dir,'products.jsonl'),[
      {goods_id:'sales',search_sort:evidence('sales')},{goods_id:'recent',search_sort:evidence('recent')}
    ].map(x=>JSON.stringify(x)).join('\n')+'\n');
    await fs.writeFile(path.join(dir,'progress.json'),JSON.stringify({sales_sort:evidence('sales'),unique_goods:2}));
    const result=await prepareSalesCheckpoint(dir,'power drill','recent');
    assert.equal(result.retained_goods,1);
    assert.equal(JSON.parse(await fs.readFile(path.join(dir,'products.jsonl'),'utf8')).goods_id,'recent');
    const state=JSON.parse(await fs.readFile(path.join(dir,'progress.json'),'utf8'));
    assert.equal(state.sales_sort.requested,'recent');assert.equal(state.keyword_complete,false);
  } finally {await fs.rm(dir,{recursive:true,force:true});}
});
test('DOM mere Top sales text is actionable but never selected evidence',()=>{
  const option=node('Top sales',{'role':'option'});const state=inspect([option]);
  assert.equal(state.option,option);assert.equal(state.selected_label,null);
  assert.equal(confirmsSalesSort(state,'power drill'),false);
});

test('DOM repeated sizing label does not hide the real sort trigger',()=>{
  const trigger=node('Sort by: Relevance\nSort by: Relevance',{'role':'button','aria-haspopup':'true'});
  assert.equal(inspect([trigger]).menu,trigger);
  assert.equal(inspect([trigger]).selected_label,null);
  const chosen=node('Sort by: Top sales\nSort by: Top sales',{'role':'button','aria-haspopup':'true'});
  assert.equal(inspect([chosen]).selected_by,'sort_trigger_label');
  assert.equal(confirmsSalesSort(inspect([chosen]),'power drill'),true);
  const mixed=node('Sort by: Relevance\nSort by: Top sales',{'role':'button'});
  assert.equal(inspect([mixed]).selected_label,null);
});
test('DOM selected attributes, native select and trigger value are evidence',()=>{
  for(const control of [node('Top sales',{'aria-selected':'true'}),node('Top sales',{'role':'combobox'}),
    node('Sort by: Top sales'),node('Sort',{}, {tagName:'SELECT',selectedOptions:[node('销量从高到低')],options:[]})]){
    assert.equal(confirmsSalesSort(inspect([control]),'power drill'),true);
  }
});
test('DOM ignores hidden controls and product-card labels, duplicate sales controls are ambiguous',()=>{
  assert.equal(inspect([node('Top sales',{'aria-selected':'true'}, {getBoundingClientRect:()=>({width:0,height:0})})]).selected_label,null);
  assert.equal(inspect([node('Top sales',{}, {closest:selector=>selector.includes('article')?{}:null})]).option,null);
  assert.equal(inspect([node('Top sales'),node('销量优先')]).ambiguous,true);
});

test('checkpoint archives originals and retains only this query confirmed rows',async()=>{
  const directory=await fs.mkdtemp(path.join(os.tmpdir(),'sales-checkpoint-'));
  try {
    const evidence={requested:'sales',state:'UI_CONFIRMED',query:'power drill'};
    const rows=[{goods_id:'keep',search_sort:evidence},{goods_id:'ordinary'},
      {goods_id:'wrong-query',search_sort:{...evidence,query:'other'}}];
    const original=rows.map(row=>JSON.stringify(row)).join('\n')+'\n';
    await fs.writeFile(path.join(directory,'products.jsonl'),original);
    await fs.writeFile(path.join(directory,'pending_products.jsonl'),original);
    await fs.writeFile(path.join(directory,'progress.json'),JSON.stringify({status:'KEYWORD_LIMIT_REACHED',
      round:20,see_more_clicks:20,consecutive_no_new_rounds:10,unique_goods:3,sales_sort:evidence}));
    const result=await prepareSalesCheckpoint(directory,'power drill');
    assert.equal(result.retained_goods,1);assert.equal(result.removed_goods,2);
    assert.equal(await fs.readFile(path.join(result.archive,'products.jsonl'),'utf8'),original);
    for(const name of ['products.jsonl','pending_products.jsonl']){
      const kept=(await fs.readFile(path.join(directory,name),'utf8')).trim().split('\n').map(JSON.parse);
      assert.deepEqual(kept,[rows[0]]);
    }
    const progress=JSON.parse(await fs.readFile(path.join(directory,'progress.json'),'utf8'));
    assert.equal(progress.keyword_complete,false);assert.equal(progress.round,0);assert.equal(progress.consecutive_no_new_rounds,0);
  } finally {await fs.rm(directory,{recursive:true,force:true});}
});

test('collector pauses with BiDi active until the current query sort is visibly confirmed',async()=>{
  const source=await fs.readFile(new URL('../scripts/temu_flower_collect.mjs',import.meta.url),'utf8');
  const pause=source.slice(source.indexOf('async function stopSalesSort('),source.indexOf('try { await waitForInitialProducts(); }'));
  const snapshots=[],events=[];let checks=0,sortReads=0,closed=0,guards=0;
  const context=vm.createContext({
    phase:'before_collection',QUERY:'power drill',SORT_REQUEST:'sales',salesSort:{requested:'sales',state:'UNCONFIRMED'},
    salesSortEstablished:false,finalStatus:null,initialProductsPreparing:true,httpBlock:null,
    now:()=>new Date().toISOString(),rand:(a)=>a,sleep:async()=>{},
    bootstrapPageState:async()=>({url:'https://www.temu.com/search_result.html?search_key=power%20drill',visibleGoodsCount:3}),
    classify:()=>null,guardInitialProducts:async()=>{guards++;return {manual:false};},
    readSalesSort:async()=>{sortReads++;return sortReads<2?{}:{url:'https://www.temu.com/search_result.html?search_key=power%20drill',selected_label:'Top sales',selected_by:'sort_trigger_label',goods_ids:['a','b','c']};},
    confirmsSalesSort,writeSnapshot:async(status,details)=>snapshots.push({status,...details}),
    appendEvent:async(type,details)=>events.push({type,...details}),bidi:{close:async()=>{closed++;}},
  });
  vm.runInContext(pause,context);
  await vm.runInContext('stopSalesSort(phase)',context);
  assert.equal(context.salesSortEstablished,true);assert.equal(context.finalStatus,null);
  assert.equal(context.initialProductsPreparing,false);assert.equal(sortReads,2);assert.equal(guards,0);
  assert.equal(closed,0);assert.equal(snapshots[0].status,'STOPPED_SALES_SORT_UNCONFIRMED');
  assert.equal(snapshots[0].bidi_preserved,true);
  assert.ok(events.some(event=>event.type==='SALES_SORT_RECOVERED_AFTER_PAUSE'));
});

test('sort pause keeps CAPTCHA guard active and leaves terminal HTTP evidence terminal',async()=>{
  const source=await fs.readFile(new URL('../scripts/temu_flower_collect.mjs',import.meta.url),'utf8');
  const pause=source.slice(source.indexOf('async function stopSalesSort('),source.indexOf('try { await waitForInitialProducts(); }'));
  const snapshots=[];let stateCalls=0,sortReads=0;
  const context=vm.createContext({phase:'initial',QUERY:'power drill',SORT_REQUEST:'sales',salesSort:{},
    salesSortEstablished:false,finalStatus:null,initialProductsPreparing:true,httpBlock:null,now:()=>'',rand:a=>a,
    sleep:async()=>{},bootstrapPageState:async()=>({url:stateCalls++===0?'https://www.temu.com/bgn_verification.html':'https://www.temu.com/search_result.html?search_key=power%20drill'}),
    classify:state=>state.url.includes('verification')?'HUMAN_VERIFICATION':null,
    guardInitialProducts:async()=>({manual:true}),readSalesSort:async()=>{sortReads++;return {url:'https://www.temu.com/search_result.html?search_key=power%20drill',selected_label:'Top sales',selected_by:'sort_trigger_label',goods_ids:['a']};},
    confirmsSalesSort,writeSnapshot:async(status,details)=>snapshots.push({status,...details}),appendEvent:async()=>{},
  });
  vm.runInContext(pause,context);await vm.runInContext('stopSalesSort(phase)',context);
  assert.equal(snapshots.some(row=>row.status==='WAITING_CAPTCHA'),true);assert.equal(sortReads,1);

  const blocked=vm.createContext({...context,
    finalStatus:null,httpBlock:{status:403},
    bootstrapPageState:async()=>({url:'https://www.temu.com/search_result.html?search_key=power%20drill'}),
    guardInitialProducts:async()=>{throw Object.assign(new Error('blocked'),{name:'InitialProductsStopped'});},
    readSalesSort:async()=>assert.fail('must not probe sort after terminal HTTP block'),
  });
  vm.runInContext(pause,blocked);
  await assert.rejects(vm.runInContext('stopSalesSort(phase)',blocked),/blocked/);
});

test('terminal HTTP remains passive until CAPTCHA then requires stable same-query products and sort',async()=>{
  const source=await fs.readFile(new URL('../scripts/temu_flower_collect.mjs',import.meta.url),'utf8');
  const monitor=source.slice(source.indexOf('async function waitForHttpVerificationRecovery('),source.indexOf('try { await waitForInitialProducts(); }'));
  const target={url,selected_label:'Top sales',selected_by:'combobox_value',goods_ids:['12','34'],loading:false};
  const makeContext=({challenge=false,wrongQuery=false}={})=>{
    const events=[],snapshots=[];let reads=0,sleeps=0,scans=0,sortReads=0;
    const context=vm.createContext({status:'STOPPED_HTTP_403',originalBlock:null,httpBlock:{status:403,observed_at:'first'},
      QUERY:'power drill',SORT_REQUEST:'sales',humanVerificationActive:false,stableChecks:0,previousSignature:null,
      URL,
      sleep:async()=>{sleeps++;if((!challenge&&sleeps>1)||(challenge&&sleeps>30))throw Error('test-stop');},
      bootstrapPageState:async()=>{reads++;if(reads>8)throw Error('test-stop');return {url:challenge&&reads===1?'https://www.temu.com/bgn_verification.html':url};},
      classify:state=>String(state.url||'').includes('verification')?'HUMAN_VERIFICATION':null,
      scan:async()=>{scans++;return {url:wrongQuery?url.replace('power%20drill','other'):url,rows:[{goods_id:'12'}]};},
      readSalesSort:async()=>{sortReads++;return target;},confirmsSalesSort:()=>true,
      appendEvent:async(type,details)=>events.push({type,...details}),
      writeSnapshot:async(status,details)=>snapshots.push({status,...details})});
    return {context,events,snapshots,get counts(){return {reads,scans,sortReads,sleeps};}};
  };
  const passive=makeContext();
  await assert.rejects(vm.runInContext('(async()=>{'+monitor+';return waitForHttpVerificationRecovery("STOPPED_HTTP_403")})()',passive.context),/test-stop/);
  assert.equal(passive.context.humanVerificationActive,false);assert.equal(passive.counts.scans,0);assert.equal(passive.counts.sortReads,0);
  assert.ok(passive.events.some(event=>event.type==='TERMINAL_HTTP_PASSIVE_PAGE_CHECK'));

  const wrong=makeContext({challenge:true,wrongQuery:true});
  await assert.rejects(vm.runInContext('(async()=>{'+monitor+';return waitForHttpVerificationRecovery("STOPPED_HTTP_403")})()',wrong.context),/test-stop/);
  assert.equal(wrong.context.humanVerificationActive,false);assert.equal(wrong.snapshots.some(row=>row.sort_confirmed===true),false);

  const recovered=makeContext({challenge:true});
  const result=await vm.runInContext('(async()=>{'+monitor+';return waitForHttpVerificationRecovery("STOPPED_HTTP_403")})()',recovered.context);
  assert.equal(result.sort.selected_label,'Top sales');assert.equal(recovered.context.humanVerificationActive,false);
  assert.equal(recovered.counts.scans,3);assert.equal(recovered.counts.sortReads,3);
  assert.equal(recovered.snapshots[0].bidi_preserved,true);
  assert.ok(recovered.events.some(event=>event.type==='HTTP_TERMINAL_VERIFICATION_OBSERVED'));
  assert.ok(recovered.snapshots.some(row=>row.status==='WAITING_HUMAN_VERIFICATION'&&row.challenge_resources_allowed));
});

test('login and verification are not overwritten by missing sorting controls during scans',async()=>{
  const source=await fs.readFile(new URL('../scripts/temu_flower_collect.mjs',import.meta.url),'utf8');
  const scanCode=source.slice(source.indexOf('async function scan('),source.indexOf('async function lightPageState()'));
  for(const [page,expected] of [
    [{url:'https://www.temu.com/login.html',rows:[]},'LOGIN_REQUIRED'],
    [{url:'https://www.temu.com/bgn_verification.html',rows:[]},'HUMAN_VERIFICATION'],
    [{documentURI:'about:neterror',rows:[]},'PROXY_OR_NETWORK_FAILURE'],
  ])for(const navigationRace of [false,true]){
    let sortReads=0;
    const context=vm.createContext({pendingRecovery:null,samplingState:()=>({reduced:false}),SORT_REQUEST:'recent',QUERY:'power drill',initialProductsPreparing:false,
      products:new Map([['saved',{main_image_url:'https://fixture/image.jpg'}]]),pendingProducts:new Map(),unclassifiedProducts:new Map(),
      now:()=>'',extractScript:'fixture',salesSortEstablished:true,salesSort:{},classify:classifyPageState,
      execute:async()=>navigationRace?{url,rows:[]}:page,
      readSalesSort:async()=>{sortReads++;return {};},confirmsSalesSort,
      bootstrapPageState:async()=>page,
      stopSalesSort:async()=>assert.fail('must preserve the actual interruption for recovery')});
    vm.runInContext(scanCode,context);
    const result=await vm.runInContext('scan()',context);
    assert.equal(classifyPageState(result),expected);
    assert.equal(sortReads,navigationRace?1:0);
    assert.equal(context.products.size,1);
  }
});
