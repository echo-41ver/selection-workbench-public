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
 const node=tag=>({tag,children:[],listeners:{},append(...values){this.children.push(...values);},replaceChildren(){this.children=[];},addEventListener(type,callback){this.listeners[type]=callback;},scrollIntoView(){this.scrolled=true;}});
 const nodes=new Map(['run-overview','human-attention','export-status','next-step','market-details'].map(id=>[id,node()])),pages=[];
 const context=vm.createContext({$:id=>nodes.get(id),document:{createElement:node},showPage:page=>pages.push(page),
  first:(row,keys)=>keys.map(k=>row?.[k]).find(v=>v!==undefined&&v!==null&&v!==''),workerCardRow:(row)=>row});
 vm.runInContext(helpers,context);return {nodes,context,pages};
}

test('recorded unfinished layer counts state the remaining work without claiming activity',()=>{
 const {nodes,context}=setup();context.data={active:false,details:{阶段:{
  amazon:{completed:3,total:4,remaining:1},
  stage1:{completed:2176,total:23272,remaining:21096},
  stage2:{completed:1176,total:2176,remaining:1000}}}};
 const original=JSON.stringify(context.data);
 vm.runInContext('renderRuntimeOverview(data)',context);
 const cards=nodes.get('run-overview').children;
 assert.equal(cards[0].children[2].textContent,'等待读取状态');
 assert.equal(cards[3].children[1].textContent,'2,176 / 23,272');
 assert.equal(cards[3].children[2].textContent,'尚有 21,096 对未处理；进度按批次更新');
 assert.equal(cards[4].children[2].textContent,'尚有 1,000 对未处理；进度按批次更新');
 for(const card of cards.slice(3,5)){
  assert.doesNotMatch(card.children[2].textContent,/正在|运行中|已完成|等待读取状态/);
  assert.doesNotMatch(card.className,/success/);
 }
 assert.equal(JSON.stringify(context.data),original);
 context.data.details.阶段.stage1={completed:0,total:12,remaining:12};
 vm.runInContext('renderRuntimeOverview(data)',context);
 assert.equal(nodes.get('run-overview').children[3].children[2].textContent,'尚有 12 对未处理；进度按批次更新');
});

test('terminal image-source failures surface in the overview and attention without claiming a batch is pending',()=>{
 const {nodes,context}=setup();
 const notice='1 件商品图片来源损坏，影响 10 对图片筛选；程序已停止这些图片的自动重试';
 context.data={active:true,details:{阶段:{stage2:{completed:52770,total:52780,remaining:10,failed:10,waiting:0,
  image_action_required:true,image_action_products:1,image_action_assets:1,image_action_pairs:10,
  manual_action_required:true,状态:notice,建议动作:'已完成结果和断点已保留。请联系维护者处理图片来源，修复后继续本任务。'}}}};
 vm.runInContext('renderRuntimeOverview(data)',context);
 const card=nodes.get('run-overview').children[4];
 assert.equal(card.children[1].textContent,'52,770 / 52,780');
 assert.equal(card.children[2].textContent,notice);assert.match(card.className,/danger/);
 assert.doesNotMatch(card.children[2].textContent,/进度按批次更新|已完成|100%/);
 assert.equal(nodes.get('human-attention').hidden,false);
 assert.match(nodes.get('human-attention').children[1].textContent,/第二层.*1 件商品.*10 对.*停止.*维护者/);
 context.data.details.阶段.stage2={completed:52780,total:52780,remaining:0,failed:0,waiting:0,image_action_required:false};
 vm.runInContext('renderRuntimeOverview(data)',context);
 assert.equal(nodes.get('human-attention').hidden,true);
 assert.equal(nodes.get('run-overview').children[4].children[2].textContent,'当前入库范围已完成');
 assert.match(nodes.get('run-overview').children[4].className,/success/);
});

test('missing, empty or inconsistent layer counts cannot imply completion or remaining work',()=>{
 const {nodes,context}=setup();
 const cases=[{}, {completed:0,total:0,remaining:0}, {completed:7,total:7},
  {completed:7,total:7,remaining:1}, {completed:8,total:7,remaining:0},
  {completed:-1,total:7,remaining:8}, {completed:1.5,total:7,remaining:5.5},
  {completed:7,total:'7',remaining:0}, {completed:NaN,total:7,remaining:0},
  {completed:Infinity,total:7,remaining:0}, {completed:0,total:7,remaining:0},
  {completed:0,total:Number.MAX_SAFE_INTEGER+1,remaining:Number.MAX_SAFE_INTEGER+1}];
 for(const row of cases){
  context.data={details:{阶段:{stage1:row,stage2:row}}};
  vm.runInContext('renderRuntimeOverview(data)',context);
  for(const card of nodes.get('run-overview').children.slice(3,5)){
   assert.equal(card.children[2].textContent,'等待读取状态',JSON.stringify(row));
   assert.doesNotMatch(card.className,/success/);
  }
 }
});

test('explicit layer status and errors take priority, while a confirmed nonempty completion stays complete',()=>{
 const {nodes,context}=setup();
 for(const row of [
  {状态:'数据库读取失败',status:'RUNNING'}, {status:'OFFLINE'},
  {状态:'已暂停'}, {error:'数据库读取失败'}, {错误:'第一层统计错误'},
  {error:{message:'数据库查询失败'}}]){
  context.data={details:{阶段:{stage1:{completed:7,total:7,remaining:0,...row}}}};
  vm.runInContext('renderRuntimeOverview(data)',context);
  const card=nodes.get('run-overview').children[3],expected=row.状态||row.status||row.错误||(typeof row.error==='string'?row.error:row.error.message);
  assert.equal(card.children[2].textContent,expected);
  if(/失败|错误/.test(expected))assert.match(card.className,/danger/);
  if(expected==='OFFLINE')assert.doesNotMatch(card.className,/success/);
 }
 context.data={details:{阶段:{stage1:{completed:7,total:7,remaining:0}}}};
 vm.runInContext('renderRuntimeOverview(data)',context);
 const card=nodes.get('run-overview').children[3];
 assert.equal(card.children[2].textContent,'当前入库范围已完成');
 assert.match(card.className,/success/);
});

test('confirmed user stop removes actionable alerts and restart restores unresolved failures',()=>{
 const {context,nodes}=setup();context.data={stage3_provider:{mode:'local_model'},
  stage3_control:{error:true,phase:'分析暂停，请查看错误记录',running:false},
  workers:[{name:'worker-01-new',status:'WAITING_CAPTCHA',manual_action_required:true}]};
 vm.runInContext('renderRuntimeOverview(data)',context);
 assert.equal(nodes.get('human-attention').hidden,false);
 context.data.task_stopped=true;context.data.stage3_control.stopped=true;
 vm.runInContext('renderRuntimeOverview(data)',context);
 assert.equal(nodes.get('human-attention').hidden,true);
 assert.equal(nodes.get('human-attention').children.length,0);
 assert.match(vm.runInContext('runtimeStage3(data)["状态"]',context),/已停止/);
 assert.doesNotMatch(nodes.get('run-overview').children[5].className,/danger/);
 vm.runInContext(html.slice(html.indexOf('function nextStep('),html.indexOf('const secretKey=')),context);
 vm.runInContext('nextStep(data)',context);
 assert.match(nodes.get('next-step').textContent,/任务已停止/);
 assert.doesNotMatch(nodes.get('next-step').className,/danger/);
 assert.equal(context.data.stage3_control.error,true);
 context.data.task_stopped=false;context.data.stage3_control.stopped=false;
 vm.runInContext('renderRuntimeOverview(data)',context);
 assert.equal(nodes.get('human-attention').hidden,false);
});

test('stopping only analysis preserves current collection alerts',()=>{
 const {context}=setup();context.data={stage3_provider:{mode:'local_model'},
  stage3_control:{stopped:true,error:true},workers:[{name:'worker-01-new',status:'WAITING_CAPTCHA',manual_action_required:true}]};
 assert.equal(vm.runInContext('attentionItems(data).length',context),1);
 assert.match(vm.runInContext('attentionItems(data)[0].name',context),/TEMU/);
});

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

test('updated union is green while an actual union block stays red',()=>{
 const {context}=setup();
 assert.equal(vm.runInContext("statusTone({状态:'汇总已更新'})",context),'success');
 assert.equal(vm.runInContext("statusTone({状态:'汇总已更新',阻断:true})",context),'danger');
 assert.equal(vm.runInContext("statusTone({状态:'汇总失败'})",context),'danger');
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

test('local model and API overview retain their active controller instead of old Agent state',()=>{
 const {context}=setup();context.data={stage3_control:{running:true,phase:'分析中',progress:{inflight_requests:2},codex_review:{running:true,pool:{phase:'已停止，结果保留',running_groups:0}}},stage3_runtime:{agent_blocks:{doubao:{message:'请完成验证码'}}}};
 for(const mode of ['local_model','multimodal_api','liquid_d1']){
  context.data.stage3_provider={mode,agent_ids:['doubao']};
  assert.equal(vm.runInContext('runtimeStage3(data)["状态"]',context),'分析中');
  assert.equal(vm.runInContext('runtimeStage3(data)["并发组数"]',context),2);
  assert.equal(vm.runInContext('attentionItems(data).length',context),0);
 }
});

test('stopped local controller ignores another running Agent review',()=>{
 const {context}=setup();context.data={stage3_provider:{mode:'local_model'},stage3_control:{running:false,phase:'分析暂停，请查看错误记录',codex_review:{running:true,pool:{phase:'旧Agent运行中'}}}};
 assert.equal(vm.runInContext('runtimeStage3(data)["状态"]',context),'分析暂停，请查看错误记录');
 assert.notEqual(vm.runInContext('runtimeStage3(data).running',context),true);
});

test('a failed local controller is red instead of idle; a resumed controller clears the alert',()=>{
 const {context,nodes}=setup();context.data={stage3_provider:{mode:'local_model'},stage3_control:{running:false,error:true,phase:'本地模型连续返回不合格结果，已暂停'}};
 vm.runInContext('renderRuntimeOverview(data)',context);
 assert.equal(nodes.get('human-attention').hidden,false);
 assert.match(nodes.get('run-overview').children[5].className,/danger/);
 assert.match(nodes.get('human-attention').children[1].textContent,/不合格/);
 context.data.stage3_control={running:true,error:false,phase:'分析中'};
 vm.runInContext('renderRuntimeOverview(data)',context);
 assert.equal(nodes.get('human-attention').hidden,true);
});

test('local activity shows real characters and uses its own heartbeat instead of an old Agent',()=>{
 const {context}=setup();context.data={stage3_provider:{mode:'local_model'},live_sample:{sampled_at:500},stage3_control:{running:true,phase:'分析中',codex_review:{pool:{updated_at:1}}},stage3_activity:{status:'receiving',received_chars:1234,updated_at:100}};
 assert.equal(vm.runInContext('stage3Heartbeat(data).time',context),100);
 assert.equal(vm.runInContext('stage3Heartbeat(data).label',context),'本地模型响应更新');
 assert.match(vm.runInContext('runtimeStage3(data)["状态"]',context),/1,234.*整组/);
 context.data.stage3_activity={status:'waiting',updated_at:110};
 assert.equal(vm.runInContext('stage3Heartbeat(data).label',context),'模型请求开始');
 assert.match(vm.runInContext('stage3ActivityText(data,130)',context),/20 秒/);
 context.data.stage3_control.running=false;
 assert.equal(vm.runInContext('stage3ActivityText(data)',context),'');
 context.data.stage3_provider.mode='local_agent';
 assert.equal(vm.runInContext('stage3Heartbeat(data).time',context),1);
 context.data.stage3_provider.mode='liquid_d1';context.data.live_sample={sampled_at:77};
 assert.equal(vm.runInContext('stage3Heartbeat(data).time',context),77);
 assert.match(vm.runInContext('stage3Heartbeat(data).label',context),/采样/);
});

test('non-streaming local D1 labels the actual live sample without claiming a response heartbeat',()=>{
 const {context}=setup();context.data={stage3_provider:{mode:'local_model'},live_sample:{sampled_at:177},
  stage3_activity:{},stage3_control:{running:true,progress:{completed:39,total:142},codex_review:{pool:{updated_at:1}}}};
 const before=JSON.stringify(context.data);
 assert.equal(vm.runInContext('stage3Heartbeat(data).label',context),'本地模型状态采样');
 assert.equal(vm.runInContext('stage3Heartbeat(data).time',context),177);
 assert.equal(vm.runInContext('stage3ActivityText(data)',context),'');
 assert.doesNotMatch(vm.runInContext('stage3Heartbeat(data).label',context),/响应|请求开始|心跳/);
 assert.equal(JSON.stringify(context.data),before);
 delete context.data.stage3_activity;
 assert.equal(vm.runInContext('stage3Heartbeat(data).time',context),177);
});

test('local sample timestamps require real finite evidence and never use another Agent heartbeat',()=>{
 const {context}=setup();context.data={stage3_provider:{mode:'local_model'},
  stage3_control:{codex_review:{pool:{updated_at:999}}}};
 for(const sampled_at of [undefined,null,0,-1,NaN,Infinity,'177',true]){
  context.data.live_sample={sampled_at};
  assert.equal(vm.runInContext('stage3Heartbeat(data).label',context),'本地模型状态采样');
  assert.equal(vm.runInContext('stage3Heartbeat(data).time',context),undefined);
 }
 context.data.live_sample={sampled_at:177};
 for(const updated_at of [undefined,null,0,-1,NaN,Infinity,'100']){
  context.data.stage3_activity={status:'receiving',updated_at};
  assert.equal(vm.runInContext('stage3Heartbeat(data).label',context),'本地模型状态采样');
  assert.equal(vm.runInContext('stage3Heartbeat(data).time',context),177);
 }
 for(const status of ['completed','incomplete']){
  context.data.stage3_activity={status,updated_at:100};
  assert.equal(vm.runInContext('stage3Heartbeat(data).label',context),'本地模型响应更新');
  assert.equal(vm.runInContext('stage3Heartbeat(data).time',context),100);
 }
});


test('partial raw observations stay visible as a lower bound without a legacy or full-completion claim',()=>{
 const {nodes,context}=setup();context.data={active:true,scope:{execution_mode:'collection'},details:{阶段:{temu:{overall:{
  count_basis:'PARTIAL_RAW_FRONTEND_CARDS',raw_count_lower_bound:2,raw_recorded_passes:2,raw_unrecorded_passes:1,raw_pending_passes:1,
  completed:null,total:8,percent:null,all_passes_complete:false}}}}};
 vm.runInContext('renderRuntimeOverview(data)',context);
 const card=nodes.get('run-overview').children[1];assert.equal(card.children[1].textContent,'已记录至少 2 条');
 assert.match(card.children[2].textContent,/另 1 轮尚未记录条数.*恢复\/初始化中/);
 assert.doesNotMatch(card.children[2].textContent,/旧任务|采集已完成|100%/);
 context.data.details.阶段.temu.overall={count_basis:'LEGACY_UNIQUE',raw_count_lower_bound:0,raw_recorded_passes:0,raw_unrecorded_passes:1};
 vm.runInContext('renderRuntimeOverview(data)',context);assert.match(nodes.get('run-overview').children[1].children[2].textContent,/旧任务未记录/);
});


test('market authorization alert uses a Chinese reason and opens its existing results controls without requests',()=>{
 const {context,nodes,pages}=setup();let calls=0;
 context.fetch=context.action=()=>{calls++;throw Error('navigation must not request');};
 context.data={scope:{execution_mode:'collection'},market_status:{status:'ACTION_REQUIRED',action:'AUTH_REQUIRED',jumpUrl:'https://openapi.geekbi.com/login?token=fixture-secret'}};
 const before=JSON.stringify(context.data);vm.runInContext('renderRuntimeOverview(data)',context);
 const p=nodes.get('human-attention').children[1],link=p.children.find(n=>n.tag==='a');
 assert.match(p.textContent,/极鲸云需要网页登录授权/);assert.doesNotMatch(p.textContent,/AUTH_REQUIRED|ACTION_REQUIRED|fixture-secret/);
 assert.equal(link.href,'#results');assert.match(link.textContent,/极鲸云/);
 let prevented=false;link.listeners.click({preventDefault(){prevented=true;}});
 assert.equal(prevented,true);assert.deepEqual(pages,['results']);assert.equal(nodes.get('market-details').open,true);assert.equal(nodes.get('market-details').scrolled,true);
 assert.equal(calls,0);assert.equal(JSON.stringify(context.data),before);
 assert.match(html,/<details id="market-details"[^>]*><summary>更多市场数据 · 极鲸云/);
});

test('simultaneous TEMU verification and market authorization retain separate actions',()=>{
 const {context,nodes,pages}=setup();context.data={scope:{execution_mode:'collection'},workers:[{name:'worker-01-new',status:'WAITING_SEARCH_VERIFICATION',manual_action_required:true}],market_status:{status:'ACTION_REQUIRED',action:'AUTH_REQUIRED'}};
 vm.runInContext('renderRuntimeOverview(data)',context);
 const ps=nodes.get('human-attention').children.slice(1),links=ps.map(p=>p.children.find(n=>n.tag==='a'));
 assert.equal(ps.length,2);assert.match(ps[0].textContent,/TEMU 第 1 路/);assert.match(ps[1].textContent,/市场数据/);
 assert.equal(links[0].href,'#monitor');assert.equal(links[1].href,'#results');
 assert.equal(links[0].listeners.click,undefined);assert.notEqual(nodes.get('market-details').open,true);
 links[1].listeners.click({preventDefault(){}});assert.deepEqual(pages,['results']);assert.equal(nodes.get('market-details').open,true);
});

test('other market actions do not claim login and a confirmed stopped task remains free of alerts',()=>{
 const {context,nodes}=setup();context.data={scope:{execution_mode:'collection'},market_status:{status:'ACTION_REQUIRED',action:'PAYMENT_REQUIRED',message:'fixture-private-message'}};
 vm.runInContext('renderRuntimeOverview(data)',context);const p=nodes.get('human-attention').children[1];
 assert.match(p.textContent,/极鲸云需要人工处理/);assert.doesNotMatch(p.textContent,/需要网页登录授权|fixture-private-message|PAYMENT_REQUIRED/);
 assert.equal(p.children.find(n=>n.tag==='a').href,'#results');
 context.data.task_stopped=true;vm.runInContext('renderRuntimeOverview(data)',context);assert.equal(nodes.get('human-attention').hidden,true);assert.equal(nodes.get('human-attention').children.length,0);
});
