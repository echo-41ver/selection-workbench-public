import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';

const html=fs.readFileSync(new URL('../web/index.html',import.meta.url),'utf8');
const code=html.slice(html.indexOf('let stage3SettingsDirty='),html.indexOf('function businessSettings'));
function setup(fetch){
 const nodes=new Map(),get=id=>{if(!nodes.has(id))nodes.set(id,{value:'',checked:false,hidden:false,children:[],replaceChildren(){this.children=[];},append(...items){for(const item of items){item.parentElement=this;this.children.push(item);}}});return nodes.get(id);};
 const context=vm.createContext({AbortController,setTimeout,clearTimeout,$:get,fetch,lastStatus:null,document:{createElement:()=>({children:[],append(...items){this.children.push(...items);}}),querySelectorAll:()=>[]}});
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
 assert.equal(get('stage3-local-model').children[1].textContent,'actual-vision · 已连接');
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

test('whole-machine inventory keeps files and unknown models out of callable choices',()=>{
 const {context,get}=setup(()=>assert.fail('Rendering must not call a model'));
 context.inventory={status:'complete',running:false,directories:15,files:30,skipped:2,message:'扫描完成',
  models:[{name:'image-model.gguf',path:'D:/models/image-model.gguf',source:'GGUF',vision:true,status:'files_only'}],
  services:{models:detected.models,candidates:[{name:'unknown-vl',source:'API',vision:null,base_url:'http://127.0.0.1:8000/v1'}]}};
 vm.runInContext('renderLocalModelScan(inventory)',context);
 assert.equal(get('stage3-local-model').children.length,2);
 assert.equal(get('stage3-local-inventory-list').children.length,3);
 assert.equal(get('stage3-local-inventory-list').children[1].children[0].children[1].textContent,'需要确认');
 assert.equal(get('stage3-local-inventory-list').children[2].children[0].children[1].textContent,'尚未连接');
 assert.match(get('stage3-local-scan-status').textContent,/3 种模型，1 种已连接/);
 assert.equal(get('stage3-local-scan-stop').hidden,true);
});

test('ordinary model cards hide hashes, ports, paths, duplicate files and test data',()=>{
 const {context,get}=setup(()=>assert.fail('Rendering must not run inference'));
 context.inventory={status:'complete',running:false,current_directory:'C:/private/path',directories:1000,files:4000,
  services:{models:[{id:'qwen2.5vl:3b',display_name:'通义千问 2.5 视觉版（3B）',vision:true,source:'Ollama',base_url:'http://127.0.0.1:11434/v1',connections:[{id:'llamacpp:'+'a'.repeat(64),base_url:'http://127.0.0.1:60300/v1'}]}],services:[{source:'Ollama',base_url:'http://127.0.0.1:11434'}]},
  models:[{name:'d1-3B-Q8_0.gguf',display_name:'D1（3B）',path:'D:/AI/models/d1-3B-Q8_0.gguf',status:'files_only'},
   {name:'hash',path:'C:/cache/models--openai--clip-vit-base-patch32/snapshot',auxiliary:true,status:'files_only'},
   {name:'real.gguf',path:'C:/Temp/pytest-of-admin/pytest-100/real.gguf',test_fixture:true,status:'files_only'},
   {name:'qwen-file',source:'Ollama 文件',path:'C:/models/manifest',status:'files_only'}]};
 vm.runInContext('renderLocalModelScan(inventory)',context);
 assert.equal(get('stage3-local-model').children[1].textContent,'通义千问 2.5 视觉版（3B） · 已连接');
 const cards=get('stage3-local-inventory-list').children;
 assert.equal(cards.length,2);assert.equal(cards[1].children[0].children[0].textContent,'D1（3B）');
 assert.equal(cards[1].children[0].children[1].textContent,'尚未连接');
 assert.doesNotMatch(get('stage3-local-scan-status').textContent,/private|1000|4000/);
 assert.match(get('stage3-local-technical').textContent,/llamacpp:|60300/);
 vm.runInContext('stage3ProviderFields()',context);
 assert.equal(get('stage3-connection-fields').parentElement,get('stage3-local-connection'));
 get('stage3-mode').value='multimodal_api';vm.runInContext('stage3ProviderFields()',context);
 assert.equal(get('stage3-connection-fields').parentElement,get('stage3-connection-home'));
});

test('whole-machine scanning is background-only and does not alter the selected provider',async()=>{
 const calls=[],{context,get}=setup(async(path,options)=>{calls.push({path,body:JSON.parse(options.body)});return {ok:true,json:async()=>({ok:true,status:'complete',models:[],services:{models:detected.models}})};});
 get('stage3-model').value='manual-choice';
 await vm.runInContext('startLocalModelScan(true)',context);
 assert.equal(calls[0].path,'/api/local-models/scan');assert.equal(calls[0].body.restart,true);
 assert.equal(get('stage3-model').value,'manual-choice');assert.equal(get('stage3-base-url').value,'http://127.0.0.1:11434/v1');
 assert.equal(calls.length,1);
});
