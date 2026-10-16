import test from 'node:test';
import assert from 'node:assert/strict';
import {bootstrapEntryPlan,isStableHomeSearchSample} from '../scripts/temu_search_bootstrap_policy.mjs';

test('fresh tabs respect each requested entry instead of silently using empty search',()=>{
  for(const mode of ['home_search','empty_search','direct_search']) {
    assert.equal(bootstrapEntryPlan(mode,'about:blank','fuel pump'),mode);
    assert.equal(bootstrapEntryPlan(mode,'https://www.temu.com/','fuel pump'),mode);
  }
});
test('keyword changes retain the existing results input; target startup avoids navigation',()=>{
  for(const mode of ['home_search','empty_search','direct_search']) {
    assert.equal(bootstrapEntryPlan(mode,'https://www.temu.com/search_result.html?search_key=Fuel%20pump','fuel pump'),'CURRENT_TARGET');
    assert.equal(bootstrapEntryPlan(mode,'https://www.temu.com/search_result.html?search_key=oil%20pump','fuel pump'),'CURRENT_SEARCH_INPUT');
    assert.equal(bootstrapEntryPlan(mode,'https://other.example/search_result.html?search_key=fuel%20pump','fuel pump'),mode);
  }
});
test('homepage submission requires usable controls and rejects verification pages',()=>{
  const state={url:'https://www.temu.com/',readyState:'complete',searchReady:true,searchEnabled:true,inputRect:{width:300,height:40}};
  assert.equal(isStableHomeSearchSample(state),true);
  for(const changed of [{searchEnabled:false},{readyState:'loading'},{text:'Security verification'},{url:'https://www.temu.com/bgn_verification.html'}]) {
    assert.equal(isStableHomeSearchSample({...state,...changed}),false);
  }
});
