import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';
const html=fs.readFileSync(new URL('../web/index.html',import.meta.url),'utf8');
const code=html.slice(html.indexOf('let stage3SettingsDirty='),html.indexOf('function businessSettings'));

test('measured defaults follow the provider while manual edits defeat late responses',async()=>{
 const nodes=new Map(),get=id=>{if(!nodes.has(id))nodes.set(id,{value:'',checked:false});return nodes.get(id);};
 let resolve;const context=vm.createContext({AbortController,setTimeout,clearTimeout,$:get,lastStatus:null,fetch:()=>new Promise(r=>{resolve=r;})});vm.runInContext(code,context);
 get('stage3-mode').value='multimodal_api';get('stage3-confidence').value='90';
 const pending=vm.runInContext('refreshConcurrencyDefault()',context);
 resolve({ok:true,json:async()=>({workers:200,message:'measured'})});await pending;
 assert.equal(get('stage3-workers').value,200);
 const stale=vm.runInContext('refreshConcurrencyDefault()',context);
 vm.runInContext('stage3ManualConcurrency=true',context);get('stage3-workers').value='777';
 resolve({ok:true,json:async()=>({workers:10,message:'old'})});await stale;
 assert.equal(get('stage3-workers').value,'777');
});

test('a delayed default for the previous provider cannot replace the new provider',async()=>{
 const nodes=new Map(),get=id=>{if(!nodes.has(id))nodes.set(id,{value:'',checked:false});return nodes.get(id);};
 const replies=[];const context=vm.createContext({AbortController,setTimeout,clearTimeout,$:get,fetch:()=>new Promise(r=>replies.push(r))});vm.runInContext(code,context);
 get('stage3-mode').value='liquid_d1';get('stage3-confidence').value='90';const first=vm.runInContext('refreshConcurrencyDefault()',context);
 get('stage3-mode').value='local_agent';const second=vm.runInContext('refreshConcurrencyDefault()',context);
 replies[1]({ok:true,json:async()=>({workers:10,message:'agent'})});await second;
 replies[0]({ok:true,json:async()=>({workers:200,message:'old-api'})});await first;
 assert.equal(get('stage3-workers').value,10);
});

test('manual concurrency preserves the entered value across Agent and API modes without an HTML ceiling',()=>{
 const nodes=new Map(),get=id=>{if(!nodes.has(id))nodes.set(id,{value:'',checked:false});return nodes.get(id);};
 const context=vm.createContext({AbortController,setTimeout,clearTimeout,$:get,lastStatus:null});vm.runInContext(code,context);
 get('stage3-workers').value='1024';get('stage3-confidence').value='90';
 for(const mode of ['local_agent','liquid_d1','multimodal_api','local_model']){
  get('stage3-mode').value=mode;const provider=vm.runInContext('stage3ProviderSettings()',context);
  assert.equal(provider.workers,1024);assert.equal(provider.concurrency_mode,'manual');
 }
 assert.doesNotMatch(html.match(/<input id="stage3-workers"[^>]*>/)[0],/\bmax=/);
});

test('four provider modes bind endpoint model protocol and concurrency without retaining key',()=>{
 const nodes=new Map();const get=id=>{if(!nodes.has(id))nodes.set(id,{value:'',hidden:false,replaceChildren(){},append(){}});return nodes.get(id);};
 const context=vm.createContext({AbortController,setTimeout,clearTimeout,$:get,lastStatus:null,fetch:async()=>({ok:true,json:async()=>({agents:[]})})});vm.runInContext(code,context);
 const cases=[['liquid_d1','https://api.liquid.ai/decisions/v1/systemone','d1','decisions',4],
 ['multimodal_api','https://api.deepseek.com','deepseek-flash','responses',4],
 ['local_model','http://127.0.0.1:18181/v1/systemone','d1-3b','decisions',2],['local_agent','','gpt-6.1-sol','responses',6]];
 get('stage3-confidence').value='90';
 for(const [mode,url,model,protocol,workers] of cases){get('stage3-mode').value=mode;get('stage3-api-key').value='TEST_ONLY_OLD_KEY';vm.runInContext('chooseStage3Mode()',context);const p=vm.runInContext('stage3ProviderSettings()',context);assert.equal(p.mode,mode);assert.equal(p.base_url,url);assert.equal(p.model,model);assert.equal(p.protocol,protocol);assert.equal(p.workers,workers);assert.equal(get('stage3-api-key').value,'');assert.equal(get('stage3-api-fields').hidden,mode==='local_agent');}
});

test('third-layer click submits a dedicated key and clears the input',()=>{
 const nodes={'start-stage3':{addEventListener:(_event,cb)=>context.click=cb},'stage3-api-key':{value:'TEST_ONLY_VISUAL_KEY'}};
 let submitted;const context=vm.createContext({AbortController,setTimeout,clearTimeout,$:id=>nodes[id],stage3ProviderSettings:()=>({mode:'liquid_d1'}),stage3SettingsDirty:true,action:(path,body)=>submitted={path,body}});
 vm.runInContext(html.split('\n').find(x=>x.startsWith("$('start-stage3').addEventListener")),context);context.click();
 assert.equal(submitted.path,'/api/start-stage3');assert.equal(submitted.body.api_key,'TEST_ONLY_VISUAL_KEY');assert.equal(submitted.body.provider.mode,'liquid_d1');assert.equal(nodes['stage3-api-key'].value,'');
});

test('concurrency click submits only width and mode without model selection or API keys',()=>{
 let click,submitted;const nodes={'apply-stage3-concurrency':{addEventListener:(_e,cb)=>click=cb},'stage3-workers':{value:'400'},'stage3-auto':{checked:false}};
 const context=vm.createContext({AbortController,setTimeout,clearTimeout,$:id=>nodes[id],action:(path,body)=>submitted={path,body}});
 vm.runInContext(html.split('\n').find(line=>line.startsWith("$('apply-stage3-concurrency').addEventListener")),context);click();
 assert.equal(submitted.path,'/api/stage3/concurrency');assert.equal(submitted.body.workers,400);
 assert.deepEqual(Object.keys(submitted.body),['workers','concurrency_mode']);
});

test('temporary catalog refresh failure retains actual models and the chosen model',async()=>{
 const context=vm.createContext({AbortController,setTimeout,clearTimeout,lastStatus:null,fetch:async()=>({ok:false,json:async()=>({error:{message:'服务暂忙'}})})});
 vm.runInContext(code,context);
 vm.runInContext("renderAgentControls=()=>{};stage3AgentSelection=['doubao'];stage3DetectedAgents=[{id:'doubao',installed:true}];stage3AgentModels={doubao:'actual-expert'};stage3AgentCatalogs={doubao:{status:'ready',models:[{id:'actual-expert'}]}}",context);
 await vm.runInContext('refreshAgentModels()',context);
 assert.equal(vm.runInContext('stage3AgentCatalogs.doubao.models[0].id',context),'actual-expert');
 assert.equal(vm.runInContext('stage3AgentModels.doubao',context),'actual-expert');
 assert.equal(vm.runInContext('stage3AgentCatalogs.doubao.refresh_error',context),'服务暂忙');
});

test('first render before status arrives preserves the initialization flow',()=>{
 const nodes=new Map();const get=id=>{if(!nodes.has(id))nodes.set(id,{value:'',hidden:false,replaceChildren(){},append(){}});return nodes.get(id);};
 const context=vm.createContext({AbortController,setTimeout,clearTimeout,$:get,document:{querySelectorAll:()=>[]}});vm.runInContext(code,context);
 assert.doesNotThrow(()=>vm.runInContext('renderStage3Settings(null)',context));
});

test('single Agent sends only the selected Agent and its model',()=>{
 const nodes=new Map();const get=id=>{if(!nodes.has(id))nodes.set(id,{value:'',hidden:false,replaceChildren(){},append(){}});return nodes.get(id);};
 const context=vm.createContext({AbortController,setTimeout,clearTimeout,$:get,lastStatus:null});vm.runInContext(code,context);
 get('stage3-mode').value='local_agent';
 vm.runInContext("stage3AgentSelection=['codex'];stage3AgentModels={codex:'native-codex',acciowork:'native-accio'}",context);
 const provider=vm.runInContext('stage3ProviderSettings()',context);
 assert.equal(provider.agent_models.codex,'native-codex');assert.equal(provider.agent_models.acciowork,undefined);
 assert.deepEqual(Array.from(provider.agent_ids),['codex']);
 assert.match(html,/id="stage3-model-box"/);assert.match(html,/\/api\/local-agents\/models/);
});

test('multiple Agents require explicit selection and retain their separate models',()=>{
 const nodes=new Map();const get=id=>{if(!nodes.has(id))nodes.set(id,{value:'',hidden:false});return nodes.get(id);};
 const context=vm.createContext({AbortController,setTimeout,clearTimeout,$:get,lastStatus:null});vm.runInContext(code,context);get('stage3-mode').value='local_agent';
 vm.runInContext("stage3AgentSelection=['codex','doubao'];stage3AgentModels={codex:'native-codex',doubao:'native-doubao',acciowork:'unused'}",context);
 const provider=vm.runInContext('stage3ProviderSettings()',context);
 assert.deepEqual(Array.from(provider.agent_ids),['codex','doubao']);
 assert.equal(provider.agent_models.doubao,'native-doubao');assert.equal(provider.agent_models.acciowork,undefined);
 assert.match(html,/<details id="stage3-multi-agent-details"[^>]*>/);
 assert.doesNotMatch(html,/<details id="stage3-multi-agent-details"[^>]*\bopen\b/);
 assert.doesNotMatch(html,/id="stage3-agent-list"|各 Agent 使用的模型/);
});

test('no Agent or an unavailable model cannot silently dispatch to all Agents',()=>{
 const context=vm.createContext({AbortController,setTimeout,clearTimeout,$:()=>({value:''}),lastStatus:null});vm.runInContext(code,context);
 assert.equal(vm.runInContext('stage3AgentInputsReady()',context),false);
 vm.runInContext("stage3AgentSelection=['codex'];stage3AgentCatalogs={codex:{status:'ready',models:[{id:'vision',vision:true},{id:'text',vision:false}]}};stage3AgentModels={codex:'text'}",context);
 assert.equal(vm.runInContext('stage3AgentInputsReady()',context),false);
 vm.runInContext("stage3AgentModels.codex='vision'",context);assert.equal(vm.runInContext('stage3AgentInputsReady()',context),true);
});

test('ordinary page keeps one primary delivery and collapses optional flows',()=>{
 assert.match(html,/\[data-excel\]:not\(\[data-excel="layer123"\]\)/);
 assert.match(html,/<summary>规则生成服务设置/);assert.match(html,/<summary>调整同款规则/);
 assert.match(html,/function renderBusiness\([^\n]*renderSelectionSummary/);
 for(const mode of ['liquid_d1','multimodal_api','local_model','local_agent'])assert.match(html,new RegExp('option value="'+mode+'"'));
 assert.doesNotMatch(html,/对（API /);
 assert.doesNotMatch(html,/中英文界面均可识别/);
 assert.doesNotMatch(html,/id="codex-review-controls"/);
});


test('static click handlers have actual page controls so initialization can reach polling',()=>{
 const ids=new Set([...html.matchAll(/\bid="([^"]+)"/g)].map(x=>x[1]));
 for(const match of html.matchAll(/\$\('([^']+)'\)\.addEventListener/g))assert.ok(ids.has(match[1]),'missing UI control: '+match[1]);
 assert.match(html,/<button[^>]*id="configure-stage3"[^>]*>保存方案与 Key（不启动）<\/button>/);
 assert.doesNotMatch(html,/d1:free/);
});


test('current dispatch state clears an obsolete catalog captcha without changing model edits',()=>{
 const node={dataset:{}},context=vm.createContext({lastStatus:{category_key:'fixture',live_sample:{age_seconds:0},stage3_runtime:{paused:false,agent_blocks:{},paused_agents:['acciowork']},stage3_control:{codex_review:{pool:{running:true,agents:[{id:'doubao',running:8}]}}}}});
 vm.runInContext(code,context);
 vm.runInContext("stage3AgentCatalogs={doubao:{status:'unavailable',message:'豆包出现验证码'}};stage3AgentModels={doubao:'专家'}",context);
 context.node=node;vm.runInContext("renderAgentStatus(node,'doubao')",context);
 assert.match(node.textContent,/当前 8 组处理中/);assert.doesNotMatch(node.textContent,/验证码/);
 assert.equal(vm.runInContext('stage3AgentModels.doubao',context),'专家');
 context.lastStatus.stage3_runtime.agent_blocks.doubao={message:'请处理验证码'};
 vm.runInContext("renderAgentStatus(node,'doubao')",context);assert.match(node.textContent,/请处理验证码/);assert.match(node.className,/agent-block/);
 delete context.lastStatus.stage3_runtime.agent_blocks.doubao;context.lastStatus.stage3_runtime.paused_agents.push('doubao');
 vm.runInContext("renderAgentStatus(node,'doubao')",context);assert.match(node.textContent,/已暂停派发新组/);
});

test('an unavailable model catalog still identifies the saved model without enabling new selection',()=>{
 const options=[],select={dataset:{},replaceChildren(){options.length=0;},append(node){options.push(node);}};
 const context=vm.createContext({document:{createElement:()=>({})}});vm.runInContext(code,context);
 vm.runInContext("stage3AgentModels={doubao:'豆包 专家'};stage3AgentCatalogs={doubao:{status:'unavailable'}}",context);
 context.select=select;vm.runInContext("populateAgentModel(select,'doubao')",context);
 assert.equal(select.value,'豆包 专家');assert.equal(select.disabled,true);assert.match(options[0].textContent,/当前已保存：豆包 专家/);
});
