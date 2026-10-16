import test from 'node:test';
import assert from 'node:assert/strict';
import {originalListingImage,enrichListingImage} from '../scripts/amazon_public_image.mjs';

const card={asin:'B00ET5VMTU',rank:2,main_image_url:'https://images-na.ssl-images-amazon.com/images/I/81dmm7FomNL._AC_UL600_SR600,400_.jpg',
  detail_status:'详情页失败：Amazon验证页面',detail_error:{code:'AMAZON_VERIFICATION_REQUIRED',retryable:false},specifications:[]};
test('original asset preserves the observed identity and rejects unrelated hosts and paths',()=>{
  assert.equal(originalListingImage(card.main_image_url),'https://images-na.ssl-images-amazon.com/images/I/81dmm7FomNL.jpg');
  for(const value of ['http://m.media-amazon.com/images/I/x.jpg','https://m.media-amazon.com.evil.test/images/I/x.jpg',
    'https://user:pass@m.media-amazon.com/images/I/x.jpg','https://m.media-amazon.com:444/images/I/x.jpg','https://www.amazon.com/dp/B00ET5VMTU']){
    assert.equal(originalListingImage(value),null);
  }
});
test('verified high-resolution image never marks missing details complete or alters scope',async()=>{
  const row=await enrichListingImage(card,async()=>({width:2000,height:2000}));
  assert.equal(row.rank,2);assert.equal(row.asin,card.asin);
  assert.equal(row.image_status,'榜单原图已核验');assert.equal(row.image_width_hint,2000);
  assert.deepEqual(row.detail_error,card.detail_error);assert.equal(row.detail_status,card.detail_status);
  assert.deepEqual(row.specifications,[]);
  assert.equal(row.public_image_evidence.listing_url,card.main_image_url);
});
test('decode failures and small images preserve prior image and bound independent retries',async()=>{
  let row=card,calls=0;
  for(let i=0;i<5;i++)row=await enrichListingImage(row,async()=>{calls++;return {width:100,height:100};});
  assert.equal(calls,3);assert.equal(row.main_image_url,card.main_image_url);
  assert.equal(row.public_image_evidence.verified,false);
});
test('verified original and detail images are not downloaded again',async()=>{
  const inspect=async()=>assert.fail('Already verified image must be reused');
  const full={...card,image_status:'详情页主图'};
  assert.equal(await enrichListingImage(full,inspect),full);
  const original={...card,public_image_evidence:{verified:true}};
  assert.equal(await enrichListingImage(original,inspect),original);
});
