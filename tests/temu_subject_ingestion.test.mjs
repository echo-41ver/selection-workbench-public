import test from 'node:test';import assert from 'node:assert/strict';import fs from 'node:fs';import vm from 'node:vm';
import {createCategoryRelevance} from '../scripts/category_relevance.mjs';
import {hasLocalWarehouseLabel} from '../scripts/temu_local_warehouse.mjs';
const src=fs.readFileSync(new URL('../scripts/temu_flower_collect.mjs',import.meta.url),'utf8');
const scan=src.slice(src.indexOf('async function scan('),src.indexOf('async function lightPageState('));
function setup(rows){
 const policy=createCategoryRelevance({category_name_zh:'鼠标',temu:{subject_filter:{mode:'TARGET_BODY_ONLY',reject_mixed_subjects:true,exclude_subject_terms:['键盘']}}});
 const ctx=vm.createContext({categoryRelevance:policy,isSevereOffTopic:policy.isSevereOffTopic,hasLocalWarehouseLabel,
 readLocalWarehouse:async()=>({selected:true}),confirmsLocalWarehouse:s=>s.selected===true,inspectLocalWarehouse:()=>({}),inspectSalesSort:()=>({}),
 products:new Map(),pendingProducts:new Map(),unclassifiedProducts:new Map(),filteredSevereOffTopic:new Map(),
 localWarehouseFilter:{},SORT_REQUEST:'default',initialProductsPreparing:false,extractScript:'fixture',now:()=> '2026-10-13T00:00:00Z',classify:()=>null,
 QUERY:'mouse',SAFE_SOURCE:'https://www.temu.com/search_result.html?search_key=mouse',round:0,
 execute:async()=>({rows,captured:[]}),enforceConfiguredProductLimit:()=>0});vm.runInContext(scan,ctx);return ctx;
}
const row=(id,title)=>({goods_id:id,title,price_usd:3,product_url:'https://www.temu.com/item-g-'+id+'.html',main_image_url:'https://img.kwcdn.com/item.jpg',source:'VISIBLE_TEMU_FRONTEND',card_text:title+'\nLocal Warehouse'});

test('cards and required filter evidence are read together without extra driver requests',async()=>{
 const ctx=setup([]);let commands=0;
 ctx.extractScript='return {rows:[],captured:[]};';
 ctx.inspectLocalWarehouse=()=>({selected:true});ctx.inspectSalesSort=()=>({selected:true});
 ctx.readLocalWarehouse=async()=>assert.fail('must use the warehouse observation from the same document');
 ctx.readSalesSort=async()=>assert.fail('must use the sorting observation from the same document');
 ctx.SORT_REQUEST='sales';ctx.salesSortEstablished=true;ctx.salesSort={};ctx.confirmsSalesSort=s=>s.selected===true;
 ctx.execute=async script=>{commands++;return vm.runInNewContext(`(function(){${script}})()`);};
 await vm.runInContext('scan()',ctx);
 assert.equal(commands,1);assert.equal(ctx.salesSort.state,'UI_CONFIRMED');
});

test('new collection accepts local warehouse cards only and image backfill cannot promote excluded cards',async()=>{
 const ctx=setup([row('1','无线鼠标'),{...row('2','无线鼠标'),card_text:'无线鼠标\nFast delivery'},{...row('3','无线鼠标'),card_text:''}]);
 ctx.pendingProducts.set('4',{...row('4','无线鼠标'),card_text:'无线鼠标'});
 ctx.execute=async()=>({rows:[row('1','无线鼠标'),{...row('2','无线鼠标'),card_text:'无线鼠标'},{...row('3','无线鼠标'),card_text:''}],captured:[{goods_id:'4',images:['https://img.kwcdn.com/new.jpg']}]});
 await vm.runInContext('scan()',ctx);assert.deepEqual([...ctx.products.keys()],['1']);
 assert.equal(ctx.pendingProducts.has('4'),true);
});

test('old checkpoints stay intact and a late warehouse badge permits a later collection scan',async()=>{
 const rows=[{...row('1','无线鼠标'),card_text:'无线鼠标'}],ctx=setup(rows);
 ctx.products.set('legacy',{...row('legacy','无线鼠标'),card_text:'旧采集记录'});
 await vm.runInContext('scan()',ctx);assert.deepEqual([...ctx.products.keys()],['legacy']);
 rows[0].card_text+='\n本地仓';await vm.runInContext('scan()',ctx);
 assert.deepEqual([...ctx.products.keys()],['legacy','1']);
});
test('a page transition reapplies the filter and never commits pre-filter rows',async()=>{
 const ctx=setup([row('1','无线鼠标')]);let selected=false,preparations=0;
 ctx.readLocalWarehouse=async()=>({selected});ctx.bootstrapPageState=async()=>({});
 ctx.prepareLocalWarehouseFilter=async()=>{preparations++;selected=true;};
 await vm.runInContext('scan()',ctx);assert.equal(ctx.products.size,0);assert.equal(preparations,1);
 await vm.runInContext('scan()',ctx);assert.equal(ctx.products.size,1);assert.equal(preparations,1);
});
test('ingestion only commits target body and routes keyboard or incomplete model outside product data',async()=>{
 const ctx=setup([row('1','无线鼠标'),row('2','键盘鼠标套装'),row('3','X3MAX')]);await vm.runInContext('scan()',ctx);
 assert.deepEqual([...ctx.products.keys()],['1']);assert.deepEqual([...ctx.filteredSevereOffTopic.keys()],['2']);assert.deepEqual([...ctx.unclassifiedProducts.keys()],['3']);
});
test('a better observation promotes quarantined title and explicit correction removes a former product',async()=>{
 const ctx=setup([row('1','无线鼠标'),row('2','键盘')]);ctx.unclassifiedProducts.set('1',row('1','X3MAX'));ctx.products.set('2',row('2','鼠标'));
 await vm.runInContext('scan()',ctx);assert.deepEqual([...ctx.products.keys()],['1']);assert.equal(ctx.unclassifiedProducts.size,0);assert.equal(ctx.filteredSevereOffTopic.size,1);
});
