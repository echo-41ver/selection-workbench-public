import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';

const html=fs.readFileSync(new URL('../web/index.html',import.meta.url),'utf8');

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
 const nodes=new Map();const get=id=>{if(!nodes.has(id))nodes.set(id,{style:{},setAttribute(){}});return nodes.get(id);};
 const code=html.slice(html.indexOf('function stage3Button(data)'),html.indexOf('const excelOpening='));
 const context=vm.createContext({$:get,statusFresh:false,busy:false});
 vm.runInContext(code,context);
 assert.doesNotThrow(()=>vm.runInContext('stage3Button(null)',context));
 assert.equal(get('start-stage3').disabled,true);
 assert.equal(get('stop-stage3').disabled,true);
 assert.equal(get('codex-review-controls').hidden,true);
 assert.match(get('stage3-count').textContent,/已进入第三层 0 对/);
});

test('Codex pool status stays separate from full candidate progress and reports Excel publishing',()=>{
 const nodes=new Map();const get=id=>{if(!nodes.has(id))nodes.set(id,{style:{},setAttribute(k,v){this[k]=v;}});return nodes.get(id);};
 const code=html.slice(html.indexOf('function stage3Button(data)'),html.indexOf('const excelOpening='));
 const context=vm.createContext({$:get,statusFresh:true,busy:false});
 vm.runInContext(code,context);
 vm.runInContext(`stage3Button({category_key:'fixture',scope:{execution_mode:'full'},stage3_control:{enabled:false,running:false,can_stop:false,phase:'等待',progress:{total:1986,completed:491,codex_completed:0,pending:1495},codex_review:{available:true,running:true,phase:'持续并行看图',target:768,imported:696,expected_tasks:8,all_reviews_complete:true,excel_current:false,pool:{enabled:true,running:true,workers:6,group_size:12,model:'gpt-6-luna',effort:'high',running_groups:6,pending_pairs:1495,completed_groups:41,phase:'并行看图并自动接续下一组'}}}})`,context);
 assert.match(get('stage3-count').textContent,/已进入第三层 1,986 对 · 已处理 491 对/);
 assert.match(get('codex-review-pool').textContent,/6 路 gpt-6-luna high · 每组 12 对/);
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
 assert.equal(calls,2);assert.deepEqual({...rendered},{ok:true});assert.equal(context.statusFresh,true);assert.equal(cleared,2);
});
