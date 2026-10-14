import test from 'node:test';
import assert from 'node:assert/strict';
import {confirmedNoMoreResults,inspectSearchEnd} from '../scripts/collection_checkpoint.mjs';

const healthy = {
  url:'https://www.temu.com/search_result.html?search_key=remote%20control&sort=top_sales',
  documentURI:'https://www.temu.com/search_result.html?search_key=remote%20control&sort=top_sales',
  text:'Remote control results\nNo more results',
  visibleGoodsCount:86,
  noMoreResultsVisible:true,
  seeMorePresent:false,
  tryAgainVisible:false,
  pageFaultVisible:false,
};
const options={query:'remote control',sortConfirmed:true,httpBlock:null};

test('visible stable end of healthy sorted search completes this keyword',()=>{
  assert.equal(confirmedNoMoreResults(healthy,{...healthy},options),true);
});

test('no-more text never hides a page fault or unfinished expansion',()=>{
  for(const changed of [
    {pageFaultVisible:true},{tryAgainVisible:true},{seeMorePresent:true},
    {documentURI:'about:neterror?e=connectionFailure'},
    {url:'https://www.temu.com/login.html'},
    {visibleGoodsCount:0},{visibleGoodsCount:87},{noMoreResultsVisible:false},
  ]) assert.equal(confirmedNoMoreResults(healthy,{...healthy,...changed},options),false);
  assert.equal(confirmedNoMoreResults(healthy,healthy,{...options,httpBlock:{status:429}}),false);
  assert.equal(confirmedNoMoreResults(healthy,healthy,{...options,sortConfirmed:false}),false);
  assert.equal(confirmedNoMoreResults(healthy,healthy,{...options,query:'power drill'}),false);
});


test('five stable results still finish a keyword even below configured quantity',()=>{
 const five={...healthy,visibleGoodsCount:5};
 assert.equal(confirmedNoMoreResults(five,{...five},options),true);
});

test('visible footer marker is detected outside the first 5000 body characters',()=>{
 const previousDocument=globalThis.document, previousStyle=globalThis.getComputedStyle;
 const row=text=>({textContent:text,getBoundingClientRect:()=>({width:100,height:20})});
 globalThis.document={documentURI:healthy.documentURI,querySelectorAll:()=>[row('x'.repeat(6000)),row('— No more results —')]};
 globalThis.getComputedStyle=()=>({display:'block',visibility:'visible'});
 try { assert.equal(inspectSearchEnd().noMoreResultsVisible,true); }
 finally { globalThis.document=previousDocument;globalThis.getComputedStyle=previousStyle; }
});

test('Chinese end marker is recognized without treating it as an empty query',()=>{
 const previousDocument=globalThis.document,previousStyle=globalThis.getComputedStyle;
 const row=text=>({textContent:text,getBoundingClientRect:()=>({width:100,height:20})});
 globalThis.getComputedStyle=()=>({display:'block',visibility:'visible'});
 try {
  for(const text of ['没有更多结果','沒有更多結果','— 没有更多结果 —']){
   globalThis.document={documentURI:healthy.documentURI,querySelectorAll:()=>[row(text)]};
   assert.equal(inspectSearchEnd().noMoreResultsVisible,true);
  }
  globalThis.document={documentURI:healthy.documentURI,querySelectorAll:()=>[row('没有搜索结果')]};
  assert.equal(inspectSearchEnd().noMoreResultsVisible,false);
 }finally{globalThis.document=previousDocument;globalThis.getComputedStyle=previousStyle;}
});
