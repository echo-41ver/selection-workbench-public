import {inspectHumanVerification} from '../scripts/temu_verification.mjs';
import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import {parseProductCardMetrics} from '../scripts/product_card_metrics.mjs';
import {inspectSearchReadiness} from '../scripts/temu_search_readiness.mjs';
import {confirmedEmptyQueryResults,decideEmptyQueryResults} from '../scripts/temu_initial_wait_policy.mjs';
const source=fs.readFileSync(new URL('../scripts/temu_flower_collect.mjs',import.meta.url),'utf8');
const template=source.match(/const extractScript = `([\s\S]*?)`;/)[1];
const script=new Function('parseProductCardMetrics','inspectSearchReadiness','inspectHumanVerification','return `'+template+'`;')(parseProductCardMetrics,inspectSearchReadiness,inspectHumanVerification);
const query='magic keyboard touch id numeric';
function scan(loading=false){
 const input={value:query,closest:()=>null,getBoundingClientRect:()=>({width:300,height:40,top:0,bottom:40})};
 const skeleton={children:[],textContent:'',closest:()=>null,getBoundingClientRect:()=>({width:130,height:150,top:100,bottom:250})};
 const document={readyState:'complete',documentURI:'https://www.temu.com/search_result.html?search_key='+encodeURIComponent(query),title:'Temu',body:{innerText:`No results for "${query}", try searching:`},documentElement:{scrollHeight:800},querySelectorAll:s=>s==='input[type="search"]'?[input]:s==='div'&&loading?Array(5).fill(skeleton):[]};
 const styles=e=>({display:'block',visibility:'visible',backgroundColor:e===skeleton?'rgb(245, 245, 245)':'rgb(255, 255, 255)'});
 return new Function('window','document','location','getComputedStyle','scrollY','innerHeight','context',`return(function(){${script}})(context);`)({},document,{href:document.documentURI},styles,0,900,{knownIds:[],repairIds:[]});
}
test('production goods scan carries readiness and settles English empty results after reconnect',()=>{
 const page=scan();assert.equal(page.loading,false);assert.equal(page.searchInputValue,query);assert.equal(page.readyState,'complete');assert.equal(confirmedEmptyQueryResults(page,query),true);
 let previous=null;for(const nowMs of [0,15000,31000]){const d=decideEmptyQueryResults({state:page,query,previous,nowMs});previous=d.clock;assert.equal(d.confirmed,nowMs===31000);}
});
test('production scan still treats skeleton cards as loading despite stale empty text',()=>{
 const page=scan(true);assert.equal(page.loading,true);assert.equal(page.loadingPlaceholderCount,5);assert.equal(confirmedEmptyQueryResults(page,query),false);
});
