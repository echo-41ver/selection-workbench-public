import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';
const html=fs.readFileSync(new URL('../web/index.html',import.meta.url),'utf8');
const code=html.slice(html.indexOf('let stage3SettingsDirty='),html.indexOf('function businessSettings'));
function view(){
 const nodes=new Map();const create=()=>({value:'',dataset:{},children:[],style:{},append(...items){this.children.push(...items);},replaceChildren(){this.children=[];}});
 const get=id=>{if(!nodes.has(id))nodes.set(id,create());return nodes.get(id);};
 const c=vm.createContext({AbortController,setTimeout,clearTimeout,$:get,lastStatus:null,document:{createElement:create,querySelectorAll:()=>[]}});
 vm.runInContext(code,c);get('stage3-mode').value='local_agent';
 vm.runInContext("stage3AgentSelection=['codex'];stage3AgentModels={codex:'native'};stage3AgentCatalogs={codex:{status:'ready',reasoning_configurable:true,default:'native',models:[{id:'native',vision:true,efforts:['low','medium','high'],default_effort:'medium'},{id:'other',vision:true,efforts:['low'],default_effort:'low'}]}};renderAgentControls=()=>{};refreshConcurrencyDefault=()=>{};",c);
 return {c,get};
}
test('native reasoning levels and the explicit model default are shown',()=>{
 const {c,get}=view();vm.runInContext("populateAgentEffort($('effort'),'codex')",c);
 assert.deepEqual(get('effort').children.map(o=>o.value),['default','low','medium','high']);
 assert.match(get('effort').children[0].textContent,/模型默认：中.*medium/);
 assert.equal(get('effort').value,'default');assert.equal(get('effort').disabled,false);
 assert.match(html,/id="stage3-agent-effort"/);
});
test('each selected Agent sends its own strength and changing model restores default',()=>{
 const {c}=view();vm.runInContext("setAgentEffort('codex','high')",c);
 assert.equal(vm.runInContext('stage3ProviderSettings().agent_efforts.codex',c),'high');
 assert.equal(vm.runInContext('stage3AgentInputsReady()',c),true);
 vm.runInContext("stage3AgentEfforts.codex='ultra'",c);assert.equal(vm.runInContext('stage3AgentInputsReady()',c),false);
 vm.runInContext("setAgentModel('codex','other')",c);
 assert.equal(vm.runInContext('stage3ProviderSettings().agent_efforts.codex',c),'default');
 vm.runInContext("stage3AgentSelection.push('doubao');stage3AgentCatalogs.doubao={status:'ready',models:[]};stage3AgentEfforts.doubao='default';",c);
 const value=vm.runInContext('stage3ProviderSettings()',c);assert.equal(value.agent_efforts.codex,'default');assert.equal(value.agent_efforts.doubao,'default');
});
test('saved strength reloads and unsupported adapters show native default',()=>{
 const {c,get}=view();vm.runInContext("stage3AgentSelection=[];renderStage3Settings({category_key:'task',stage3_provider:{mode:'local_agent',base_url:'',model:'native',protocol:'responses',workers:1,confidence:.9,agent_ids:['codex'],agent_models:{codex:'native'},agent_efforts:{codex:'high'}}})",c);
 assert.equal(vm.runInContext('stage3AgentEfforts.codex',c),'high');
 vm.runInContext("stage3AgentCatalogs.doubao={status:'ready',models:[]};populateAgentEffort($('effort'),'doubao')",c);
 assert.equal(get('effort').value,'default');assert.equal(get('effort').disabled,true);
 assert.match(get('effort').children[0].textContent,/原生设置/);
});
test('an older busy backend keeps its existing payload until safe service update',()=>{
 const {c,get}=view();vm.runInContext("lastStatus={stage3_provider:{mode:'local_agent',agent_models:{codex:'native'}}};populateAgentEffort($('effort'),'codex')",c);
 assert.equal(get('effort').disabled,true);assert.match(get('effort').children[0].textContent,/更新后/);
 assert.equal(Object.hasOwn(vm.runInContext('stage3ProviderSettings()',c),'agent_efforts'),false);
});
