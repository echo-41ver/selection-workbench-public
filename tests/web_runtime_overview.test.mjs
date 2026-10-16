import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';

const html=fs.readFileSync(new URL('../web/index.html',import.meta.url),'utf8');
const helpers=html.slice(html.indexOf('function statusTone('),html.indexOf('let statusFresh='));

test('collection stays above analysis until collection completes, including after switching tasks',()=>{
 let order=[];const box={input:'preserved',get previousElementSibling(){return order[order.indexOf(box)-1];},get nextElementSibling(){return order[order.indexOf(box)+1];}};
 const move=offset=>{order=order.filter(n=>n!==box);order.splice(order.indexOf(analysis)+offset,0,box);};
 const analysis={before(){move(0);},after(){move(1);}};order=[analysis,box];
 const context=vm.createContext({$:id=>id==='collection-workspace'?box:analysis});
 vm.runInContext(html.slice(html.indexOf('function placeCollectionWorkspace('),html.indexOf('function render(data)')),context);
 vm.runInContext('placeCollectionWorkspace({collection_complete:false})',context);assert.equal(order[0],box);
 vm.runInContext('placeCollectionWorkspace({collection_complete:true})',context);assert.equal(order[1],box);
 vm.runInContext('placeCollectionWorkspace({collection_complete:false})',context);assert.equal(order[0],box);assert.equal(box.input,'preserved');
});
function setup(){
 const node=()=>({children:[],append(...values){this.children.push(...values);},replaceChildren(){this.children=[];}});
 const nodes=new Map(['run-overview','human-attention','export-status','next-step'].map(id=>[id,node()]));
 const context=vm.createContext({$:id=>nodes.get(id),document:{createElement:node},
  first:(row,keys)=>keys.map(k=>row?.[k]).find(v=>v!==undefined&&v!==null&&v!==''),workerCardRow:(row)=>row});
 vm.runInContext(helpers,context);return {nodes,context};
}

test('one running page contains analysis controls; export and results stay separate',()=>{
 const monitor=html.slice(html.indexOf('id="monitor-panel"'),html.indexOf('id="results-panel"'));
 const results=html.slice(html.indexOf('id="results-panel"'),html.indexOf('id="history-panel"'));
 assert.match(monitor,/id="run-overview"/);assert.match(monitor,/id="stage3-controls"/);
 assert.doesNotMatch(monitor,/id="excel-controls"|Excel 持续更新/);
 assert.match(results,/id="excel-controls"/);assert.match(results,/id="commercial"/);
 assert.doesNotMatch(results,/id="stage3-controls"/);
 assert.match(html,/data-page="monitor"[^>]*>运行总览/);
});

test('verification overrides running state and clears after user completes it',()=>{
 const {nodes,context}=setup();
 context.data={category_key:'task',scope:{execution_mode:'full'},workers:[],
  stage3_provider:{mode:'local_agent',agent_ids:['doubao']},stage3_control:{codex_review:{running:true},progress:{completed:24,api_completed:0,codex_completed:24,total:100,pending_after_codex:76}},
  stage3_runtime:{agent_blocks:{doubao:{code:'HUMAN_VERIFICATION_REQUIRED',message:'豆包需要验证码'}}}};
 vm.runInContext('renderRuntimeOverview(data)',context);
 assert.equal(nodes.get('human-attention').hidden,false);
 assert.match(nodes.get('human-attention').children[1].textContent,/第三层分析.*验证码.*继续此 Agent/);
 const cards=nodes.get('run-overview').children;
 assert.equal(cards.length,6);assert.match(cards[5].className,/danger/);
 assert.equal(cards[5].children[1].textContent,'24 / 100');
 context.data.stage3_runtime.agent_blocks={};
 vm.runInContext('renderRuntimeOverview(data)',context);
 assert.equal(nodes.get('human-attention').hidden,true);
 assert.match(nodes.get('run-overview').children[5].className,/success/);
});

test('manual pause is neutral, unrelated Agent blocks do not stop the selected Agent',()=>{
 const {context}=setup();context.data={stage3_provider:{mode:'local_agent',agent_ids:['doubao']},stage3_control:{codex_review:{running:true}},stage3_runtime:{paused_agents:['doubao']}};
 assert.match(vm.runInContext('runtimeStage3(data)["状态"]',context),/已暂停/);
 assert.equal(vm.runInContext('attentionItems(data).length',context),0);
 context.data.stage3_runtime={agent_blocks:{codex:{code:'LOGIN_REQUIRED'}}};
 assert.equal(vm.runInContext('attentionItems(data).length',context),0);
});

test('all manual-only stages are aggregated; automatic waits are not alerts',()=>{
 const {context,nodes}=setup();context.data={workers:[{name:'worker-01-new',status:'WAITING_CAPTCHA',manual_action_required:true},{name:'worker-02-new',status:'WAITING_INITIAL_PRODUCTS'}],
  details:{阶段:{temu_union:{状态:'汇总恢复预算耗尽',recovery_exhausted:true},stage12:{状态:'失败，需要处理'},excel:{状态:'已更新',actual_count:16}}},scope:{execution_mode:'full'}};
 vm.runInContext('renderRuntimeOverview(data)',context);
 assert.match(nodes.get('human-attention').children[0].textContent,/3 项/);
 assert.match(nodes.get('export-status').textContent,/已更新.*16 份文件/);
 assert.equal(vm.runInContext('statusTone({status:"WAITING_POST_VERIFICATION_SEARCH_BOX"})',context),'success');
 assert.equal(vm.runInContext('statusTone({status:"WAITING_LOGIN"})',context),'success');
});

test('page scripts parse after moving controls without duplicate IDs',()=>{
 for(const script of html.matchAll(/<script[^>]*>([\s\S]*?)<\/script>/g))new vm.Script(script[1]);
 const ids=Array.from(html.matchAll(/\bid="([^"]+)"/g),match=>match[1]);
 assert.equal(ids.length,new Set(ids).size);
});
