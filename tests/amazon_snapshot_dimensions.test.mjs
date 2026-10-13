import test from 'node:test';
import assert from 'node:assert/strict';
import os from 'node:os';
import path from 'node:path';
import {serializedJsonWriter} from '../scripts/collection_checkpoint.mjs';
import {highResolutionDimensions,verifiedDetailImage} from '../scripts/amazon_detail_quality.mjs';

test('failed serialized snapshot rejects its caller without poisoning later queued snapshots',async()=>{
  const bodies=[];
  const failure=Object.assign(new Error('locked'),{code:'EPERM'});
  const write=serializedJsonWriter(path.join(os.tmpdir(),'amazon-writer-unit.json'),{writeSnapshot:async(_file,body)=>{
    bodies.push(JSON.parse(body));if(bodies.length===1)throw failure;
  }});
  const first=write({version:1});const second=write({version:2});
  await assert.rejects(first,error=>error===failure);
  await second;await write({version:3});
  assert.deepEqual(bodies,[{version:1},{version:2},{version:3}]);
});

test('narrow high-resolution originals qualify but thumbnails and undersized edges do not',()=>{
  for(const [w,h] of [[600,600],[345,1460],[441,1500],[382,1500],[120,1500],[180,1000]]){
    assert.equal(highResolutionDimensions(w,h),true,`${w}x${h}`);
    assert.equal(highResolutionDimensions(h,w),true,`${h}x${w}`);
    assert.equal(verifiedDetailImage({main_image_url:'url',detail_quality:{image:{url:'url',verified:true,decoded:true,http_status:200,mime:'image/jpeg',width:w,height:h}}}),true);
  }
  for(const [w,h] of [[599,599],[345,679],[119,2000],[120,1499],[180,999],[0,1500],[120.5,1500],['600',600]])
    assert.equal(highResolutionDimensions(w,h),false,`${w}x${h}`);
});

import {nativeOriginalUrl,nativeOriginalDimensions,qualifyAmazonDetail} from '../scripts/amazon_detail_quality.mjs';
import {runAmazonDetailQueue} from '../scripts/amazon_detail_queue.mjs';
const native='https://m.media-amazon.com/images/I/native.jpg';
const observed='https://m.media-amazon.com/images/I/native._AC_SY679_.jpg';
test('native exception requires original CDN identity with observed detail provenance',()=>{
  const proof={url:native,width:239,height:874,native_original:{observed_detail_url:observed}};
  assert.equal(nativeOriginalUrl(observed),native);
  assert.equal(nativeOriginalDimensions(proof),true);
  for(const bad of [{...proof,url:observed},{...proof,native_original:{observed_detail_url:'https://evil.test/images/I/native.jpg'}},{...proof,width:100},{...proof,height:700}]) assert.ok(!nativeOriginalDimensions(bad));
});
test('exhausted image-only repair retains facts and attempt count without fetching detail pages',async()=>{
  const row={asin:'TEST',rank:1,title:'Verified title',specifications:[{label:'Brand',value:'Example'}],detail_image_attempts:10,
    detail_quality:{title_verified:true},image_candidates:[{url:observed,source:'详情页动态主图'}]};
  let repairs=0;const snapshots=[];
  const repairImage=async r=>{repairs++;return qualifyAmazonDetail(r,{},r.image_candidates,async(url,native_original)=>({width:239,height:874,verified:true,decoded:true,http_status:200,mime:'image/jpeg',native_original}));};
  const result=await runAmazonDetailQueue({records:[row],repairImage,onCheckpoint:async s=>snapshots.push(s),fetchDetail:async()=>assert.fail('No detail page needed')});
  assert.equal(result.queue.completed_count,1);assert.equal(result.records[0].detail_image_attempts,10);
  assert.deepEqual(result.records[0].specifications,row.specifications);assert.equal(result.records[0].main_image_url,native);
  assert.equal(snapshots[0].records[0].native_image_recovery_attempted,true);
  await runAmazonDetailQueue({records:result.records,repairImage,onCheckpoint:async()=>{},fetchDetail:async()=>assert.fail('Already complete')});
  assert.equal(repairs,1);
});
