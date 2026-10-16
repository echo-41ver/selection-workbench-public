import test from 'node:test';
import assert from 'node:assert/strict';
import {endpoint, networkEvidence, diagnosticLimiter, supersededByVerification, supersededBySuccess, collectionRequest} from '../scripts/temu_network_evidence.mjs';

test('only selected document and goods-search requests block collection', () => {
  const sample = {selected_context:true, resource_type:'other', navigation:false};
  for (const pathname of ['/api/poppy/v1/search','/api/poppy/v2/search','/api/search']) {
    assert.equal(collectionRequest({...sample,endpoint:'https://www.temu.com'+pathname}),true);
  }
  for (const pathname of ['/api/seo/get_common_biz_data','/api/cusco/platform/chat/showQuickEntry',
    '/api/poppy/v1/search_suggest','/api/passport/token/touch']) {
    assert.equal(collectionRequest({...sample,endpoint:'https://www.temu.com'+pathname}),false);
  }
  assert.equal(collectionRequest({...sample,endpoint:'https://www.temu.com/search_result.html',navigation:true,resource_type:'document'}),true);
  assert.equal(collectionRequest({...sample,endpoint:'https://www.temu.com/api/search',selected_context:false}),false);
  assert.equal(collectionRequest({...sample,endpoint:'https://temu.com.evil.invalid/api/search'}),false);
});

test('network evidence strips credentials and query strings, retains source and error code', () => {
  const value = networkEvidence({context:'a', navigation:null,
    request:{url:'https://user:secret@www.temu.com/api/search?token=secret', method:'POST', destination:'empty'},
    response:{status:429}, errorText:'NS_ERROR_CONNECTION_REFUSED https://host/?token=secret',
  }, 'https://www.temu.com/search_result.html?search_key=private', 'a');
  assert.equal(value.endpoint,'https://www.temu.com/api/search');
  assert.equal(value.status,429);
  assert.equal(value.selected_context,true);
  assert.equal(value.error_code,'NS_ERROR_CONNECTION_REFUSED');
  assert.equal(JSON.stringify(value).includes('secret'),false);
  assert.equal(JSON.stringify(value).includes('private'),false);
  assert.equal(endpoint('about:neterror?secret'),'about:');
});

test('diagnostics bound duplicate and total log volume', () => {
  const allow = diagnosticLimiter(3,100);
  assert.equal(allow({endpoint:'a'},0),true);
  assert.equal(allow({endpoint:'a'},1),false);
  assert.equal(allow({endpoint:'b'},1),true);
  assert.equal(allow({endpoint:'a'},100),true);
  assert.equal(allow({endpoint:'c'},200),false);
});

test('activation 429 is diagnostic and cannot stop the goods collection',()=>{
 const sample={selected_context:true,resource_type:'other',navigation:false,status:429,
  endpoint:'https://www.temu.com/api/poppy/v2/search_activation'};
 assert.equal(collectionRequest(sample),false);
 assert.equal(collectionRequest({...sample,endpoint:'https://www.temu.com/api/poppy/v2/search'}),true);
});

test('a displayed verification step supersedes only the preceding search-document error', () => {
  const state={url:'https://www.temu.com/bgn_verification.html'};
  const block={status:429,document_endpoint:'https://www.temu.com/search_result.html'};
  assert.equal(supersededByVerification(state,block),true);
  assert.equal(supersededByVerification({url:block.document_endpoint},block),false);
  assert.equal(supersededByVerification({url:block.document_endpoint,verificationVisible:true},block),true);
  assert.equal(supersededByVerification({url:block.document_endpoint+'?refer_page_name=bgn_verification'},block),false);
  assert.equal(supersededByVerification(state,{...block,document_endpoint:state.url}),false);
  assert.equal(supersededByVerification(state,{status:429}),false);
  assert.equal(supersededByVerification(state,{...block,document_endpoint:'https://another.example/search_result.html'}),false);
});

test('failed search is cleared only by a successful response on the same selected endpoint', () => {
  const block={status:429,endpoint:'https://www.temu.com/api/poppy/v1/search'};
  const response={...block,selected_context:true,status:200};
  assert.equal(supersededBySuccess(response,block),true);
  assert.equal(supersededBySuccess({...response,status:429},block),false);
  assert.equal(supersededBySuccess({...response,selected_context:false},block),false);
  assert.equal(supersededBySuccess({...response,endpoint:'https://www.temu.com/api/poppy/v2/search_activation'},block),false);
  assert.equal(supersededBySuccess(response,null),false);
});
