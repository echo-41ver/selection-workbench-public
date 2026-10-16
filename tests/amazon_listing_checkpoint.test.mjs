import test from 'node:test';
import assert from 'node:assert/strict';
import {collectRankedListing} from '../scripts/amazon_listing_checkpoint.mjs';
const scope={source_url:'https://www.amazon.com/zgbs/hi/552794',category_key:'fixture',target_count:2};
const rows=[1,2].map(rank=>({rank,asin:`B00000000${rank}`,title:'Drill',product_url:'https://www.amazon.com/dp/B000000001',main_image_url:'https://m.media-amazon.com/images/I/x.jpg'}));
test('partial ranks retry and only a contiguous page is committed',async()=>{
  let calls=0;const saved=[];
  const result=await collectRankedListing({scope,loadPage:async()=>++calls===1?rows.slice(0,1):rows,
    save:async state=>saved.push(structuredClone(state)),sleep:async()=>{}});
  assert.equal(calls,2);assert.equal(result.length,2);
  assert.equal(saved[0].status,'LISTING_FAILED');assert.deepEqual(saved[0].pages,{});
  assert.equal(saved.at(-1).status,'LISTING_COMPLETE');
});
test('real verification signal never becomes a retry loop',async()=>{
  let calls=0;
  await assert.rejects(collectRankedListing({scope,save:async()=>{},sleep:async()=>{},
    loadPage:async()=>{calls++;throw Object.assign(new Error('verification'),{retryable:false});}}),/verification/);
  assert.equal(calls,1);
});
test('different ranking scope cannot silently reuse checkpoints',async()=>{
  await assert.rejects(collectRankedListing({scope,checkpoint:{schema_version:'amazon-ranking-pages-v1',scope:{...scope,target_count:1}},
    loadPage:async()=>{throw new Error('must not load');},save:async()=>{}}),/scope mismatch/);
});
