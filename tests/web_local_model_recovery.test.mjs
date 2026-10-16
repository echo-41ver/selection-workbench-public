import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';

const html=fs.readFileSync(new URL('../web/index.html',import.meta.url),'utf8');
const code=html.slice(html.indexOf('let stage3SettingsDirty='),html.indexOf('function businessSettings'));
function setup(fetch){
 const nodes=new Map(),get=id=>{if(!nodes.has(id))nodes.set(id,{value:'',checked:false,hidden:false,children:[],replaceChildren(){this.children=[];},append(x){this.children.push(x);}});return nodes.get(id);};
 const context=vm.createContext({AbortController,setTimeout,clearTimeout,$:get,fetch,lastStatus:null,document:{createElement:()=>({}),querySelectorAll:()=>[]}});
 vm.runInContext(code,context);vm.runInContext('refreshConcurrencyDefault=()=>{}',context);
 for(const [id,value] of Object.entries({'stage3-mode':'local_model','stage3-model':'','stage3-base-url':'http://127.0.0.1:11434/v1','stage3-protocol':'chat_completions','stage3-workers':'77','stage3-confidence':'90'}))get(id).value=value;
 return {context,get};
}
const detected={ok:true,message:'已识别实际模型',models:[{id:'actual-vision',name:'actual-vision',source:'Ollama',vision:true,base_url:'http://127.0.0.1:11435/v1',protocol:'chat_completions'}],provider:{model:'actual-vision',base_url:'http://127.0.0.1:11435/v1',protocol:'chat_completions'}};

test('local repair displays real model and protocol without changing manual concurrency',async()=>{
 const calls=[],{context,get}=setup(async(path,options)=>{calls.push({path,body:JSON.parse(options.body)});return {ok:true,json:async()=>detected};});
 await vm.runInContext('repairLocalModelConnection()',context);
 assert.equal(calls[0].path,'/api/local-models/repair');assert.equal(calls[0].body.provider.model,'');
 assert.equal(get('stage3-model').value,'actual-vision');assert.equal(get('stage3-base-url').value,'http://127.0.0.1:11435/v1');
 assert.equal(get('stage3-protocol').value,'chat_completions');assert.equal(get('stage3-workers').value,'77');
 assert.equal(get('stage3-local-model').children[1].textContent,'actual-vision · Ollama');
 assert.equal(calls.length,1);
});

test('a late detection cannot overwrite a manually selected model',async()=>{
 let resolve;const {context,get}=setup(()=>new Promise(r=>{resolve=r;}));
 const pending=vm.runInContext('repairLocalModelConnection()',context);get('stage3-model').value='manual-choice';
 resolve({ok:true,json:async()=>detected});await pending;
 assert.equal(get('stage3-model').value,'manual-choice');assert.match(get('stage3-local-status').textContent,/保留/);
});

test('a late local response cannot modify another analysis mode',async()=>{
 let resolve;const {context,get}=setup(()=>new Promise(r=>{resolve=r;}));
 const pending=vm.runInContext('repairLocalModelConnection()',context);get('stage3-mode').value='liquid_d1';get('stage3-model').value='d1';
 resolve({ok:true,json:async()=>detected});await pending;
 assert.equal(get('stage3-model').value,'d1');assert.equal(get('stage3-base-url').value,'http://127.0.0.1:11434/v1');
});
