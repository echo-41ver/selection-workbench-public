import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';
import {parseProductCardMetrics} from '../scripts/product_card_metrics.mjs';

const source=fs.readFileSync(new URL('../scripts/temu_flower_collect.mjs',import.meta.url),'utf8');
const start=source.indexOf('const extractScript = `')+'const extractScript = '.length;
const end=source.indexOf('`;',start)+1;
const script=vm.runInNewContext(source.slice(start,end),{parseProductCardMetrics});

function node({tag='DIV',text='',attrs={},children=[]}={}) {
  const item={tagName:tag,innerText:text,attributes:[],children,parentElement:null,
    getAttribute:key=>attrs[key]??null,
    getBoundingClientRect:()=>({width:100,height:100,left:0,top:0,right:100,bottom:100}),
    querySelector:()=>null,
    querySelectorAll(selector){
      const all=[];const walk=n=>{for(const child of n.children||[]){all.push(child);walk(child);}};walk(this);
      if(selector==='*')return all;
      if(selector==='img')return all.filter(n=>n.tagName==='IMG');
      if(selector==='a[href]')return all.filter(n=>n.tagName==='A'&&n.href);
      if(selector==='a[href],[data-goods-id],[data-goodsid]')return all.filter(n=>(n.tagName==='A'&&n.href)||n.getAttribute('data-goods-id')||n.getAttribute('data-goodsid'));
      return [];
    }};
  if(attrs.href)item.href=attrs.href;
  for(const child of children)child.parentElement=item;
  return item;
}

function extract({ambiguous=false,accessible=false}={}) {
  const anchor=node({tag:'A',text:'无线鼠标商品',attrs:{href:'https://www.temu.com/mouse-g-123456789.html'}});
  let inner=anchor;
  for(let i=0;i<10;i++)inner=node({text:'无线鼠标商品',children:[inner]});
  const label=node({attrs:{'aria-label':'评分 4.8 分（满分5分）'}});
  const metrics='无线鼠标商品\n$3.83\n已售\n2,005件\n268条评价';
  const card=node({text:metrics,children:[inner,...(accessible?[label]:[])]});
  const other=node({text:'其它商品\n已售9999件\n评分1.1分',attrs:{'data-goods-id':'987654321'}});
  const list=node({text:metrics+'\n'+other.innerText,children:[card,other]});
  const body=node({tag:'BODY',text:list.innerText,children:[list]});
  if(ambiguous){card.children.push(other);other.parentElement=card;card.innerText=metrics+'\n'+other.innerText;}
  const document={querySelectorAll:selector=>selector==='a[href]'?[anchor]:[],body,
    documentElement:{scrollHeight:800},documentURI:'https://www.temu.com/search_result.html',title:'Search',readyState:'complete'};
  const context=vm.createContext({document,window:{},URL,location:{href:document.documentURI},scrollY:0,innerHeight:1000,innerWidth:1200,
    getComputedStyle:()=>({display:'block',visibility:'visible',opacity:'1',backgroundImage:''})});
  return vm.runInContext('(function(){'+script+'})({knownIds:[],repairIds:[],observedAt:"2026-10-13T03:50:00Z"})',context).rows[0];
}

test('deep same-product wrappers expose card metrics without crossing a sibling product id',()=>{
  const row=extract({accessible:true});
  assert.equal(row.sales_count,2005);assert.equal(row.review_count,268);assert.equal(row.rating,4.8);
  assert.equal(row.card_ancestor_depth,11);
  assert.ok(!row.card_text.includes('9999'));assert.equal(row.source,'VISIBLE_TEMU_FRONTEND');
  assert.equal(row.observed_at,'2026-10-13T03:50:00Z');
});

test('ambiguous ancestor with a second data-goods-id is excluded even without a second anchor',()=>{
  const row=extract({ambiguous:true});
  assert.equal(row.card_ancestor_depth,10);assert.equal(row.sales_text,null);assert.equal(row.rating,null);
  assert.ok(!row.card_text.includes('9999'));assert.ok(!row.card_text.includes('2,005'));
});

test('unlabelled numbers in card copy never become a rating',()=>{
  const row=extract();assert.equal(row.rating,null);assert.equal(row.sales_count,2005);
});
