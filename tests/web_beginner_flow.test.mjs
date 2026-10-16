import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';
const html=fs.readFileSync(new URL('../web/index.html',import.meta.url),'utf8');
const helpers=html.slice(html.indexOf('function statusTone('),html.indexOf('let statusFresh='));
const first=(row,keys)=>keys.map(k=>row?.[k]).find(v=>v!==undefined&&v!==null&&v!=='');

test('one rule editor follows the current page without resetting unsaved edits',()=>{
 const code=html.slice(html.indexOf('function placeDraftEditor('),html.indexOf('function renderDraft('));
 const editor={value:'unsaved user rules'},create={hidden:true,append(n){n.parentElement=this;}},monitor={append(n){n.parentElement=this;}};
 const context=vm.createContext({$:id=>({'draft-editor':editor,'create-panel':create,'monitor-panel':monitor}[id])});
 vm.runInContext(code,context);vm.runInContext('placeDraftEditor()',context);
 assert.equal(editor.parentElement,monitor);assert.equal(editor.value,'unsaved user rules');
 create.hidden=false;vm.runInContext('placeDraftEditor()',context);
 assert.equal(editor.parentElement,create);assert.equal(editor.value,'unsaved user rules');
 create.hidden=true;vm.runInContext('placeDraftEditor()',context);assert.equal(editor.parentElement,monitor);
 assert.equal((html.match(/id="draft-editor"/g)||[]).length,1);
});
test('errors and human-action waits red, automatic waits green',()=>{
 const context=vm.createContext({first});vm.runInContext(helpers,context);
 const cases=[['RUNNING','success'],['WORKER_COMPLETE','success'],['WAITING_CAPTCHA','danger'],['WAITING_HUMAN_VERIFICATION','danger'],['STOPPED_LOGIN_REQUIRED','success'],['BINDING_FAILED','danger'],['FAILED_AFTER_3_ATTEMPTS','danger'],['STOPPED_PROXY_CONNECTION','danger'],['WAITING_INITIAL_PRODUCTS','success'],['WAITING_POST_VERIFICATION_SEARCH_BOX','success'],['REPLACING_LOGIN_ENVIRONMENT','success'],['尚未启动',''],['已停止','']];
 for(const [status,expected] of cases){context.row={status};assert.equal(vm.runInContext('statusTone(row)',context),expected,status);}
});
test('local redraw cannot turn a disconnected snapshot into current status',()=>{
 const render=html.slice(html.indexOf('function render(data)'),html.indexOf('\nlet polling='));
 assert.doesNotMatch(render,/statusFresh\s*=\s*true/);
 const filter=html.split('\n').find(x=>x.startsWith("$('only-errors').addEventListener"));
 let callback,calls=0;const context=vm.createContext({$:()=>({addEventListener:(_type,fn)=>callback=fn}),statusFresh:false,lastStatus:{active:false},render:()=>calls++});
 vm.runInContext(filter,context);callback();assert.equal(calls,0);context.statusFresh=true;callback();assert.equal(calls,1);
});
test('instructions distinguish finished generation from collection and layer3',()=>{
 const code=html.slice(html.indexOf('function nextStep(data)'),html.indexOf('const secretKey='));
 const node={};const context=vm.createContext({$:()=>node,statusTone:()=>'',workerCardRow:x=>x,attentionItems:()=>[]});vm.runInContext(code,context);
 context.data={active:true,category_key:'new',workers:[],generation:{stage:'complete'},stage3_control:{running:false}};
 vm.runInContext('nextStep(data)',context);assert.match(node.textContent,/正在采集/);assert.doesNotMatch(node.textContent,/正在准备/);
 context.data.stage3_control.running=true;vm.runInContext('nextStep(data)',context);assert.match(node.textContent,/第三层图片分析/);
});
