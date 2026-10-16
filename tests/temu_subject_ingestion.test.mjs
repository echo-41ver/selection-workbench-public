import test from 'node:test';import assert from 'node:assert/strict';import fs from 'node:fs';import vm from 'node:vm';
import {createCategoryRelevance} from '../scripts/category_relevance.mjs';
const src=fs.readFileSync(new URL('../scripts/temu_flower_collect.mjs',import.meta.url),'utf8');
const scan=src.slice(src.indexOf('async function scan('),src.indexOf('async function lightPageState('));
function setup(rows){
 const policy=createCategoryRelevance({category_name_zh:'鼠标',temu:{subject_filter:{mode:'TARGET_BODY_ONLY',reject_mixed_subjects:true,exclude_subject_terms:['键盘']}}});
 const ctx=vm.createContext({categoryRelevance:policy,isSevereOffTopic:policy.isSevereOffTopic,
 products:new Map(),pendingProducts:new Map(),unclassifiedProducts:new Map(),filteredSevereOffTopic:new Map(),
 SORT_REQUEST:'default',initialProductsPreparing:false,extractScript:'fixture',now:()=> '2026-10-13T00:00:00Z',classify:()=>null,
 QUERY:'mouse',SAFE_SOURCE:'https://www.temu.com/search_result.html?search_key=mouse',round:0,
 execute:async()=>({rows,captured:[]}),enforceConfiguredProductLimit:()=>0});vm.runInContext(scan,ctx);return ctx;
}
const row=(id,title)=>({goods_id:id,title,price_usd:3,product_url:'https://www.temu.com/item-g-'+id+'.html',main_image_url:'https://img.kwcdn.com/item.jpg'});
test('ingestion only commits target body and routes keyboard or incomplete model outside product data',async()=>{
 const ctx=setup([row('1','无线鼠标'),row('2','键盘鼠标套装'),row('3','X3MAX')]);await vm.runInContext('scan()',ctx);
 assert.deepEqual([...ctx.products.keys()],['1']);assert.deepEqual([...ctx.filteredSevereOffTopic.keys()],['2']);assert.deepEqual([...ctx.unclassifiedProducts.keys()],['3']);
});
test('a better observation promotes quarantined title and explicit correction removes a former product',async()=>{
 const ctx=setup([row('1','无线鼠标'),row('2','键盘')]);ctx.unclassifiedProducts.set('1',row('1','X3MAX'));ctx.products.set('2',row('2','鼠标'));
 await vm.runInContext('scan()',ctx);assert.deepEqual([...ctx.products.keys()],['1']);assert.equal(ctx.unclassifiedProducts.size,0);assert.equal(ctx.filteredSevereOffTopic.size,1);
});
