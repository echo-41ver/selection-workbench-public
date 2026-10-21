import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';

const helperHtml=fs.readFileSync(new URL('../web/index.html',import.meta.url),'utf8');
function sandbox(value){
 const context=vm.createContext(value);
 // Load the real render dependencies, rather than replacing new UI behavior with no-ops.
 const sourceCounts=helperHtml.split('\n').find(line=>line.startsWith('function renderSourceCounts('));
 const secretKey=helperHtml.split('\n').find(line=>line.startsWith('const secretKey='));
 const diagnostics=helperHtml.slice(helperHtml.indexOf('function runtimeDiagnosticSnapshot('),helperHtml.indexOf('function text(value)'));
 assert.ok(sourceCounts&&secretKey&&diagnostics,'current render dependencies must be present');
 vm.runInContext(helperHtml.slice(helperHtml.indexOf('function stage3Heartbeat('),helperHtml.indexOf('function runtimeStage3('))+'\n'+secretKey+'\n'+diagnostics+'\n'+sourceCounts,context);
 return context;
}

test('preparation displays current phase after rules complete and actual detail progress',()=>{
 const nodes=new Map();const get=id=>{if(!nodes.has(id))nodes.set(id,{});return nodes.get(id);};
 const context=sandbox({$:get});
 vm.runInContext(helperHtml.slice(helperHtml.indexOf('function preparationOnly('),helperHtml.indexOf('function render(data)')),context);
 context.data={console_busy:true,phase:'正在采集 Amazon 商品标题、详情参数并提炼关键词',
   generation:{stage:'complete',message:'本品类配置已生成并通过检查'}};
 vm.runInContext('renderPreparationProgress(data)',context);
 assert.equal(get('create-progress').textContent,context.data.phase);
 context.data.generation={stage:'amazon_details',message:'正在采集 Amazon 详情：0/10 件已完成；10 件遇到访问门禁，原断点保留'};
 vm.runInContext('renderPreparationProgress(data)',context);
 assert.equal(get('create-progress').textContent,context.data.generation.message);
 assert.equal(get('create-progress').hidden,false);
 context.data={category_key:'started'};vm.runInContext('renderPreparationProgress(data)',context);
 assert.equal(get('create-progress').hidden,true);
});


test('history downloads show their actual publication time and filename',()=>{
 const html=fs.readFileSync(new URL('../web/index.html',import.meta.url),'utf8');
 const code=html.slice(html.indexOf('function historyExcelLinks('),html.indexOf('async function openHistory('));
 const createElement=tag=>({tag,children:[],style:{},append(child){this.children.push(child);}});
 const context=sandbox({document:{createElement},encodeURIComponent,Date});
 vm.runInContext(code,context);
 context.files={layer123:{available:true,filename:'刀具_查看快照_20261012_121518.xlsx',updated_at:1791778518}};
 const links=vm.runInContext("historyExcelLinks('knife_task',files)",context);
 assert.equal(links.children.length,4);
 const link=links.children[3];assert.equal(link.href,'/api/history/knife_task/excel/layer123');
 assert.match(link.textContent,/下载最新 Excel/);
 assert.match(link.children[0].textContent,/Excel 更新：.*刀具_查看快照_20261012_121518.xlsx/);
 assert.equal(links.children[0].href,undefined);
 assert.match(html,/采集任务保存状态：/);
});

test('final controls keep market retry enabled while collection is active',()=>{
  const html=fs.readFileSync(new URL('../web/index.html',import.meta.url),'utf8');
  const line=html.split('\n').find(s=>s.trim().startsWith("$('refresh-market').disabled=busy||!statusFresh"));
  assert.ok(line);
  const button={disabled:true};
  const context=sandbox({$:()=>button,busy:false,statusFresh:true,active:true,data:{active:true,category_key:'fixture',market_busy:false,market_status:{status:'ACTION_REQUIRED'}}});
  vm.runInContext(line,context);assert.equal(button.disabled,false);
  context.data.release_update={pending:true};vm.runInContext(line,context);assert.equal(button.disabled,false);
  context.data.market_busy=true;vm.runInContext(line,context);assert.equal(button.disabled,true);
  context.data.market_busy=false;context.statusFresh=false;vm.runInContext(line,context);assert.equal(button.disabled,true);
});

test('saved market authorization is retried automatically, with cooldown and expiry bounds',async()=>{
 const html=fs.readFileSync(new URL('../web/index.html',import.meta.url),'utf8');
 const code=html.slice(html.indexOf('let marketLoginRetry='),html.indexOf("window.addEventListener('focus',()=>continueMarketLogin(true))"));
 let now=100000,calls=0;
 const data={category_key:'fixture',market_busy:false,release_update:{pending:true},market_status:{status:'ACTION_REQUIRED',action:'AUTH_REQUIRED',jumpUrl:'https://openapi.geekbi.com/agent/auth/login',updated_at:new Date(0).toISOString(),expiresIn:600}};
 const context=sandbox({lastStatus:data,busy:false,statusFresh:true,Date:{now:()=>now,parse:Date.parse},Number,action:async endpoint=>{assert.equal(endpoint,'/api/refresh-market');calls++;}});
 vm.runInContext(code,context);
 await vm.runInContext('continueMarketLogin()',context);assert.equal(calls,1);
 await vm.runInContext('continueMarketLogin(true)',context);assert.equal(calls,1);
 now+=15000;await vm.runInContext('continueMarketLogin()',context);assert.equal(calls,2);
 now=700000;await vm.runInContext('continueMarketLogin()',context);assert.equal(calls,2);
 await vm.runInContext('continueMarketLogin(true)',context);assert.equal(calls,3);
 now+=15000;data.market_status.status='COMPLETE';await vm.runInContext('continueMarketLogin(true)',context);assert.equal(calls,3);
 data.market_status.status='ACTION_REQUIRED';data.market_status.action='PAYMENT_REQUIRED';await vm.runInContext('continueMarketLogin(true)',context);assert.equal(calls,3);
 data.market_status.action='AUTH_REQUIRED';data.market_busy=true;await vm.runInContext('continueMarketLogin(true)',context);assert.equal(calls,3);
});

test('overall progress shows weighted count, hours and minutes, and paused lanes',()=>{
  const html=fs.readFileSync(new URL('../web/index.html',import.meta.url),'utf8');
  const code=html.slice(html.indexOf('function renderCollectionTotal(data)'),html.indexOf('function render(data)'));
  const nodes=new Map();
  const get=id=>{if(!nodes.has(id))nodes.set(id,{style:{},replaceChildren(){},setAttribute(k,v){this[k]=v;}});return nodes.get(id);};
  const context=sandbox({$:get,Number,Math});
  vm.runInContext(code,context);
  context.data={active:true,details:{'阶段':{temu:{overall:{completed:1200,total:4800,percent:25,keyword_count:4,finished_passes:2,total_passes:8,remaining:3600,eta_seconds:7260,source:'history',history_tasks:3,rate_per_hour:1785,blocked_lanes:1}}}}};
  vm.runInContext('renderCollectionTotal(data)',context);
  assert.equal(get('collection-total-fill').style.width,'25%');
  assert.match(get('collection-total-count').textContent,/1,200 \/ 4,800/);
  assert.match(get('collection-total-eta').textContent,/2 小时 1 分钟/);
  assert.match(get('collection-total-eta').textContent,/历史 3 个任务/);
  assert.match(get('collection-total-eta').textContent,/1 路需处理/);
  context.data.details['阶段'].temu.overall.eta_seconds=null;
  vm.runInContext('renderCollectionTotal(data)',context);
  assert.match(get('collection-total-eta').textContent,/采集等待恢复/);
  assert.doesNotMatch(get('collection-total-eta').textContent,/暂无足够|预计还需约/);
  context.data={};vm.runInContext('renderCollectionTotal(data)',context);
  assert.equal(get('collection-total').hidden,true);
});

test('run button submits the reviewed full pipeline and never enables paid vision',()=>{
  const html=fs.readFileSync(new URL('../web/index.html',import.meta.url),'utf8');
  const handler=html.split('\n').find(line=>line.startsWith("$('run').addEventListener('click'"));
  let clicked,request;
  const nodes={run:{addEventListener(type,callback){assert.equal(type,'click');clicked=callback;}},
    'worker-count':{value:'4'},'stage1-rule':{value:'reviewed text gate'},'stage2-threshold':{value:'65'},
    'stage3-rule':{value:'reviewed whole product rule'},'stage3-modules':{value:'{"module":100}'}};
  vm.runInNewContext(handler,{$:id=>nodes[id],draftId:'draft-1',Number,
    businessSettings:()=>({same_limit:12}),action:(url,body)=>{request={url,body};}});
  clicked();
  assert.equal(request.url,'/api/run');
  assert.equal(request.body.stage1_text,'reviewed text gate');
  assert.equal(request.body.stage2_threshold,65);
  assert.equal(request.body.stage3_text,'reviewed whole product rule');
  assert.equal(request.body.commercial.same_limit,12);
  assert.equal(request.body.execution_mode,undefined);
  assert.equal(request.body.paid,undefined);
});

test('collection status polls keep hidden controls attached across repeated renders',()=>{
  const html=fs.readFileSync(new URL('../web/index.html',import.meta.url),'utf8');
  const code=html.slice(html.indexOf('function render(data)'),html.indexOf('\nlet polling='));
  const nodes=new Map();
  function node(id){
    const n={id,connected:true,children:[],hidden:false,dataset:{},style:{},
      replaceChildren(){for(const child of this.children)child.connected=false;this.children=[];},
      append(...children){for(const child of children){assert.ok(child,'cannot append a missing element');child.connected=true;this.children.push(child);}},
    };
    if(id)nodes.set(id,n);return n;
  }
  for(const match of html.matchAll(/id="([^"]+)"/g))node(match[1]);
  const context=sandbox({
    $:id=>nodes.get(id)?.connected?nodes.get(id):null,
    document:{createElement:()=>node()},busy:false,lastStatus:null,draftId:'draft',keywordReady:true,
    renderFrontendMarket(){},renderBusiness(){},renderDraft(){},renderPlainApplication(){},cards:[],addProgressCard(title,row,wide,n){if(n)context.cards.push(n);},sectionLabel(){},workerCardRow(actual){return actual;},
    first:(row,keys)=>keys.map(k=>row[k]).find(v=>v!==undefined),stage3Button(){},excelButtons(){},safe:x=>x,text:String,redFlags:/FAILED|失败/,URL,Date,
  });
  vm.runInContext(html.slice(html.indexOf('function statusTone('),html.indexOf('let statusFresh=')),context);
  context.lastGenerationFeedback='';context.statusFresh=true;context.nextStep=()=>{};context.updateControls=()=>{};context.feedback=(message,error)=>{nodes.get('feedback').textContent=message;nodes.get('feedback').className='notice '+(error?'danger':'success');};
  vm.runInContext(code,context);
  const third=nodes.get('stage3-controls');
  for(const mode of ['collection','collection','collection','full','collection']){
    context.fixture={active:true,phase:'运行中',severity:'red',category_key:'power_bank_7a9b6ea0fc85_260f367e484a',scope:{execution_mode:mode,worker_count:4},workers:[],details:{},commercial:{}};
    context.preparationFeedback=true;
    nodes.get('feedback').textContent='编译和校验规则';
    vm.runInContext('render(fixture)',context);
    assert.equal(nodes.get('connection').textContent,'本地服务已连接');
    assert.equal(nodes.get('overview').className,'notice success');
    assert.equal(nodes.get('feedback').textContent,'');
    assert.equal(third.connected,true);
    assert.equal(third.hidden,mode==='collection');
  }
  context.fixture={active:true,phase:'FAILED',severity:'red',scope:{worker_count:4},workers:[],details:{}};
  vm.runInContext('render(fixture)',context);
  assert.equal(nodes.get('overview').className,'notice danger');
  context.fixture={active:false,can_resume:true,category_key:'saved',scope:{worker_count:4},workers:[],details:{}};
  vm.runInContext('render(fixture)',context);
  assert.equal(nodes.get('resume').disabled,false);
  assert.equal(nodes.get('stop').disabled,true);
  context.fixture.active=true;
  vm.runInContext('render(fixture)',context);
  assert.equal(nodes.get('resume').disabled,true);
  context.cards=[];
  context.fixture={active:true,phase:'运行中',scope:{execution_mode:'collection',worker_count:50},workers:[{name:'worker-04-new',manual_action_required:true},{name:'worker-12-new',manual_action_required:true}],details:{}};
  vm.runInContext('render(fixture)',context);
  assert.equal(context.cards.length,50);
  assert.deepEqual(Array.from(context.cards.slice(0,2)),[4,12]);
  assert.match(nodes.get('lane-totals').textContent,/需处理 2/);
  context.fixture={active:false,phase:'榜单定位重复，未找到准确品类',severity:'red',can_retry_generation:true,generation:{stage:'error',message:'榜单定位重复，未找到准确品类'},scope:{category:'遥控器',worker_count:12},workers:[],details:{}};
  vm.runInContext('render(fixture)',context);
  assert.match(nodes.get('overview').className,/danger/);
  assert.match(nodes.get('feedback').className,/danger/);
  assert.equal(nodes.get('listing-recovery').hidden,false);
  assert.match(nodes.get('lane-totals').textContent,/运行 0/);
  assert.match(nodes.get('lane-totals').textContent,/准备\/等待 12/);
  context.fixture={active:true,phase:'运行中',scope:{worker_count:50},workers:[{name:'worker-04-new',manual_action_required:true},{name:'worker-12-new',manual_action_required:true}],details:{}};
  nodes.get('only-errors').checked=true;context.cards=[];
  vm.runInContext('render(fixture)',context);
  assert.deepEqual(Array.from(context.cards),[4,12]);
  context.fixture={active:true,phase:'运行中',scope:{worker_count:2},workers:[{name:'worker-01-new',status:'WAITING_SEE_MORE'},{name:'worker-02-new',status:'RETRYING_SALES_SORT'}],details:{}};
  nodes.get('only-errors').checked=false;vm.runInContext('render(fixture)',context);
  assert.match(nodes.get('lane-totals').textContent,/运行 2/);
});

test('current lane evidence clears stale alerts and still displays real verification waits',()=>{
  const html=fs.readFileSync(new URL('../web/index.html',import.meta.url),'utf8');
  const code=html.slice(html.indexOf('const statusLabels='),html.indexOf('function sectionLabel('));
  const modules={children:[],append(n){this.children.push(n);}};
  const node=()=>({children:[],style:{},dataset:{},append(...n){this.children.push(...n);},setAttribute(){},addEventListener(){}});
  const context=sandbox({document:{createElement:node},$:()=>modules,
    redFlags:/captcha|verification|human_action|blocked|failed|error|验证码|人工|阻断|失败/i,
    busy:false,lastStatus:{active:true,category_key:'fixture'},rotationSubmitting:new Set()});
  context.statusFresh=true;
  vm.runInContext(html.slice(html.indexOf('function statusTone('),html.indexOf('let statusFresh=')),context);
  vm.runInContext(code,context);
  const stale={'状态':'等待人工验证','采集状态':'WAITING_CAPTCHA','需人工介入':true,'阻断':true,'建议动作':'请在当前浏览器完成验证'};
  const running={'状态':'运行中','采集状态':'RUNNING','需人工介入':false,'阻断':false};
  function card(live,stage=stale){
    context.live=live;context.stage=stage;
    vm.runInContext("addProgressCard('TEMU 第 1 路',workerCardRow({live_snapshot:live},stage,{}),false,1)",context);
    return modules.children.at(-1);
  }
  const recovered=card(running);
  assert.equal(recovered.className,'progress-card success lane-card');
  assert.equal(recovered.children.some(n=>n.className==='advice'),false);
  const waiting=card(stale,running);
  assert.equal(waiting.className,'progress-card danger lane-card');
  assert.equal(waiting.children.find(n=>n.className==='advice').textContent,stale['建议动作']);
  const post=card({...running,'状态':'验证后等待搜索页就绪','采集状态':'WAITING_POST_VERIFICATION_SEARCH_BOX','阻断':true});
  assert.equal(post.children.some(n=>n.className==='advice'),false);
  context.oldLogin={...stale,'采集状态':'STOPPED_LOGIN_REQUIRED','建议动作':'请登录'};
  const replacing=vm.runInContext("workerCardRow({status:'REPLACING_LOGIN_ENVIRONMENT',live_snapshot:oldLogin},oldLogin,{})",context);
  assert.equal(replacing['需人工介入'],false);
  assert.equal(replacing['建议动作'],'');
  assert.match(replacing['状态'],/自动更换环境/);
});


test('third layer starts during collection and displays live queue and stop controls',()=>{
 const html=fs.readFileSync(new URL('../web/index.html',import.meta.url),'utf8');
 const a=html.indexOf('function stage3Button(data)'),b=html.indexOf('\n}',a)+2;
 const nodes=new Map();const get=id=>{if(!nodes.has(id))nodes.set(id,{dataset:{},style:{},replaceChildren(){},setAttribute(k,v){this[k]=v;}});return nodes.get(id);};
 const context=sandbox({$:get,statusFresh:true,busy:false,Math,Number,codexReviewPanel:()=>{},renderStage3Settings:()=>{},stage3ProviderSettings:()=>undefined,stage3ResumeRequired:()=>false});
 vm.runInContext(html.slice(a,b),context);
 context.data={active:true,category_key:'task',scope:{execution_mode:'full'},stage3_control:{can_start:true,running:false,progress:{total:100,completed:40,pending:60,percent:40,rate_per_hour:60,eta_seconds:3600,rate_source:'history'}}};
 vm.runInContext('stage3Button(data)',context);assert.equal(get('start-stage3').disabled,false);assert.match(get('stage3-count').textContent,/100/);assert.equal(get('stage3-progress-fill').style.width,'40%');assert.match(get('stage3-speed').textContent,/1 小时 0 分钟/);
 context.data.stage3_control.running=true;context.data.stage3_control.can_stop=true;
 vm.runInContext('stage3Button(data)',context);assert.equal(get('start-stage3').disabled,true);assert.equal(get('stop-stage3').disabled,false);
});


test('combined paid and Codex completion shows full progress without stale ETA',()=>{
 const html=fs.readFileSync(new URL('../web/index.html',import.meta.url),'utf8');
 const code=html.slice(html.indexOf('function stage3Button(data)'),html.indexOf('function codexReviewPanel(data)'));
 const nodes=new Map(),get=id=>{if(!nodes.has(id))nodes.set(id,{dataset:{},style:{},replaceChildren(){},setAttribute(k,v){this[k]=v;}});return nodes.get(id);};
 const context=sandbox({$:get,statusFresh:true,busy:false,codexReviewPanel:()=>{},renderStage3Settings:()=>{},stage3ProviderSettings:()=>undefined});
 vm.runInContext(code,context);
 context.data={category_key:'fixture',stage3_control:{can_start:false,progress:{total:10855,completed:10855,api_completed:5976,codex_completed:4879,pending:0,pending_after_codex:0,eta_seconds:1616}}};
 vm.runInContext('stage3Button(data)',context);
 assert.equal(get('stage3-progress-fill').style.width,'100%');
 assert.match(get('stage3-count').textContent,/已处理 10,855/);
 assert.match(get('stage3-count').textContent,/已处理 10,855 对/);
 assert.match(get('stage3-speed').textContent,/剩余 0 分钟/);
 assert.equal(get('start-stage3').disabled,true);
});


test('cold candidate progress never unlocks paid action while collection is active',()=>{
 const html=fs.readFileSync(new URL('../web/index.html',import.meta.url),'utf8');
 const code=html.slice(html.indexOf('function stage3Button(data)'),html.indexOf('function codexReviewPanel(data)'));
 const nodes=new Map(),get=id=>{if(!nodes.has(id))nodes.set(id,{dataset:{},style:{},replaceChildren(){},setAttribute(){}});return nodes.get(id);};
 const context=sandbox({$:get,statusFresh:true,busy:false,codexReviewPanel:()=>{},renderStage3Settings:()=>{},stage3ProviderSettings:()=>undefined});
 vm.runInContext(code,context);context.data={active:true,category_key:'fixture',stage3_control:{can_start:true,progress:{counting:true}}};
 vm.runInContext('stage3Button(data)',context);assert.equal(get('start-stage3').disabled,true);
 assert.match(get('stage3-phase').textContent,/正在核对/);
 context.data.stage3_control.progress={total:0,pending:0};vm.runInContext('stage3Button(data)',context);assert.equal(get('start-stage3').disabled,false);
});

test('old authorized retry success is pending free review instead of current API completion',()=>{
 const html=fs.readFileSync(new URL('../web/index.html',import.meta.url),'utf8');
 const code=html.slice(html.indexOf('function stage3Button(data)'),html.indexOf('function codexReviewPanel(data)'));
 const nodes=new Map(),get=id=>{if(!nodes.has(id))nodes.set(id,{dataset:{},style:{},replaceChildren(){},setAttribute(){}});return nodes.get(id);};
 const context=sandbox({$:get,statusFresh:true,busy:false,codexReviewPanel:()=>{},renderStage3Settings:()=>{},stage3ProviderSettings:()=>undefined});vm.runInContext(code,context);
 context.data={category_key:'fixture',stage3_control:{can_start:false,progress:{total:1,completed:0,api_completed:0,pending:1,pending_after_codex:1,runnable_pending:0,billing_retry_consumed_pairs:1}}};
 vm.runInContext('stage3Button(data)',context);
 assert.match(get('stage3-count').textContent,/已处理 0 对.*待处理 1 对/);
 assert.match(get('stage3-count').textContent,/旧版本已补试，当前需免费看图复核，不自动付费/);
 assert.equal(get('start-stage3').disabled,true);
});


test('changed candidate input retains dated completion snapshot without claiming current zero queue',()=>{
 const html=fs.readFileSync(new URL('../web/index.html',import.meta.url),'utf8');
 const code=html.slice(html.indexOf('function stage3Button(data)'),html.indexOf('function codexReviewPanel(data)'));
 const nodes=new Map(),get=id=>{if(!nodes.has(id))nodes.set(id,{dataset:{},style:{},replaceChildren(){},setAttribute(){}});return nodes.get(id);};
 const context=sandbox({$:get,statusFresh:true,busy:false,codexReviewPanel:()=>{},renderStage3Settings:()=>{},stage3ProviderSettings:()=>undefined});vm.runInContext(code,context);
 for(const flag of ['refreshing','current_inputs_pending']){
  context.data={category_key:'fixture',stage3_control:{can_start:true,progress:{total:10855,completed:10855,api_completed:5976,codex_completed:4879,pending_after_codex:0,eta_seconds:0,updated_at:1791566063,[flag]:true}}};
  vm.runInContext('stage3Button(data)',context);
  assert.equal(get('start-stage3').disabled,true);
  assert.match(get('stage3-phase').textContent,/最近核验快照.*当前待处理尚未确认/);
  assert.match(get('stage3-count').textContent,/已处理 10,855 对/);
  assert.match(get('stage3-count').textContent,/快照待处理 0.*最近核验时间.*当前输入待核对/);
  assert.match(get('stage3-speed').textContent,/新增输入核对后显示/);
  assert.doesNotMatch(get('stage3-speed').textContent,/当前候选已全部处理|剩余 0 分钟/);
 }
});
