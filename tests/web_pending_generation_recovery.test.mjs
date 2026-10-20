import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';

const html=fs.readFileSync(new URL('../web/index.html',import.meta.url),'utf8');
const recoveryCode=html.slice(html.indexOf('const taskScopeFieldIds='),html.indexOf('async function action(endpoint,body)'));
const draftCode=html.slice(html.indexOf('function renderDraft(draft)'),html.indexOf('function renderFrontendMarketDetails('));
const retryCode=html.split('\n').find(line=>line.startsWith("$('retry-generation').addEventListener"));
const submitCode=html.split('\n').find(line=>line.startsWith("$('task-form').addEventListener"));
const listing='https://www.amazon.com/Best-Sellers-Shower-Curtains/zgbs/home-garden/13749881';

function pending(overrides={}){
 return {category_key:null,active:false,can_retry_generation:true,
  scope:{category:'浴帘',amazon_count:1,per_keyword:1,worker_count:1,
   bestsellers_url:listing,commercial:{same_limit:3},...overrides}};
}

function setup(){
 const nodes=new Map();
 function node(id){
  if(!nodes.has(id))nodes.set(id,{id,value:'',hidden:false,children:[],listeners:{},
   addEventListener(type,callback){(this.listeners[type]??=[]).push(callback);},
   insertAdjacentElement(position,child){
    assert.equal(position,'afterend');
    if(child.parentElement)child.parentElement.children=child.parentElement.children.filter(n=>n!==child);
    child.parentElement=this.parentElement;
    this.parentElement.children.splice(this.parentElement.children.indexOf(this)+1,0,child);
   },removeAttribute(name){delete this[name];}});
  return nodes.get(id);
 }
 for(const [id,value] of Object.entries({category:'','bestsellers-url':'',amazon:'10',quantity:'100',
  'worker-count':'4','new-same-limit':'5','api-key':'','api-base-url':'https://api.deepseek.com',
  'api-model':'deepseek-flash','retry-listing-url':''}))node(id).value=value;
 const create=node('create-panel'),monitor=node('monitor-panel');monitor.hidden=true;
 node('task-form').parentElement=create;create.children.push(node('task-form'));
 node('listing-recovery').parentElement=monitor;node('listing-recovery').hidden=true;
 monitor.children.push(node('listing-recovery'));
 const requests=[];
 const context=vm.createContext({$:node,Number,JSON,String,URL,lastStatus:null,busy:false,
  draftId:null,keywordReady:false,applyingScope:null,plainApplied:'',plainApplied3:'',
  placeDraftEditor(){},renderKeywordPreview(){},action:(endpoint,body)=>requests.push({endpoint,body})});
 vm.runInContext(recoveryCode+'\n'+draftCode+'\n'+retryCode+'\n'+submitCode,context);
 function render(data){context.lastStatus=data;context.renderGenerationRecovery(data);}
 function input(id,value){node(id).value=String(value);for(const callback of node(id).listeners.input||[])callback();}
 function click(id){for(const callback of node(id).listeners.click||[])callback();}
 function submit(){for(const callback of node('task-form').listeners.submit||[])callback({preventDefault(){}});}
 function values(){return ['category','bestsellers-url','amazon','quantity','worker-count','new-same-limit'].map(id=>node(id).value);}
 return {node,context,requests,render,input,click,submit,values};
}

test('reload restores the exact pending sample and exposes the same retry control on create',()=>{
 const ui=setup();ui.render(pending());
 assert.deepEqual(ui.values(),['浴帘',listing,'1','1','1','3']);
 assert.equal(ui.node('retry-listing-url').value,listing);
 const retry=ui.node('listing-recovery');
 assert.equal(retry.hidden,false);
 assert.equal(retry.parentElement,ui.node('create-panel'));
 assert.equal(retry.parentElement.hidden,false);
 assert.equal(ui.node('monitor-panel').hidden,true);
 ui.render(pending());
 assert.equal(ui.node('create-panel').children.filter(n=>n.id==='listing-recovery').length,1);
 assert.equal(ui.requests.length,0);
});

test('a user entering another category before status arrives keeps the entire new scope',()=>{
 const ui=setup();ui.input('category','电钻');ui.input('bestsellers-url','https://www.amazon.com/zgbs/tools/123');
 ui.input('amazon',2);ui.input('quantity',4);ui.input('worker-count',2);ui.input('new-same-limit',7);
 const entered=ui.values();ui.render(pending());
 assert.deepEqual(ui.values(),entered);
 assert.equal(ui.node('retry-listing-url').value,listing);
 assert.equal(ui.requests.length,0);
});

test('later polls and an unrelated preserved draft cannot overwrite pending sample settings',()=>{
 const ui=setup();ui.render(pending());
 ui.context.renderDraft({id:'old-draft',category_name:'电钻',worker_count:4,
  commercial:{same_limit:9},keyword_plan:[]});
 assert.deepEqual(ui.values(),['浴帘',listing,'1','1','1','3']);
 ui.input('quantity',2);const entered=ui.values();
 ui.render(pending());
 ui.context.renderDraft({id:'another-old-draft',category_name:'浴帘',worker_count:8,
  commercial:{same_limit:10},keyword_plan:[]});
 assert.deepEqual(ui.values(),entered);
});

test('retry uses its pending scope and edited retry URL without altering an in-progress new form',()=>{
 const ui=setup();ui.render(pending());
 const newUrl='https://www.amazon.com/zgbs/tools/123',retryUrl=listing+'?ref=retry';
 ui.input('category','电钻');ui.input('bestsellers-url',newUrl);ui.input('amazon',2);
 ui.input('quantity',5);ui.input('worker-count',2);ui.input('new-same-limit',8);
 ui.input('retry-listing-url',retryUrl);ui.render(pending());
 const entered=ui.values();ui.click('retry-generation');
 assert.deepEqual(ui.values(),entered);
 assert.equal(ui.requests.length,1);
 const request=ui.requests[0];assert.equal(request.endpoint,'/api/generate');
 assert.equal(request.body.category_name,'浴帘');
 assert.equal(request.body.amazon_count,1);assert.equal(request.body.per_keyword,1);
 assert.equal(request.body.worker_count,1);assert.equal(request.body.commercial.same_limit,3);
 assert.equal(request.body.bestsellers_url,retryUrl);
});

test('a blank retry URL reuses the resolved pending URL without clearing the new task URL',()=>{
 const ui=setup();ui.render(pending());ui.input('retry-listing-url','');
 ui.input('category','电钻');ui.input('bestsellers-url','https://www.amazon.com/zgbs/tools/123');
 const entered=ui.values();ui.click('retry-generation');
 assert.equal(ui.requests[0].body.bestsellers_url,listing);
 assert.deepEqual(ui.values(),entered);
});

test('a different scope cannot silently increase a previously restored sample',()=>{
 const ui=setup();ui.render(pending());const restored=ui.values();
 ui.render(pending({amazon_count:10,per_keyword:100,worker_count:4}));
 assert.deepEqual(ui.values(),restored);
 ui.input('amazon',2);ui.input('quantity',3);ui.input('worker-count',2);
 ui.submit();
 ui.render(pending({amazon_count:2,per_keyword:3,worker_count:2}));
 assert.deepEqual(ui.values(),['浴帘',listing,'2','3','2','3']);
 assert.equal(ui.requests.length,1);
});

test('running tasks and malformed pending counts do not hydrate new-task fields',()=>{
 for(const data of [{...pending(),category_key:'running-shower'},pending({amazon_count:0}),
  pending({per_keyword:'100'}),pending({worker_count:33})]){
  const ui=setup(),before=ui.values();ui.render(data);assert.deepEqual(ui.values(),before);
 }
 const ui=setup();ui.render(pending({commercial:{same_limit:10001}}));
 assert.equal(ui.node('new-same-limit').value,'5');
});

test('existing current input values and user edits are not replaced by an unrelated draft',()=>{
 const ui=setup();ui.node('category').value='电钻';ui.node('bestsellers-url').value='https://www.amazon.com/zgbs/tools/123';
 const before=ui.values();ui.render(pending());assert.deepEqual(ui.values(),before);
 ui.context.lastStatus={category_key:null,scope:{}};ui.input('worker-count',2);
 ui.context.renderDraft({id:'old-draft',category_name:'电钻',worker_count:12,commercial:{same_limit:9}});
 assert.equal(ui.node('worker-count').value,'2');assert.equal(ui.node('new-same-limit').value,'5');
});

test('the complete inline browser script remains syntactically valid',()=>{
 for(const match of html.matchAll(/<script(?:\s[^>]*)?>([\s\S]*?)<\/script>/g))assert.doesNotThrow(()=>new vm.Script(match[1]));
});
