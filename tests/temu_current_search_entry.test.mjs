import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';
import {bootstrapEntryPlan,isStableExistingSearchSample} from '../scripts/temu_search_bootstrap_policy.mjs';

test('existing result page search box is reusable; verification and disabled inputs are not',()=>{
  const page={url:'https://www.temu.com/search_result.html?search_key=old',readyState:'complete',
    searchReady:true,searchEnabled:true,inputRect:{width:300,height:40}};
  assert.equal(isStableExistingSearchSample(page),true);
  for(const patch of [{searchEnabled:false},{text:'Security verification'},
    {url:'https://www.temu.com/bgn_verification.html'},
    {url:'https://elsewhere.test/search_result.html?search_key=old'}])
    assert.equal(isStableExistingSearchSample({...page,...patch}),false);
});

test('next keyword reuses current results; fresh environment enters empty search page',async()=>{
  const source=fs.readFileSync(new URL('../scripts/temu_flower_collect.mjs',import.meta.url),'utf8');
  const code=source.slice(source.indexOf('async function waitForInitialProducts()'),source.indexOf('async function prepareSalesSort()'));
  for(const existing of [true,false]) {
    const actions=[],events=[];
    const context=vm.createContext({URL,Date,Number,Boolean,QUERY:'new keyword',ENTRY_MODE:'empty_search',bootstrapEntryPlan,bootstrapFromSafeTab:true,bidiStatus:'ACTIVE',
      TEMU_EMPTY_SEARCH:'https://www.temu.com/search_result.html',
      bootstrapPageState:async()=>({url:existing?'https://www.temu.com/search_result.html?search_key=old':'about:newtab'}),
      classify:()=>null,guardInitialProducts:async()=>({manual:false}),
      execute:async script=>actions.push(script),
      bootstrapThroughEmptySearch:async options=>actions.push(options||{navigate:true}),
      appendEvent:async type=>events.push(type),sleep:async()=>{},rand:()=>0,ensurePageGuards:async()=>{},
      scan:async()=>({url:'https://www.temu.com/search_result.html?search_key=new%20keyword',visibleGoodsCount:20})});
    await vm.runInContext(code+';waitForInitialProducts()',context);
    if(existing) {
      assert.ok(actions[0].includes('window.scrollTo'));
      assert.equal(actions[1].navigate,false);
      assert.equal(actions[1].reuseSearchPage,true);
      assert.ok(events.includes('NEXT_KEYWORD_CURRENT_SEARCH_ENTRY'));
      assert.ok(!events.includes('BLANK_BOOTSTRAP_NAVIGATION'));
    } else {
      assert.equal(actions[0].navigate,true);
      assert.ok(events.includes('BLANK_BOOTSTRAP_NAVIGATION'));
    }
  }
});
