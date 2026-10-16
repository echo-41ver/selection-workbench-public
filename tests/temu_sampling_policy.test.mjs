import test from 'node:test';import assert from 'node:assert/strict';
import {samplingDecision as decide} from '../scripts/temu_sampling_policy.mjs';
test('only redundant extension sampling is shortened, independently per pass',()=>{
 const ids=Array.from({length:250},(_,i)=>String(i));
 const p={enabled:true,minimum_sample:250,reference_goods_ids:ids.slice(0,240)};
 assert.equal(decide(ids,p,500).reduced,true);
 assert.equal(decide(ids,{...p,protected:true},500).reduced,false);
 assert.equal(decide(ids.slice(0,200),p,500).reduced,false);
 assert.equal(decide(ids,{...p,reference_goods_ids:ids.slice(0,200)},500).reduced,false);
 assert.equal(decide(ids,p,250).reduced,false);
});
