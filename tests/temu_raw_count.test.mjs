import test from 'node:test';import assert from 'node:assert/strict';
import fs from 'node:fs';import vm from 'node:vm';
import {createRawCounter,RAW_COUNT_VERSION} from '../scripts/temu_raw_count.mjs';
const cards=n=>Array.from({length:n},(_,i)=>({goods_id:String(i%17)}));
test('ten 40-card pages meet a 400 raw-card cap despite only 17 distinct goods',()=>{
 const counter=createRawCounter(400);
 for(let page=1;page<=10;page++){
  counter.observe(cards(page*40),'doc',page>1);
  assert.equal(counter.count,page*40);
  for(let scan=0;scan<4;scan++)counter.observe(cards(page*40),'doc');
  assert.equal(counter.count,page*40);
 }
 assert.equal(counter.observe(cards(440),'doc',true).count,400);
});
test('resume and a new document cannot recount previously consumed positions',()=>{
 const counter=createRawCounter(400,{version:RAW_COUNT_VERSION,count:240});
 for(const n of [40,80,120,160,200,240])counter.observe(cards(n),'newdoc',true);
 assert.equal(counter.count,240);counter.observe(cards(280),'newdoc',true);assert.equal(counter.count,280);
});
test('replacement pagination and overlapping virtualized windows count positions without ID dedup',()=>{
 const c=createRawCounter(400),list=(start,n)=>Array.from({length:n},(_,i)=>({goods_id:String(start+i)}));
 c.observe(list(0,40),'doc');c.observe(list(20,40),'doc',true);assert.equal(c.count,60);
 c.observe(list(20,40),'doc');assert.equal(c.count,60);
 c.observe(list(100,40),'doc',true);assert.equal(c.count,100);
});
test('collector scan counts before category filtering and limits the final page by positions',async()=>{
 const source=fs.readFileSync(new URL('../scripts/temu_flower_collect.mjs',import.meta.url),'utf8');
 const scan=source.slice(source.indexOf('async function scan('),source.indexOf('async function lightPageState('));
 const rows=[{goods_id:'1',title:'keyboard',card_text:'Local Warehouse',main_image_url:'https://example.org/a.jpg'}];
 const ctx=vm.createContext({rawCounter:createRawCounter(400),rawLoadRequested:false,
  products:new Map(),pendingProducts:new Map(),unclassifiedProducts:new Map(),filteredSevereOffTopic:new Map(),
  categoryRelevance:{strict:true,classify:()=>({state:'EXCLUDED'})},isSevereOffTopic:()=>true,hasLocalWarehouseLabel:()=>true,
  readLocalWarehouse:async()=>({}),confirmsLocalWarehouse:()=>true,localWarehouseFilter:{},inspectLocalWarehouse:()=>({}),inspectSalesSort:()=>({}),
  SORT_REQUEST:'default',initialProductsPreparing:false,extractScript:'fixture',now:()=>'',classify:()=>null,
  QUERY:'mouse',SAFE_SOURCE:'fixture',round:0,enforceConfiguredProductLimit:()=>0,
  execute:async()=>({rawCards:Array.from({length:400},()=>({goods_id:'1'})),rawDocumentId:'doc',rows,captured:[]})});
 vm.runInContext(scan,ctx);await vm.runInContext('scan()',ctx);
 assert.equal(ctx.rawCounter.count,400);assert.equal(ctx.products.size,0);assert.equal(ctx.filteredSevereOffTopic.size,1);
 const cap=createRawCounter(65);cap.observe(cards(40),'doc');const result=cap.observe(cards(80),'doc',true);
 assert.equal(result.count,65);
 assert.match(source,/rawCounter.count < MAX_PRODUCTS/);assert.match(source,/rawCounter.count >= MAX_PRODUCTS/);
 assert.doesNotMatch(source,/products.size < MAX_PRODUCTS|products.size >= MAX_PRODUCTS/);
});
