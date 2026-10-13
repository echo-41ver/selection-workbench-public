import test from 'node:test';
import assert from 'node:assert/strict';
import {blankVerificationDecision as decide} from '../scripts/temu_blank_page_policy.mjs';
const empty={url:'https://www.temu.com/bgn_verification.html',text:'',bodyEmpty:true,visibleControls:0};
test('a genuinely empty failed verification document reloads only once after 30 seconds',()=>{
  let clock=decide(empty,null,0);
  assert.equal(decide(empty,clock,29999).refresh,false);
  clock=decide(empty,clock,30000);assert.equal(clock.refresh,true);
  assert.equal(decide(empty,clock,90000).refresh,false);
});
test('never reload a rendered challenge, incomplete observation, or ordinary page',()=>{
  for(const change of [{bodyEmpty:false},{visibleControls:1},{text:'Slide to verify'},{transientStateUnavailable:true},{url:'https://www.temu.com/search_result.html'}]){
    assert.equal(decide({...empty,...change},{since:0},60000).refresh,false);
  }
});
