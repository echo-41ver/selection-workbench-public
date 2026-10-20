import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';
const html=fs.readFileSync(new URL('../web/index.html',import.meta.url),'utf8');
const helpers=html.slice(html.indexOf('function statusTone('),html.indexOf('let statusFresh='));
const first=(row,keys)=>keys.map(k=>row?.[k]).find(v=>v!==undefined&&v!==null&&v!=='');

test('rule editor stays on the creation page and preserves edits across navigation',()=>{
 const code=html.slice(html.indexOf('function placeDraftEditor('),html.indexOf('function renderDraft('));
 const editor={value:'unsaved user rules'},create={hidden:true,append(n){n.parentElement=this;}},monitor={append(n){n.parentElement=this;}};
 const context=vm.createContext({$:id=>({'draft-editor':editor,'create-panel':create,'monitor-panel':monitor}[id])});
 vm.runInContext(code,context);vm.runInContext('placeDraftEditor()',context);
 assert.equal(editor.parentElement,create);assert.equal(editor.value,'unsaved user rules');
 create.hidden=false;vm.runInContext('placeDraftEditor()',context);
 assert.equal(editor.parentElement,create);assert.equal(editor.value,'unsaved user rules');
 create.hidden=true;vm.runInContext('placeDraftEditor()',context);assert.equal(editor.parentElement,create);
 assert.equal((html.match(/id="draft-editor"/g)||[]).length,1);
});
test('pending startup navigation remains compatible with startup snapshots',()=>{
 const code=html.slice(html.indexOf('function finishRunNavigation('),html.indexOf('function renderDraft('));
 const pages=[],context=vm.createContext({pendingRunNavigation:true,showPage:page=>pages.push(page)});
 vm.runInContext(code,context);
 context.data={console_busy:true,category_key:null,draft:{id:'draft'}};vm.runInContext('finishRunNavigation(data)',context);
 assert.deepEqual(pages,[]);assert.equal(context.pendingRunNavigation,true);
 context.data={console_busy:false,category_key:null,draft:{id:'draft'}};vm.runInContext('finishRunNavigation(data)',context);
 assert.deepEqual(pages,[]);assert.equal(context.pendingRunNavigation,false);
 context.pendingRunNavigation=true;context.data={console_busy:false,category_key:'task',draft:null};vm.runInContext('finishRunNavigation(data)',context);
 assert.deepEqual(pages,['monitor']);
});
test('new task threshold has no less-than-or-equal option and always submits strict less-than',()=>{
 assert.doesNotMatch(html,/<option value="lte">/);
 const code=html.slice(html.indexOf('function businessSettings()'),html.indexOf('function renderBusinessDetails('));
 const context=vm.createContext({document:{querySelectorAll:()=>[{id:'biz-same_operator',value:'lte'},{id:'biz-same_limit',value:'5'}]}});
 vm.runInContext(code,context);const result=vm.runInContext('businessSettings()',context);
 assert.equal(result.same_operator,'lt');assert.equal(result.same_limit,5);
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
 context.data={active:true,console_busy:true,category_key:null,workers:[],generation:{stage:'complete'},stage3_control:{running:false}};
 vm.runInContext('nextStep(data)',context);assert.match(node.textContent,/正在准备规则与 Amazon 关键词/);assert.doesNotMatch(node.textContent,/正在采集并自动/);
});
