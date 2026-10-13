import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';
import {isStableEmptySearchSample,isPostVerificationEmptySearchSample} from '../scripts/temu_search_bootstrap_policy.mjs';
const ready={url:'https://www.temu.com/search_result.html',readyState:'complete',searchReady:true,searchEnabled:true,inputRect:{x:10,y:20,width:240,height:40}};
test('empty search entry requires committed empty URL and visible enabled search box',()=>{
  for(const accept of [isStableEmptySearchSample,isPostVerificationEmptySearchSample]){
    assert.equal(accept(ready),true);
    for(const change of [{url:'https://www.temu.com/'},{url:'https://www.temu.com/search_result.html?search_key=knives'},{url:'https://www.temu.com/search_result.htmlbgn_verification.html'},{readyState:'loading'},{searchReady:false},{searchEnabled:false},{inputRect:{width:0,height:0}}])assert.equal(accept({...ready,...change}),false);
  }
});
test('empty search navigation waits past old search DOM rather than touching its stale input',async()=>{
  const source=fs.readFileSync(new URL('../scripts/temu_flower_collect.mjs',import.meta.url),'utf8');
  const fn=source.slice(source.indexOf('async function navigateConfirmed('),source.indexOf('async function submitHomeSearch('));
  let reads=0,commands=0;
  const context=vm.createContext({bootstrapPageState:async()=>({url:++reads<5?'https://www.temu.com/search_result.html?search_key=knives':'https://www.temu.com/search_result.html'}),guardInitialProducts:async()=>({manual:false}),wd:async()=>{commands++;},sleep:async()=>{},appendEvent:async()=>{},writeSnapshot:async()=>{}});
  await vm.runInContext(fn+"navigateConfirmed('https://www.temu.com/search_result.html',/^https:\\/\\/www\\.temu\\.com\\/search_result\\.html$/,'HOME')",context);
  assert.equal(commands,1);assert.equal(reads,5);
});
test('all legacy entry modes navigate empty search page and retain already-ready target continuation',async()=>{
 const source=fs.readFileSync(new URL('../scripts/temu_flower_collect.mjs',import.meta.url),'utf8');
 const fn=source.slice(source.indexOf('const TEMU_EMPTY_SEARCH ='),source.indexOf('const SAFE_SOURCE ='))+source.slice(source.indexOf('async function bootstrapThroughEmptySearch('),source.indexOf('const bidi = await startBidi'));
 for(const mode of ['home_search','empty_search','direct_search']){
  const destinations=[];
  const context=vm.createContext({ENTRY_MODE:mode,QUERY:'knives',bidiStatus:'ACTIVE',TEMU_EMPTY_SEARCH:'https://www.temu.com/search_result.html',TEMU_EMPTY_SEARCH_URL:/^https:\/\/www\.temu\.com\/search_result\.html$/,bootstrapPageState:async()=>({url:'about:blank'}),decideSearchSubmission:()=> 'REPLACE_AND_ENTER',appendEvent:async()=>{},navigateConfirmed:async url=>destinations.push(url),waitForStableEmptySearch:async()=>({targetAlreadyReady:true}),waitForPostVerificationSearchBox:async()=>({targetAlreadyReady:true})});
  await vm.runInContext(fn+'bootstrapThroughEmptySearch()',context);assert.deepEqual(destinations,['https://www.temu.com/search_result.html']);
  context.bootstrapPageState=async()=>({url:'https://www.temu.com/search_result.html?search_key=knives',visibleGoodsCount:80});context.decideSearchSubmission=()=> 'SKIP_ALREADY_TARGET_URL';
  await vm.runInContext('bootstrapThroughEmptySearch({navigate:false})',context);assert.equal(destinations.length,1);
 }
});
