import test from 'node:test';
import assert from 'node:assert/strict';
import {hasLocalWarehouseLabel,selectLocalWarehouse,confirmsLocalWarehouse,inspectLocalWarehouse} from '../scripts/temu_local_warehouse.mjs';
import vm from 'node:vm';

const card=text=>({source:'VISIBLE_TEMU_FRONTEND',card_text:text});
test('only explicit Chinese or English local warehouse badges qualify',()=>{
 for(const label of ['Local Warehouse','Local warehouse','LOCAL WAREHOUSE','本地仓','本地仓库','本地倉庫','本地倉'])
  assert.equal(hasLocalWarehouseLabel(card('Wireless keyboard\n$12.99\n'+label)),true);
});

const query='wireless keyboard',url='https://www.temu.com/search_result.html?search_key=wireless%20keyboard';
const selected=()=>({url,selected:true,explicit:true,goods_ids:['123'],dialog_open:false});
test('selection confirms checked state and stable results, never toggles an already selected filter',async()=>{
 let calls=0;const result=await selectLocalWarehouse({inspect:async()=>selected(),click:async()=>assert.fail('already selected'),guard:async()=>{},sleep:async()=>{calls++;},query});
 assert.equal(result.state,'UI_CONFIRMED');assert.equal(calls,2);
});
test('filter clicks and optional apply are verified before accepting new results',async()=>{
 let checked=false,dialog=true;const actions=[];
 const result=await selectLocalWarehouse({query,guard:async()=>{},sleep:async()=>{},
  inspect:async()=>({...selected(),selected:checked,option:'checkbox',dialog_open:dialog,apply:dialog?'apply':null}),
  click:async e=>{actions.push(e);if(e==='checkbox')checked=true;if(e==='apply')dialog=false;}});
 assert.equal(result.state,'UI_CONFIRMED');assert.deepEqual(actions,['checkbox','apply']);
});
test('missing filter fails closed and CAPTCHA guard prevents interaction',async()=>{
 assert.equal((await selectLocalWarehouse({query,maxChecks:4,inspect:async()=>({url}),guard:async()=>{},sleep:async()=>{},click:async()=>assert.fail('no control')})).state,'UNCONFIRMED');
 await assert.rejects(()=>selectLocalWarehouse({query,inspect:async()=>({url,option:'checkbox'}),guard:async()=>{throw Error('CAPTCHA');},sleep:async()=>{},click:async()=>assert.fail('blocked')}),/CAPTCHA/);
 assert.equal(confirmsLocalWarehouse({...selected(),url:'https://www.temu.com/search_result.html?search_key=mouse'},query),false);
});
test('applied results can unmount the radio; filter reset invalidates the observed proof',async()=>{
 let applied=false;const filterUrl=url+'&filter_items=400%3A1%7C0%3A1';
 const result=await selectLocalWarehouse({query,guard:async()=>{},sleep:async()=>{},
  inspect:async()=>({...selected(),url:filterUrl,filter_group:'400',selected:!applied,explicit:!applied,dialog_open:!applied,apply:applied?null:'apply',menu:'menu'}),
  click:async e=>{assert.equal(e,'apply');applied=true;}});
 assert.equal(result.state,'UI_CONFIRMED');assert.equal(result.filter_item,'400:1');
 assert.equal(Boolean(confirmsLocalWarehouse({...selected(),selected:false,explicit:false,url},query,result)),false);
 assert.equal(Boolean(confirmsLocalWarehouse({...selected(),selected:false,explicit:false,url:filterUrl},query,result)),true);
 // Sales sorting changes other filter entries. The selected warehouse stays valid.
 assert.equal(Boolean(confirmsLocalWarehouse({...selected(),selected:false,explicit:false,url:filterUrl.replace('%7C0%3A1','%7C1%3A1')},query,result)),true);
 assert.equal(Boolean(confirmsLocalWarehouse({...selected(),selected:false,explicit:false,url:filterUrl.replace('400%3A1','400%3A2')},query,result)),false);
});
test('real TEMU radio labels and counted Show results buttons work in English and Chinese',()=>{
 for(const [prefix,badge,apply] of [['Ships From','Local Warehouse','Show 1000+ results'],['Ships From (1)','Local Warehouse','Show 1000+ results'],['物流配送（1）','本地仓库','显示 1000+ 个结果']]) {
  const node=(tag,text,attrs={},extra={})=>({tagName:tag,innerText:text,textContent:text,disabled:false,
   getAttribute:k=>attrs[k]??null,hasAttribute:k=>k in attrs,closest:()=>null,querySelector:()=>null,
   getBoundingClientRect:()=>({width:100,height:30}),...extra});
  const radio=node('SPAN','',{'role':'radio','aria-label':prefix+', '+badge,'aria-checked':'true'});
  const submit=node('DIV',apply,{role:'button'}),dialog=node('DIV','',{role:'dialog'},{contains:e=>e===submit});
  const result=vm.runInNewContext(`(${inspectLocalWarehouse.toString()})()`,{location:{href:url},getComputedStyle:()=>({}),
   document:{querySelectorAll:s=>s==='[role="dialog"]'?[dialog]:s.includes('button,label')?[radio,submit]:[]}});
  assert.equal(result.selected,true);assert.equal(result.apply,submit);assert.equal(result.dialog_open,true);
 }
});
test('inspector distinguishes a real checked filter from product badge text',()=>{
 const node=(tag,text,attrs={},extra={})=>({tagName:tag,innerText:text,textContent:text,disabled:false,
  getAttribute:k=>attrs[k]??null,hasAttribute:k=>k in attrs,closest:()=>null,querySelector:()=>null,
  getBoundingClientRect:()=>({width:100,height:30}),...extra});
 const checkbox=node('INPUT','',{}, {checked:true});
 const label=node('LABEL','Local Warehouse',{}, {control:checkbox});
 const badge=node('SPAN','Local Warehouse',{}, {closest:()=>({tagName:'A'})});
 const result=vm.runInNewContext(`(${inspectLocalWarehouse.toString()})()`,{location:{href:url},getComputedStyle:()=>({}),
  document:{querySelectorAll:s=>s==='a[href]'?[{href:'https://www.temu.com/item-g-123.html'}]:s.includes('button,label')?[label,badge]:[]}});
 assert.equal(result.selected,true);assert.equal(result.explicit,true);assert.equal(result.ambiguous,false);
});
test('shipping claims and warehouse wording in titles do not qualify',()=>{
 for(const text of ['Fast delivery','Ships from US','Local','Free shipping','US warehouse','Local Warehouse storage shelf','本地仓货架商品','Not Local Warehouse'])
  assert.equal(hasLocalWarehouseLabel(card(text)),false);
 assert.equal(hasLocalWarehouseLabel({title:'Local Warehouse'}),false);
 assert.equal(hasLocalWarehouseLabel({...card('Local Warehouse'),source:'SEARCH_RESPONSE_JSON'}),false);
});
