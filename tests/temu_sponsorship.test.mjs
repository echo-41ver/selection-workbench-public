import {inspectHumanVerification} from '../scripts/temu_verification.mjs';
import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';
import {parseProductCardMetrics} from '../scripts/product_card_metrics.mjs';
import {inspectSearchReadiness} from '../scripts/temu_search_readiness.mjs';
const source=fs.readFileSync(new URL('../scripts/temu_flower_collect.mjs',import.meta.url),'utf8');
const block=source.slice(source.indexOf('const extractScript = `')+23,source.indexOf('\nconst installResponseCaptureScript'));
const script=vm.runInNewContext('`'+block.slice(0,block.lastIndexOf('`;'))+'`',{parseProductCardMetrics,inspectSearchReadiness,inspectHumanVerification});
function extract({text='Remote control\n$5.00',hidden=false,label='',long=false,offscreen=false,partial=false,labelDelay=false,loading=false,shortGap=false}={}){
  const img={currentSrc:'https://img.kwcdn.com/product.jpg',alt:'Remote control product',getAttribute:()=>null,attributes:[]};
  const card={innerText:long?'x'.repeat(10001):text,parentElement:null,attributes:[],getAttribute:k=>k==='aria-label'?label:null,
    getBoundingClientRect:()=>({width:hidden?0:100,height:100,top:offscreen?200:0,left:0,bottom:partial?150:offscreen?300:100,right:100}),querySelector:()=>loading?{}:null,querySelectorAll:q=>q==='img'?[img]:q==='*'?[]:[anchor]};
  const anchor={href:'https://www.temu.com/item-g-123456789.html',innerText:text,parentElement:card,getAttribute:()=>null};
  const document={querySelectorAll:q=>q==='a[href]'?[anchor]:[],body:{innerText:text},documentElement:{scrollHeight:100},readyState:'complete',title:'Search',documentURI:'https://www.temu.com/search_result.html'};
  const ctx=vm.createContext({document,window:{},URL,location:{href:document.documentURI},scrollY:0,innerHeight:100,innerWidth:100,getComputedStyle:()=>({display:'block',visibility:'visible',opacity:'1',backgroundImage:''})});
  const run=at=>vm.runInContext('(function(){'+script+'})({knownIds:[],repairIds:[],observedAt:"'+at+'"})',ctx).rows[0];
  const first=run('2026-10-05T00:00:00.000Z');
  if(!hidden&&!long&&!offscreen&&!partial&&!loading&&!label&&!/Sponsored/.test(text))assert.equal(first.is_sponsored,undefined);
  if(labelDelay)card.getAttribute=k=>k==='aria-label'?'Sponsored':null;
  return run(shortGap?'2026-10-05T00:00:00.500Z':'2026-10-05T00:00:02.100Z');
}
test('visible complete card records observed absence and retains unknown sales',()=>{
 const row=extract();assert.equal(row.is_sponsored,false);assert.equal(row.sales_text,null);assert.equal(row.sponsorship_observed_at,'2026-10-05T00:00:02.100Z');
 assert.equal(row.sponsorship_observation,'VISIBLE_COMPLETE_CARD_LABEL_SCAN_V1');
});
test('text and accessible ad labels are observed as advertising',()=>{
 for(const row of [extract({text:'Remote control\n$5.00\nSponsored'}),extract({label:'广告'})])assert.equal(row.is_sponsored,true);
});
test('hidden and truncated cards remain unknown rather than natural products',()=>{
 for(const row of [extract({hidden:true}),extract({long:true}),extract({offscreen:true}),extract({partial:true})])assert.equal(row.is_sponsored,undefined);
});

test('late advertising label wins and loading skeleton never becomes natural',()=>{assert.equal(extract({labelDelay:true}).is_sponsored,true);assert.equal(extract({loading:true}).is_sponsored,undefined);});

test('a second scan before settling remains unknown',()=>assert.equal(extract({shortGap:true}).is_sponsored,undefined));
