import test from 'node:test';
import assert from 'node:assert/strict';
import {hasLocalWarehouseLabel,selectLocalWarehouse,confirmsLocalWarehouse,inspectLocalWarehouse,loadLocalWarehouseEvidence} from '../scripts/temu_local_warehouse.mjs';
import vm from 'node:vm';
import fs from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';

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

test('a search missing its filter bar reloads once then still requires checked and applied warehouse results',async()=>{
 let phase=0,reloads=0;const actions=[];
 const result=await selectLocalWarehouse({query,guard:async()=>{},sleep:async()=>{},
  reload:async()=>{reloads++;phase=1;},
  inspect:async()=>({url:phase>=3?url+'&filter_items=400%3A1':url,
   goods_ids:phase===4?['local-product']:['unfiltered-product'],
   selected:phase===3,explicit:phase===2||phase===3,filter_group:phase>=2?'400':null,
   menu:phase===1?'menu':null,option:phase===2?'checkbox':null,
   dialog_open:phase===2||phase===3,apply:phase===3?'apply':null}),
  click:async control=>{actions.push(control);phase++;}});
 assert.equal(reloads,1);assert.deepEqual(actions,['menu','checkbox','apply']);
 assert.equal(result.state,'UI_CONFIRMED');assert.equal(result.filter_item,'400:1');
});

test('one reload cannot turn a persistently missing filter into success',async()=>{
 let reloads=0;
 const result=await selectLocalWarehouse({query,guard:async()=>{},sleep:async()=>{},
  inspect:async()=>({url,goods_ids:['unfiltered-product']}),
  reload:async()=>{reloads++;},click:async()=>assert.fail('no filter control')});
 assert.equal(reloads,1);assert.equal(result.state,'UNCONFIRMED');
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
test('all-filters entry takes priority over a shipping control in the horizontal carousel',()=>{
 const button=text=>({tagName:'DIV',innerText:text,getAttribute:k=>k==='role'?'button':null,hasAttribute:()=>false,
  closest:()=>null,querySelector:()=>null,getBoundingClientRect:()=>({width:100,height:30})});
 const filters=button('Filters (1)\nFilters (1)'),shipping=button('Ships From (1)\nShips From (1)');
 const result=vm.runInNewContext(`(${inspectLocalWarehouse.toString()})()`,{location:{href:url},getComputedStyle:()=>({}),
  document:{querySelectorAll:s=>s.includes('button,label')?[shipping,filters]:[]}});
 assert.equal(result.menu,filters);
});
test('an obstructed click is recorded and retried against freshly read controls',async()=>{
 let clicked=0;const retries=[];
 const result=await selectLocalWarehouse({query,guard:async()=>{},sleep:async()=>{},onRetry:async r=>retries.push(r),
  inspect:async()=>({...selected(),selected:clicked>=2,option:'checkbox'}),
  click:async()=>{clicked++;if(clicked===1)throw Error('element click intercepted');}});
 assert.equal(result.state,'UI_CONFIRMED');assert.equal(retries.length,1);assert.equal(retries[0].reason,'控件被遮挡');
});
test('shipping claims and warehouse wording in titles do not qualify',()=>{
 for(const text of ['Fast delivery','Ships from US','Local','Free shipping','US warehouse','Local Warehouse storage shelf','本地仓货架商品','Not Local Warehouse'])
  assert.equal(hasLocalWarehouseLabel(card(text)),false);
 assert.equal(hasLocalWarehouseLabel({title:'Local Warehouse'}),false);
 assert.equal(hasLocalWarehouseLabel({...card('Local Warehouse'),source:'SEARCH_RESPONSE_JSON'}),false);
});

test('resumed filter proof is accepted only while its exact query and URL filter remain applied',async()=>{
 const evidence={state:'UI_CONFIRMED',query,filter_item:'400:1'};
 const state={...selected(),url:url+'&filter_items=400%3A1%7C1%3A1',selected:false,explicit:false,menu:'menu'};
 const result=await selectLocalWarehouse({query,initialEvidence:evidence,inspect:async()=>state,guard:async()=>{},sleep:async()=>{},
  click:async()=>assert.fail('already applied filter must not be clicked again')});
 assert.equal(result.state,'UI_CONFIRMED');assert.equal(result.filter_item,'400:1');
 const reset=await selectLocalWarehouse({query,initialEvidence:evidence,maxChecks:3,
  inspect:async()=>({...state,url,menu:null}),guard:async()=>{},sleep:async()=>{},click:async()=>{}});
 assert.equal(reset.state,'UNCONFIRMED');
});

test('legacy filter proof is recovered from its own query events without inventing filter values',async()=>{
 const directory=await fs.mkdtemp(path.join(os.tmpdir(),'warehouse-proof-'));
 try{
  const proof={type:'LOCAL_WAREHOUSE_FILTER_RESULT',state:'UI_CONFIRMED',query,filter_item:'400:1'};
  await fs.writeFile(path.join(directory,'progress.json'),'{"local_warehouse_filter":{"state":"PENDING"}}');
  await fs.writeFile(path.join(directory,'events.jsonl'),JSON.stringify(proof)+'\n{"type":"LOCAL_WAREHOUSE_FILTER_RESULT","state":"UNCONFIRMED"}\n{partial');
  assert.equal((await loadLocalWarehouseEvidence(directory,query)).filter_item,'400:1');
  assert.equal((await loadLocalWarehouseEvidence(directory,'different query')).state,'PENDING');
 }finally{await fs.rm(directory,{recursive:true,force:true});}
});
