import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';

const html=fs.readFileSync(new URL('../web/index.html',import.meta.url),'utf8');

test('history timeout releases refresh lock and allows the next request',async()=>{
 const nodes=new Map();const get=id=>{if(!nodes.has(id))nodes.set(id,{hidden:true,replaceChildren(){this.textContent='';}});return nodes.get(id);};
 let calls=0;const timers=[];
 const context=vm.createContext({$:get,AbortController,Error,
  setTimeout(callback,delay){timers.push(delay);if(delay===8000)queueMicrotask(callback);return timers.length;},clearTimeout(){},
  fetch:async(_path,{signal})=>{if(++calls===1)return new Promise((_resolve,reject)=>signal.addEventListener('abort',()=>reject(Object.assign(Error('timeout'),{name:'AbortError'}))));return {ok:true,json:async()=>({ok:true,runs:[]})};}});
 vm.runInContext(html.slice(html.indexOf('let historyLoading=false'),html.indexOf('function historyExcelLinks(')),context);
 await vm.runInContext('loadHistory()',context);
 assert.match(get('history-list').textContent,/超时.*自动重试/);
 assert.equal(vm.runInContext('historyLoading',context),false);
 await vm.runInContext('loadHistory()',context);
 assert.equal(calls,2);assert.match(get('history-list').textContent,/还没有历史任务/);
 assert.ok(timers.includes(3000));
});

test('background history loading shows progress and schedules a retry instead of empty history',async()=>{
 const box={replaceChildren(){throw Error('must preserve loading state');}};const timers=[];
 const context=vm.createContext({$:()=>box,AbortController,Error,
  setTimeout(_callback,delay){timers.push(delay);return timers.length;},clearTimeout(){},
  fetch:async()=>({ok:true,json:async()=>({ok:true,history_loading:true})})});
 vm.runInContext(html.slice(html.indexOf('let historyLoading=false'),html.indexOf('function historyExcelLinks(')),context);
 await vm.runInContext('loadHistory()',context);
 assert.match(box.textContent,/正在后台读取历史记录/);assert.ok(timers.includes(2000));
 assert.equal(vm.runInContext('historyLoading',context),false);
});

test('rule application feedback is independent and never overwrites typed requirements',()=>{
 const nodes=new Map();const get=id=>{if(!nodes.has(id))nodes.set(id,{value:'用户尚未应用的输入'});return nodes.get(id);};
 const context=vm.createContext({$:get,applyingScope:'stage3'});
 const fn=html.slice(html.indexOf('function renderPlainApplication('),html.indexOf('function applyLayer('));
 vm.runInContext(fn,context);
 vm.runInContext("renderPlainApplication({stage1:{state:'applied',message:'要求已成功应用。'},stage3:{state:'failed',message:'应用失败：规则结构无效'}})",context);
 assert.match(get('plain-status-stage1').textContent,/第一层.*成功/);
 assert.match(get('plain-status-stage3').textContent,/第三层.*失败.*结构无效/);
 assert.equal(get('plain-status-stage3').className,'notice danger');
 assert.equal(get('plain-rule3').value,'用户尚未应用的输入');
 assert.equal(vm.runInContext('applyingScope',context),null);
});

test('cold page controls accept absent task state before the first status request',()=>{
 const nodes=new Map();const get=id=>{if(!nodes.has(id))nodes.set(id,{style:{},replaceChildren(){},setAttribute(){}});return nodes.get(id);};
 const code=html.slice(html.indexOf('function stage3Button(data)'),html.indexOf('const excelOpening='));
 const context=vm.createContext({$:get,statusFresh:false,busy:false,renderStage3Settings(){},stage3ProviderSettings:()=>undefined});
 vm.runInContext(code,context);
 assert.doesNotThrow(()=>vm.runInContext('stage3Button(null)',context));
 assert.equal(get('start-stage3').disabled,true);
 assert.equal(get('stop-stage3').disabled,true);
 assert.equal(get('codex-review-controls').hidden,true);
 assert.match(get('stage3-count').textContent,/等待读取候选与已处理数量/);
});

test('Codex pool status stays separate from full candidate progress and reports Excel publishing',()=>{
 const nodes=new Map();const get=id=>{if(!nodes.has(id))nodes.set(id,{style:{},replaceChildren(){},setAttribute(k,v){this[k]=v;}});return nodes.get(id);};
 const code=html.slice(html.indexOf('function stage3Button(data)'),html.indexOf('const excelOpening='));
 const context=vm.createContext({$:get,statusFresh:true,busy:false,renderStage3Settings(){},stage3ProviderSettings:()=>undefined});
 vm.runInContext(code,context);
 vm.runInContext(`stage3Button({category_key:'fixture',scope:{execution_mode:'full'},stage3_control:{enabled:false,running:false,can_stop:false,phase:'等待',progress:{total:1986,completed:491,codex_completed:0,pending:1495},codex_review:{available:true,running:true,phase:'持续并行看图',target:768,imported:696,expected_tasks:8,all_reviews_complete:true,excel_current:false,pool:{enabled:true,running:true,workers:6,group_size:12,model:'gpt-6-luna',effort:'high',running_groups:6,pending_pairs:1495,completed_groups:41,phase:'并行看图并自动接续下一组'}}}})`,context);
 assert.match(get('stage3-count').textContent,/已进入第三层 1,986 对 · 已处理 491 对/);
 assert.match(get('codex-review-pool').textContent,/6 组并发 · gpt-6-luna high · 每组 1 个 Amazon＋最多 12 个 TEMU/);
 assert.match(get('codex-review-pool').textContent,/自动接续下一组/);
 assert.match(get('codex-review-target').textContent,/已导入分片结果 696 \/ 768 对/);
 assert.match(get('codex-review-publisher').textContent,/最新 Excel 尚未确认/);
 vm.runInContext(`stage3Button({category_key:'fixture',scope:{execution_mode:'full'},stage3_control:{enabled:false,running:false,can_stop:false,progress:{total:1986,completed:491},codex_review:{available:true,all_reviews_complete:true,excel_current:false,publisher:{phase:'等待发布锁',error:'另一个发布器持锁'}}}})`,context);
 assert.match(get('codex-review-publisher').textContent,/等待发布锁.*另一个发布器持锁/);
});

test('compact worker surfaces its round, state heartbeat time, and deduplicated scope',()=>{
 const nodes=new Map();const get=id=>{if(!nodes.has(id))nodes.set(id,{children:[],append(...values){this.children.push(...values);}});return nodes.get(id);};
 const element=()=>({children:[],style:{},dataset:{},append(...values){this.children.push(...values);},setAttribute(k,v){this[k]=v;},addEventListener(){}});
 const code=html.slice(html.indexOf('function first(row,keys)'),html.indexOf('function addProgressCard('));
 const context=vm.createContext({$:get,document:{createElement:element},busy:false,statusFresh:true,lastStatus:{active:true,category_key:'fixture'},rotationSubmitting:new Set(),rotateSession(){}});
 vm.runInContext(code,context);
 const card=element();card.className='progress-card';
 context.card=card;
 vm.runInContext(`compactWorker(card,{unique_goods:17,round:4,status_updated_at:1791596096},2,false)`,context);
 const activity=card.children.find(child=>child.className==='small worker-activity');
 assert.match(activity.textContent,/第 4 轮/);
 assert.match(activity.textContent,/状态更新 \d{2}:\d{2}:\d{2}/);
 assert.match(activity.textContent,/当前关键词 \/ 排序去重商品/);
 const completedCard=element();completedCard.className='progress-card';context.card=completedCard;
 vm.runInContext(`compactWorker(card,{unique_goods:7159,round:4,status_updated_at:1791596096,'采集状态':'WORKER_COMPLETE'},2,false)`,context);
 const completedActivity=completedCard.children.find(child=>child.className==='small worker-activity');
 assert.match(completedActivity.textContent,/已完成关键词 \/ 排序累计商品/);
});

test('changing the address fragment switches the visible panel without resetting edits',()=>{
 const panels=Object.fromEntries(['create','monitor','results','history'].map(n=>[n+'-panel',{hidden:n!=='monitor'}]));
 const buttons=['create','monitor','results','history'].map(page=>({dataset:{page},setAttribute(k,v){this[k]=v;}}));
 let changed,editorPlacements=0;
 const context=vm.createContext({$:id=>panels[id],document:{querySelectorAll:()=>buttons},location:{hash:'#monitor'},history:{replaceState(_s,_t,hash){context.location.hash=hash;}},placeDraftEditor(){editorPlacements++;},loadHistory(){},window:{addEventListener(type,callback){assert.equal(type,'hashchange');changed=callback;}}});
 const fn=html.slice(html.indexOf('function showPage(page)'),html.indexOf('let historyLoading='));
 const listener=html.split('\n').find(s=>s.startsWith("window.addEventListener('hashchange'"));
 vm.runInContext(fn+'\n'+listener,context);
 context.location.hash='#results';changed();
 assert.equal(panels['results-panel'].hidden,false);
 assert.equal(panels['monitor-panel'].hidden,true);
 assert.equal(buttons.find(b=>b.dataset.page==='results')['aria-selected'],'true');
 assert.equal(context.location.hash,'#results');assert.equal(editorPlacements,1);
});

test('a hung status connection times out and the next poll can recover',async()=>{
 const nodes=new Map();const get=id=>{if(!nodes.has(id))nodes.set(id,{});return nodes.get(id);};
 let deadline,deadlineMs,cleared=0,calls=0,rendered;
 const context=vm.createContext({$:get,statusFresh:false,AbortController,Error,console:{error(){}},updateControls(){},render(data){rendered=data;},setTimeout(callback,ms){deadline=callback;deadlineMs=ms;return 1;},clearTimeout(){cleared++;},fetch:(_url,{signal})=>{calls++;if(calls===1)return new Promise((_resolve,reject)=>signal.addEventListener('abort',()=>reject(Object.assign(new Error('aborted'),{name:'AbortError'}))));return Promise.resolve({ok:true,json:async()=>({ok:true})});}});
 const code=html.slice(html.indexOf('let polling=false;'),html.indexOf('function showPage(page)'));
 vm.runInContext(code,context);
 const first=vm.runInContext('poll()',context);assert.equal(deadlineMs,10000);
 await vm.runInContext('poll()',context);assert.equal(calls,1);
 deadline();await first;
 assert.match(get('connection').textContent,/超时.*自动重试/);
 assert.equal(vm.runInContext('polling',context),false);assert.equal(context.statusFresh,false);
 await vm.runInContext('poll()',context);
 assert.equal(calls,2);assert.equal(rendered.ok,true);assert.ok(rendered.received_at);assert.equal(context.statusFresh,true);assert.equal(cleared,2);
});

test('background status loading and stale samples never enable task actions',async()=>{
 const nodes=new Map();const get=id=>{if(!nodes.has(id))nodes.set(id,{});return nodes.get(id);};
 let rendered=0;const context=vm.createContext({$:get,statusFresh:true,AbortController,Error,Date,Math,console:{error(){}},updateControls(){},render(){rendered++;},setTimeout(){return 1;},clearTimeout(){},fetch:async()=>({ok:true,json:async()=>({ok:true,status_loading:true,status_sample:{refreshing:true}})})});
 const code=html.slice(html.indexOf('let polling=false;'),html.indexOf('function showPage(page)'));vm.runInContext(code,context);
 await vm.runInContext('poll()',context);assert.equal(rendered,0);assert.equal(context.statusFresh,false);assert.match(get('connection').textContent,/正在汇总/);
 context.fetch=async()=>({ok:true,json:async()=>({ok:true,status_sample:{age_seconds:31}})});
 await vm.runInContext('poll()',context);assert.equal(context.statusFresh,false);assert.match(get('overview').textContent,/31 秒前/);
});


test('live controls survive slow report accounting but disconnect immediately removes live authority',async()=>{
 const nodes=new Map(),get=id=>{if(!nodes.has(id))nodes.set(id,{});return nodes.get(id);};
 const context=vm.createContext({$:get,lastStatus:{live_loading:false},statusFresh:true,AbortController,Error,Date,Math,console:{error(){}},updateControls(){},render(data){context.lastStatus=data;},setTimeout(){return 1;},clearTimeout(){},fetch:async()=>({ok:true,json:async()=>({ok:true,status_sample:{age_seconds:40},live_sample:{age_seconds:0,sampled_at:Date.now()/1000},stage3_runtime:{agent_blocks:{}}})})});
 vm.runInContext(html.slice(html.indexOf('let polling=false;'),html.indexOf('function showPage(page)')),context);
 await vm.runInContext('poll()',context);
 assert.equal(context.statusFresh,false);assert.match(get('connection-warning').textContent,/第三层运行控制已单独同步/);
 assert.match(get('overview').textContent,/第三层运行状态已同步/);
 context.fetch=async()=>{throw Error('offline');};await vm.runInContext('poll()',context);
 assert.equal(context.lastStatus.live_loading,true);assert.match(get('connection-warning').textContent,/上次记录/);
 context.fetch=async()=>({ok:true,json:async()=>({ok:true,status_sample:{age_seconds:0},live_sample:{age_seconds:0,sampled_at:Date.now()/1000}})});
 await vm.runInContext('poll()',context);assert.equal(get('connection-warning').hidden,true);
});

test('a timed out control request unlocks the page and never resubmits automatically',async()=>{
 let timeout,calls=0,message='';
 const context=vm.createContext({busy:false,statusFresh:false,lastStatus:{category_key:'fixture'},liveStage3Fresh:()=>true,AbortController,
  updateControls(){},feedback(text){message=text;},poll(){},setTimeout(callback){timeout=callback;return 1;},clearTimeout(){},
  fetch:(_path,{signal})=>{calls++;return new Promise((_resolve,reject)=>signal.addEventListener('abort',()=>reject(Object.assign(Error('timeout'),{name:'AbortError'}))));}});
 vm.runInContext(html.slice(html.indexOf('async function action(endpoint,body)'),html.indexOf("$('retry-generation').addEventListener")),context);
 const pending=vm.runInContext("action('/api/control-stage3',{action:'pause'})",context);
 assert.equal(calls,1);timeout();await pending;
 assert.equal(context.busy,false);assert.equal(calls,1);assert.match(message,/提交结果待确认.*不会自动重复提交/);
});
