import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';
const html=fs.readFileSync(new URL('../web/index.html',import.meta.url),'utf8');
const code=html.slice(html.indexOf('function stage3Button(data)'),html.indexOf('function codexReviewPanel(data)'));
function view(){const nodes=new Map(),get=id=>{if(!nodes.has(id))nodes.set(id,{value:'liquid_d1',dataset:{},style:{},replaceChildren(){},setAttribute(){}});return nodes.get(id);};const context=vm.createContext({$:get,statusFresh:true,busy:false,codexReviewPanel(){},renderStage3Settings(){},stage3ProviderSettings:()=>undefined});vm.runInContext(html.slice(html.indexOf('function stage3ActivityText('),html.indexOf('function runtimeStage3('))+code,context);return {get,context};}
test('initial page before the first status response initializes controls',()=>{const {get,context}=view();assert.doesNotThrow(()=>vm.runInContext('stage3Button(null)',context));assert.equal(get('start-stage3').disabled,true);});

test('native model summary and concurrency help use the backend batch capacity instead of twelve',()=>{
 const {get,context}=view();context.localModelOptions=[];context.localModelLabel=row=>row.name;
 context.data={category_key:'task',stage3_provider:{mode:'local_model',model:'LiquidAI/d1-3B'},
  stage3_control:{running:false,progress:{total:24,pending:24}},stage3_runtime:{}};
 for(const limit of [10,5,1]){
  context.data.stage3_batch_capacity={ready:true,max_pairs:limit};vm.runInContext('stage3Button(data)',context);
  assert.match(get('stage3-current').textContent,new RegExp('每组最多 '+limit+' 个 TEMU'));
  assert.match(get('stage3-batch-hint').textContent,new RegExp('每组最多 '+limit+' 个 TEMU'));
  assert.doesNotMatch(get('stage3-current').textContent,/每组最多 12/);
 }
});

test('missing or blocked capacity is shown honestly without inventing a batch maximum',()=>{
 const {get,context}=view();context.data={category_key:'task',stage3_control:{progress:{total:1,pending:1}}};
 vm.runInContext('stage3Button(data)',context);assert.match(get('stage3-current').textContent,/每组容量待读取/);
 context.data.stage3_batch_capacity={ready:false,max_pairs:0,message:'单个商品对需要 11 题，当前模型不能承载完整规则'};
 vm.runInContext('stage3Button(data)',context);
 assert.match(get('stage3-current').textContent,/11 题/);assert.match(get('stage3-batch-hint').textContent,/完整规则/);
 assert.doesNotMatch(get('stage3-current').textContent,/每组最多 (?:0|12)/);
});

test('Agent speed and ETA use Agent groups instead of API history',()=>{
 const {get,context}=view();context.data={category_key:'task',stage3_control:{running:false,
  codex_review:{running:true,speed:{recent_rate_per_hour:120,window_seconds:900,estimated_rate_per_hour:240,sample_groups:10}},
  progress:{total:500,completed:20,pending_after_codex:480,rate_per_hour:99999,eta_seconds:1}}};
 vm.runInContext('stage3Button(data)',context);
 assert.match(get('stage3-speed').textContent,/速度 120 对.*近 15 分钟/);
 assert.match(get('stage3-speed').textContent,/预计剩余 2 小时 0 分钟/);
 assert.doesNotMatch(get('stage3-speed').textContent,/99,999/);
 context.data.stage3_control.codex_review.speed.recent_rate_per_hour=0;
 vm.runInContext('stage3Button(data)',context);assert.match(get('stage3-speed').textContent,/速度 0 对/);
});

test('running concurrency remains editable when models or candidate counts are unavailable',()=>{
 const {get,context}=view();get('stage3-mode').value='local_agent';context.stage3AgentInputsReady=()=>false;
 context.data={category_key:'task',release_update:{pending:true},stage3_control:{running:true,progress:{counting:true}},stage3_runtime:{}};
 vm.runInContext('stage3Button(data)',context);
 assert.equal(get('start-stage3').disabled,true);assert.equal(get('apply-stage3-concurrency').disabled,false);
 context.data.stage3_runtime.switch_pending=true;vm.runInContext('stage3Button(data)',context);
 assert.equal(get('apply-stage3-concurrency').disabled,true);
});

test('human verification is visible instead of a generic running phase',()=>{const {get,context}=view();context.data={category_key:'task',stage3_provider:{mode:'local_agent',agent_ids:['doubao']},stage3_control:{running:false,can_start:false,phase:'已暂停',progress:{total:24,pending:12}},stage3_runtime:{agent_blocks:{doubao:{code:'HUMAN_VERIFICATION_REQUIRED',message:'豆包出现验证码，请本人完成验证后继续'}}}};vm.runInContext('stage3Button(data)',context);assert.match(get('stage3-phase').textContent,/验证码/);assert.match(get('start-stage3').textContent,/等待处理/);assert.match(get('stage3-speed').textContent,/新组派发已暂停/);assert.doesNotMatch(get('stage3-speed').textContent,/正在进行/);});
test('paused running analysis exposes resume while ordinary running disables duplicate start',()=>{const {get,context}=view();context.data={category_key:'task',stage3_control:{running:true,can_start:true,progress:{total:24,pending:12}},stage3_runtime:{paused:true}};vm.runInContext('stage3Button(data)',context);assert.equal(get('start-stage3').disabled,false);assert.match(get('start-stage3').textContent,/继续/);context.data.stage3_runtime.paused=false;vm.runInContext('stage3Button(data)',context);assert.equal(get('start-stage3').disabled,true);});

for(const [running,inputError] of [[false,false],[true,false],[false,true]])test(`${inputError?'input failure':running?'paused active':'paused stopped'} analysis distinguishes applying a changed draft from resuming the saved provider`,()=>{
 const nodes=new Map(),requests=[];
 const node=()=>({value:'',checked:false,dataset:{},style:{},replaceChildren(){},setAttribute(){},
  before(child){nodes.set(child.id,child);},addEventListener(event,callback){this[event]=callback;}});
 const get=id=>{if(id==='pause-stage3'&&!nodes.has(id))return undefined;if(!nodes.has(id))nodes.set(id,node());return nodes.get(id);};
 const context=vm.createContext({$:get,document:{createElement:node},statusFresh:true,busy:false,
  codexReviewPanel(){},renderStage3Settings(){},reasoningSettingsSupported:()=>false,apiReasoningReady:()=>true,
  stage3AgentSelection:[],stage3AgentModels:{},stage3AgentEfforts:{},stage3SettingsDirty:true,
  action:(path,body)=>requests.push(JSON.parse(JSON.stringify({path,body})))});
 vm.runInContext(html.slice(html.indexOf('function stage3ActivityText('),html.indexOf('function runtimeStage3('))+code,context);
 vm.runInContext(html.slice(html.indexOf('function stage3ProviderSettings(){'),html.indexOf('function stage3ProviderFields(){')),context);
 vm.runInContext(html.split('\n').find(line=>line.startsWith("$('start-stage3').addEventListener")),context);
 for(const [id,value] of Object.entries({'stage3-mode':'multimodal_api','stage3-base-url':'https://api.example.invalid',
  'stage3-model':'saved-api-model','stage3-protocol':'responses','stage3-workers':'1','stage3-confidence':'90'}))get(id).value=value;
 const saved=vm.runInContext('stage3ProviderSettings()',context);
 context.data=context.lastStatus={category_key:'task',stage3_provider:saved,
  stage3_control:{running,can_start:true,can_stop:running,progress:{total:10,pending:10},...(inputError?{error:true,error_code:'CURRENT_INPUT_UNAVAILABLE'}:{})},stage3_runtime:{paused:!inputError}};
 vm.runInContext('stage3Button(data)',context);
 assert.match(get('start-stage3').textContent,/所选方案/);assert.equal(get('pause-stage3').textContent,inputError?'重试当前方案':'恢复当前方案');
 assert.notEqual(get('start-stage3').textContent,get('pause-stage3').textContent);
 // Changing the controls is only a draft. Both actual click routes must remain distinct.
 get('stage3-mode').value='local_model';get('stage3-base-url').value='http://127.0.0.1:11434/v1';
 get('stage3-model').value='LiquidAI/d1-3B';get('stage3-protocol').value='decisions';
 vm.runInContext('stage3Button(data)',context);
 assert.equal(requests.length,0);
 assert.equal(get('start-stage3').disabled,false);assert.equal(get('pause-stage3').disabled,false);
 assert.match(get('start-stage3').textContent,/所选方案/);assert.equal(get('pause-stage3').textContent,inputError?'重试当前方案':'恢复当前方案');
 assert.notEqual(get('start-stage3').textContent,get('pause-stage3').textContent);
 assert.match(get('stage3-current').textContent,/saved-api-model/);assert.doesNotMatch(get('stage3-current').textContent,/LiquidAI/);
 get('start-stage3').click();assert.equal(requests[0].path,'/api/start-stage3');
 assert.equal(requests[0].body.provider.mode,'local_model');assert.equal(requests[0].body.provider.model,'LiquidAI/d1-3B');
 get('pause-stage3').click();assert.deepEqual(requests[1],{path:'/api/control-stage3',body:{action:'resume'}});
});

test('selected verification gate can restore the saved provider even after global pause was cleared',()=>{const {get,context}=view();context.data={category_key:'task',stage3_provider:{mode:'local_agent',agent_ids:['doubao']},stage3_control:{running:false,progress:{total:24,pending:12}},stage3_runtime:{paused:false,agent_blocks:{doubao:{code:'HUMAN_VERIFICATION_REQUIRED'}}}};vm.runInContext('stage3Button(data)',context);assert.equal(get('pause-stage3').disabled,false);assert.equal(get('pause-stage3').textContent,'恢复当前方案');assert.equal(vm.runInContext('stage3ResumeRequired(data)',context),true);context.data.stage3_provider.agent_ids=['codex'];assert.equal(vm.runInContext('stage3ResumeRequired(data)',context),false);});
