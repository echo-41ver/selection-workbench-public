import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';
import {sessionRecoveryRequest, captchaNeedsManualVerification} from '../scripts/temu_session_recovery.mjs';
import {retryAfterSeconds, networkEvidence} from '../scripts/temu_network_evidence.mjs';
import {createCategoryRelevance,COLLECTION_PROGRESS_VERSION} from '../scripts/category_relevance.mjs';

const source = fs.readFileSync(new URL('../scripts/temu_flower_collect.mjs', import.meta.url), 'utf8');
const snapshotSource = source.slice(source.indexOf('async function writeSnapshot('), source.indexOf('function startBidi()'));

for (const [status,kind,scrolled=true] of [['WAITING_LOGIN','LOGIN'], ['WAITING_SEARCH_VERIFICATION','CAPTCHA'],
  ['STOPPED_HTTP_403','ACCESS'], ['STOPPED_HTTP_429','RATE_LIMIT'],
  ...['WAITING_CAPTCHA','WAITING_SEARCH_VERIFICATION','WAITING_EMPTY_SEARCH_VERIFICATION','WAITING_HUMAN_VERIFICATION']
    .map(status=>[status,null,false])]) {
  test(`${status} scrolled=${scrolled}: snapshot preserves products and selects recovery`, async () => {
    const writes = new Map(), calls = [];
    const row = {goods_id:'123', title:'retained', main_image_url:'https://example.com/product.jpg'};
    const context = vm.createContext({sessionRecoveryRequest, captchaNeedsManualVerification,
      categoryRelevance:createCategoryRelevance({temu:{include_terms:['retained'],severe_exclude_terms:[]}}),COLLECTION_PROGRESS_VERSION,
      AUTO_SESSION_RECOVERY:true, scrollingCollectionStarted:scrolled,
      products:new Map([['123',row]]), pendingProducts:new Map(), unclassifiedProducts:new Map(), filteredSevereOffTopic:new Map(),
      isCategoryRelated:()=>true, atomic:async (file,text)=>{writes.set(file,text);calls.push(file);},
      PRODUCTS:'products', PENDING_PRODUCTS:'pending', UNCLASSIFIED_PRODUCTS:'unclassified', FILTERED_OFF_TOPIC:'filtered', IMAGE_BACKFILL_QUEUE:'images',
      NETWORK_USAGE:'network', PROGRESS:'progress', networkUsageSnapshot:()=>({}),
      QUERY:'mouse', salesSort:{requested:'sales'}, round:2, clicks:3, consecutiveNoNewRounds:0,
      IDLE_REQUIRED:10, clicksSinceRecovery:3, seeMoreBottomOffset:20, bidiStatus:'ACTIVE', imageBlocked:0,
      imageCancelRaces:0, trafficSavingMode:true, nonEssentialBlocked:0, httpBlock:null,
      bootstrapFromSafeTab:false, startedAt:'fixture', now:()=> 'fixture', OUT:'fixture',
      noMoreResultsConfirmed:false, MAX_PRODUCTS:50,
      appendEvent:async (name)=>calls.push(name), boundedShutdown:async code=>calls.push(`shutdown-${code}`),
    });
    vm.runInContext(snapshotSource,context);
    await vm.runInContext(`writeSnapshot(${JSON.stringify(status)}, {manual_action_required:true, automatic_retry:false})`,context);
    const saved=JSON.parse(writes.get('progress'));
    assert.equal(saved.unique_goods,1);
    assert.equal(saved.scroll_collection_started,scrolled);
    assert.equal(saved.collection_phase,scrolled ? 'SCROLLING' : 'BEFORE_SCROLL');
    assert.equal(JSON.parse(writes.get('products').trim()).goods_id,'123');
    if (!kind) {
      assert.equal(saved.session_recovery_kind,undefined);
      assert.equal(saved.manual_action_required,true);
      assert.equal(saved.automatic_retry,false);
      assert.equal(saved.resume_requires_session_rotation,false);
      assert.equal(saved.automatic_recovery_eligible,false);
      assert.ok(!calls.includes('SESSION_RECOVERY_REQUESTED'));
      assert.ok(!calls.includes('shutdown-2'));
      return;
    }
    assert.equal(saved.session_recovery_kind,kind);
    assert.equal(saved.manual_action_required,false);
    assert.equal(saved.resume_requires_session_rotation,true);
    assert.equal(JSON.parse(writes.get('products').trim()).goods_id,'123');
    assert.ok(calls.indexOf('progress') < calls.indexOf('SESSION_RECOVERY_REQUESTED'));
    assert.equal(calls.at(-1),'shutdown-2');
  });
}

test('explicit stop, uncertain driver command, success and standalone manual mode do not rotate', () => {
  for(const status of ['STOPPED_BROWSER_COMMAND_TIMEOUT','STOPPED_USER_REQUEST','KEYWORD_COMPLETE']) {
    assert.equal(sessionRecoveryRequest(status,{http_block:{status:403}},true),null);
  }
  assert.equal(sessionRecoveryRequest('WAITING_CAPTCHA',{},false),null);
});

test('startup CAPTCHA cannot be overridden by restored counts, HTTP evidence or old flags', () => {
  for (const status of ['WAITING_CAPTCHA','CAPTCHA_REQUIRED','WAITING_SEARCH_VERIFICATION',
    'WAITING_EMPTY_SEARCH_VERIFICATION','WAITING_HUMAN_VERIFICATION']) {
    for (const marker of [undefined,false,1,'true']) {
      const progress={scroll_collection_started:marker,round:37,unique_goods:1504,
        automatic_recovery_eligible:true,http_block:{status:429},confirmed_login_redirect:true};
      assert.equal(sessionRecoveryRequest(status,progress,true),null);
      assert.equal(sessionRecoveryRequest(status,{...progress,scroll_collection_started:true},true).session_recovery_kind,'CAPTCHA');
    }
  }
});

test('collection scroll marker is set after a successful scroll, never by an initial CAPTCHA', async () => {
  const scrollSource=source.slice(source.indexOf('async function recordCollectionScroll()'),
    source.indexOf('async function rememberSeeMoreBottomOffset('));
  for (const learned of [false,true]) {
    for (const blocked of [true,false]) {
      const calls=[];
      const page={scrollY:0,scrollHeight:2000,innerHeight:700};
      let scans=0;
      const context=vm.createContext({scrollingCollectionStarted:false,seeMoreBottomOffset:learned?300:null,
        QUERY:'mouse',SORT_REQUEST:'sales',scan:async()=>({blocked}),
        classify:s=>s.blocked,lightPageState:async()=>({...page,blocked:scans++>0}),
        execute:async()=>calls.push('scroll'),sleep:async()=>{},rand:()=>300,
        appendEvent:async event=>calls.push(event),writeSnapshot:async status=>calls.push(status)});
      vm.runInContext(scrollSource,context);
      await vm.runInContext('visibleScrollToLearnedSeeMoreZone()',context);
      assert.equal(context.scrollingCollectionStarted,!blocked);
      assert.deepEqual(calls,blocked?[]:['scroll','SCROLL_COLLECTION_STARTED','RUNNING']);
    }
  }
});

test('HTTP evidence accepts numeric strings and legacy flags', () => {
  assert.equal(sessionRecoveryRequest('STOPPED_HTTP_BLOCK',{http_block:{status:'429'},automatic_retry:false},true).session_recovery_kind,'RATE_LIMIT');
});

test('Retry-After captures BiDi string values and HTTP dates without retaining other headers', () => {
  const headers=[{name:'Retry-After',value:{type:'string',value:'180'}},
    {name:'Set-Cookie',value:{type:'string',value:'private-cookie'}}];
  assert.equal(retryAfterSeconds(headers),180);
  assert.equal(retryAfterSeconds([{name:'retry-after',value:'Thu, 01 Jan 1970 00:02:00 GMT'}],0),120);
  assert.equal(retryAfterSeconds([{name:'retry-after',value:'invalid'}]),null);
  assert.equal(retryAfterSeconds([{name:'retry-after',value:{type:'base64',value:'ignored'}}]),null);
  const evidence=networkEvidence({response:{status:429,headers}},'',null);
  assert.equal(evidence.retry_after_seconds,180);
  assert.ok(!JSON.stringify(evidence).includes('private-cookie'));
});
