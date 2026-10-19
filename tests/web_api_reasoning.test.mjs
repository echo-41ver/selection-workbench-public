import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';

const html=fs.readFileSync(new URL('../web/index.html',import.meta.url),'utf8');
const code=html.slice(html.indexOf("let stage3ApiReasoningValue="),html.indexOf('function stage3ProviderFields('));
function setup(){
 const node=()=>({value:'',children:[],append(child){this.children.push(child);},replaceChildren(){this.children=[];}});
 const nodes=new Map(),get=id=>{if(!nodes.has(id))nodes.set(id,node());return nodes.get(id);};
 for(const [id,value] of Object.entries({'stage3-mode':'multimodal_api','stage3-base-url':'https://models.example/v1','stage3-model':'vision-test','stage3-protocol':'responses','stage3-api-key':'TEST_ONLY_KEY','stage3-workers':'4','stage3-confidence':'90'}))get(id).value=value;
 const requests=[],context=vm.createContext({$:get,document:{createElement:node},lastStatus:{api_reasoning_supported:true},
  stage3AgentSelection:[],stage3AgentModels:{},stage3AgentEfforts:{},reasoningSettingsSupported:()=>true,
  AbortController,setTimeout,clearTimeout,fetch:async(url,options)=>{requests.push({url,body:JSON.parse(options.body)});return {ok:true,json:async()=>({catalog:{status:'ready',efforts:['low','high'],default_effort:'low',message:'接口已返回档位'}})};}});
 vm.runInContext(code,context);return {get,context,requests};
}

test('API strengths come only from metadata, with an explicit native default',async()=>{
 const {get,context,requests}=setup();await vm.runInContext('refreshApiReasoning()',context);
 assert.deepEqual(get('stage3-api-effort').children.map(o=>o.value),['default','low','high']);
 assert.match(get('stage3-api-effort').children[0].textContent,/默认.*低.*low/);
 assert.equal(get('stage3-api-effort').value,'default');
 assert.equal(requests[0].url,'/api/stage3/api-reasoning');
 assert.equal(requests[0].body.api_key,'TEST_ONLY_KEY');
 vm.runInContext("stage3ApiReasoningValue='high'",context);
 assert.equal(vm.runInContext('stage3ProviderSettings().reasoning_effort',context),'high');
 context.lastStatus={};
 assert.equal(vm.runInContext('stage3ProviderSettings().reasoning_effort',context),undefined);
});

test('a late reply for the previous model cannot repopulate its strengths',async()=>{
 const {get,context}=setup();let respond;
 context.fetch=()=>new Promise(resolve=>{respond=resolve;});
 const pending=vm.runInContext('refreshApiReasoning()',context);
 get('stage3-model').value='new-model';vm.runInContext('resetApiReasoning()',context);
 respond({ok:true,json:async()=>({catalog:{status:'ready',efforts:['max']}})});await pending;
 assert.equal(vm.runInContext('stage3ApiReasoningCatalog',context),null);
 assert.equal(vm.runInContext('stage3ApiReasoningValue',context),'default');
});

test('unreported support keeps default and failed refresh preserves an explicit saved choice',async()=>{
 const {get,context}=setup();context.fetch=async()=>({ok:true,json:async()=>({catalog:{status:'unreported',efforts:[],message:'接口未提供档位'}})});
 await vm.runInContext('refreshApiReasoning()',context);
 assert.deepEqual(get('stage3-api-effort').children.map(o=>o.value),['default']);
 assert.equal(get('stage3-api-effort').disabled,true);
 vm.runInContext("stage3ApiReasoningValue='high';renderApiReasoning()",context);
 assert.equal(get('stage3-api-effort').value,'high');
 assert.equal(get('stage3-api-effort').disabled,false);
 assert.equal(vm.runInContext('apiReasoningReady()',context),false);
});

test('editing credentials waits for a completed change instead of sending partial text',async()=>{
 const {get,context,requests}=setup();context.document.activeElement={id:'stage3-api-key'};
 await vm.runInContext('refreshApiReasoning()',context);assert.equal(requests.length,0);
 await vm.runInContext('refreshApiReasoning(true)',context);assert.equal(requests.length,1);
 get('stage3-mode').value='local_agent';vm.runInContext('renderApiReasoning()',context);
 assert.equal(get('stage3-api-effort-box').hidden,true);
});

test('models load without first entering a model ID and declare image compatibility',async()=>{
 const {get,context,requests}=setup();get('stage3-model').value='';
 context.fetch=async(url,options)=>{requests.push(JSON.parse(options.body));return {ok:true,json:async()=>({
  catalog:{status:'unreported',efforts:[]},inventory:{status:'ready',models:[
   {id:'vision-model',name:'Vision',vision:true,reasoning:{status:'ready',efforts:['high','max']}},
   {id:'text-model',vision:false,reasoning:{status:'ready',efforts:['high']}}]}})};};
 await vm.runInContext('refreshApiReasoning()',context);
 assert.equal(requests.length,1);
 assert.deepEqual(get('stage3-api-model').children.map(o=>o.value),['','vision-model','text-model']);
 assert.equal(get('stage3-api-model').children[2].disabled,true);
 assert.match(get('stage3-api-model').children[2].textContent,/不支持图片/);
 assert.equal(vm.runInContext('apiReasoningReady()',context),false);
 get('stage3-model').value='vision-model';
 assert.equal(vm.runInContext('apiReasoningReady()',context),true);
});
