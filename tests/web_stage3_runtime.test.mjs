import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';
const html=fs.readFileSync(new URL('../web/index.html',import.meta.url),'utf8');
const code=html.slice(html.indexOf('function stage3Button(data)'),html.indexOf('function codexReviewPanel(data)'));
function view(){const nodes=new Map(),get=id=>{if(!nodes.has(id))nodes.set(id,{value:'liquid_d1',style:{},replaceChildren(){},setAttribute(){}});return nodes.get(id);};const context=vm.createContext({$:get,statusFresh:true,busy:false,codexReviewPanel(){},renderStage3Settings(){},stage3ProviderSettings:()=>undefined});vm.runInContext(html.slice(html.indexOf('function stage3ActivityText('),html.indexOf('function runtimeStage3('))+code,context);return {get,context};}
test('initial page before the first status response initializes controls',()=>{const {get,context}=view();assert.doesNotThrow(()=>vm.runInContext('stage3Button(null)',context));assert.equal(get('start-stage3').disabled,true);});

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

test('selected verification gate exposes Continue even after global pause was cleared',()=>{const {get,context}=view();context.data={category_key:'task',stage3_provider:{mode:'local_agent',agent_ids:['doubao']},stage3_control:{running:false,progress:{total:24,pending:12}},stage3_runtime:{paused:false,agent_blocks:{doubao:{code:'HUMAN_VERIFICATION_REQUIRED'}}}};vm.runInContext('stage3Button(data)',context);assert.equal(get('pause-stage3').disabled,false);assert.equal(get('pause-stage3').textContent,'继续第三层');assert.equal(vm.runInContext('stage3ResumeRequired(data)',context),true);context.data.stage3_provider.agent_ids=['codex'];assert.equal(vm.runInContext('stage3ResumeRequired(data)',context),false);});
