import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';

const html=fs.readFileSync(new URL('../web/index.html',import.meta.url),'utf8');
const code=html.slice(html.indexOf('function codexReviewPanel(data)'),html.indexOf('const excelOpening='));
function render(imported,target,allComplete=false,excelCurrent=false){
 const nodes=new Map();
 const get=id=>{
  if(!nodes.has(id))nodes.set(id,{style:{},setAttribute(name,value){this[name]=value;}});
  return nodes.get(id);
 };
 const ctx=vm.createContext({$:get});
 vm.runInContext(code,ctx);
 ctx.data={stage3_control:{progress:{codex_completed:5700,codex_different:5500,codex_unknown:199,codex_same:1},
  codex_review:{available:true,imported,target,expected_tasks:3,all_reviews_complete:allComplete,excel_current:excelCurrent}}};
 vm.runInContext('codexReviewPanel(data)',ctx);
 return nodes;
}

test('new batch progress does not become 100 percent from older valid reviews',()=>{
 const nodes=render(786,1384);
 assert.equal(nodes.get('codex-review-progress-fill').style.width,'56.8%');
 assert.match(nodes.get('codex-review-target').textContent,/786 \/ 1,384/);
 assert.match(nodes.get('codex-review-count').textContent,/5,700/);
});

test('finished reviews still show Excel pending until latest publication is delivered',()=>{
 const nodes=render(1384,1384,true,false);
 assert.equal(nodes.get('codex-review-progress-fill').style.width,'100%');
 assert.match(nodes.get('codex-review-target').textContent,/等待Excel更新/);
 assert.match(render(1384,1384,true,true).get('codex-review-target').textContent,/Excel已更新/);
});

test('API historical speed is not shown as the ETA for active free review',()=>{
 const nodes=new Map();
 const get=id=>{
  if(!nodes.has(id))nodes.set(id,{style:{},setAttribute(){}});
  return nodes.get(id);
 };
 const ctx=vm.createContext({$:get,statusFresh:true,busy:false,codexReviewPanel(){}});
 const buttonCode=html.slice(html.indexOf('function stage3Button(data)'),html.indexOf('function codexReviewPanel(data)'));
 vm.runInContext(buttonCode,ctx);
 ctx.data={category_key:'task',stage3_control:{running:false,codex_review:{running:true},
  progress:{total:11117,completed:10800,api_completed:5401,codex_completed:5399,pending_after_codex:317,
   rate_per_hour:9566,eta_seconds:120,rate_source:'current'}}};
 vm.runInContext('stage3Button(data)',ctx);
 assert.match(nodes.get('stage3-speed').textContent,/API 当前未运行/);
 assert.match(nodes.get('stage3-speed').textContent,/不用于估算本轮看图时间/);
 assert.doesNotMatch(nodes.get('stage3-speed').textContent,/9,566|2 分钟/);
});
