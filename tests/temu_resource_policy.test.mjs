import test from 'node:test';
import assert from 'node:assert/strict';
import {nonEssentialRequest, allowPageImages, blockImageRequest} from '../scripts/temu_resource_policy.mjs';

test('bootstrap blocks images while real verification remains usable', () => {
  const documentUrl = 'https://www.temu.com/search_result.html?search_key=remote';
  assert.equal(allowPageImages({documentUrl, preparing:true}), false);
  assert.equal(allowPageImages({documentUrl, humanVerification:true}), true);
  assert.equal(allowPageImages({documentUrl}), false);
  assert.equal(allowPageImages({documentUrl:documentUrl+'&refer_page_name=bgn_verification'}),false);
  assert.equal(allowPageImages({documentUrl:'https://www.temu.com/bgn_verification.html'}),true);
});

test('search and lazy-loaded CDN scripts survive collection traffic saving', () => {
  for (const host of ['static.kwcdn.com', 'static-1.kwcdn.com']) {
    for (const destination of ['script', 'style', 'empty', '']) {
      assert.equal(nonEssentialRequest(host,destination,true,false), false);
    }
  }
});
test('verification and functional resources survive while telemetry can be blocked', () => {
  assert.equal(nonEssentialRequest('pftka-us.temu.com','empty',true,false),true);
  assert.equal(nonEssentialRequest('pftka-us.temu.com','empty',true,true),false);
  assert.equal(nonEssentialRequest('pftka-us.temu.com','script',true,false),false);
  assert.equal(nonEssentialRequest('pftka-us.temu.com','empty',false,false),false);
  assert.equal(nonEssentialRequest('www.temu.com','empty',true,false),false);
});

test('product pictures are blocked before first results, while verification can load', () => {
  const bootstrap = {documentUrl:'https://www.temu.com/search_result.html', preparing:true};
  for (const humanVerification of [false,true]) {
    assert.equal(blockImageRequest({...bootstrap,humanVerification,
      url:'https://img.kwcdn.com/product/fancy/example.jpg',destination:'image'}),true);
  }
  assert.equal(blockImageRequest({...bootstrap,
    url:'https://aimg.kwcdn.com/captcha/puzzle.png',destination:'image'}),false);
  assert.equal(blockImageRequest({documentUrl:'https://www.temu.com/bgn_verification.html',
    url:'https://img.kwcdn.com/upload_aimg/puzzle.png',destination:'image'}),false);
  assert.equal(blockImageRequest({documentUrl:'https://www.temu.com/search_result.html',
    url:'https://img.kwcdn.com/captcha/puzzle.png',destination:'image'}),false);
});

test('image classification cannot cancel scripts or search requests', () => {
  assert.equal(blockImageRequest({url:'https://aimg.kwcdn.com/product/script.js',destination:'script'}),false);
  assert.equal(blockImageRequest({url:'https://static.kwcdn.com/search.js',destination:'script'}),false);
  assert.equal(blockImageRequest({url:'https://www.temu.com/api/poppy/v1/search',destination:'empty'}),false);
  assert.equal(blockImageRequest({url:'https://img.kwcdn.com/upload_aimg/banner.webp',destination:'image'}),true);
});
