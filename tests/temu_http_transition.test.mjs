import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';
import {observeHttpPageTransition} from '../scripts/temu_http_transition.mjs';
import {classifyPageState} from '../scripts/temu_flower_recovery_policy.mjs';
import {supersededByVerification} from '../scripts/temu_network_evidence.mjs';
import {terminalCollectionStop} from '../scripts/collection_checkpoint.mjs';
import {decideInitialProductsWait,decideEmptyQueryResults} from '../scripts/temu_initial_wait_policy.mjs';

const blank={url:'https://www.temu.com/search_result.html',visibleGoodsCount:0,text:'',readyState:'complete'};
const block={status:429};
async function run(states,options={}){
  let clock=0,reads=0,announcements=0;
  const result=await observeHttpPageTransition({state:blank,block,sleep:async ms=>{clock+=ms},now:()=>clock,
    readState:async()=>{const value=states[Math.min(reads++,states.length-1)];if(value instanceof Error)throw value;return value;},
    onWaiting:async()=>{announcements++},...options});
  return {...result,reads,announcements};
}
test('429 followed by delayed bgn navigation preserves verification rather than stopping',async()=>{
  const challenge={...blank,url:'https://www.temu.com/bgn_verification.html',readyState:'loading'};
  const r=await run([blank,blank,null,challenge]);
  assert.equal(r.reason,'HUMAN_VERIFICATION');assert.equal(r.state.url,challenge.url);
  assert.equal(r.waitedMs,1000);assert.equal(r.announcements,1);
});
test('inline verification after 403 also preserves the same page',async()=>{
  const r=await run([blank,{...blank,text:'Security verification'}],{block:{status:403}});
  assert.equal(r.reason,'HUMAN_VERIFICATION');assert.equal(r.waitedMs,500);
});
test('confirmed login and real browser errors do not acquire a ten-second delay',async()=>{
  for(const state of [{...blank,url:'https://www.temu.com/login.html'},
    {...blank,text:'Access denied'},{...blank,documentURI:'about:neterror'}]){
    const r=await run([],{state});assert.equal(r.waitedMs,0);assert.equal(r.reads,0);
  }
});
test('unchanged empty page has a bounded observation, not endless waiting',async()=>{
  const r=await run([blank]);assert.equal(r.reason,'TRANSITION_NOT_OBSERVED');
  assert.equal(r.waitedMs,10000);assert.equal(r.reads,40);assert.equal(r.announcements,1);
});
test('transient driver navigation errors survive and usable products end observation',async()=>{
  const r=await run([new Error('navigation'),{...blank,visibleGoodsCount:40}]);
  assert.equal(r.reason,'VISIBLE_RESULTS');assert.equal(r.waitedMs,500);
});

test('actual bootstrap guard clears preceding 429 and returns manual verification without stopping',async()=>{
  const source=fs.readFileSync(new URL('../scripts/temu_flower_collect.mjs',import.meta.url),'utf8');
  const start=source.indexOf('async function guardInitialProducts(');
  const end=source.indexOf('\nasync function navigateConfirmed',start);
  const events=[];const snapshots=[];
  const context=vm.createContext({httpBlock:{status:429,document_endpoint:blank.url},
    classify:classifyPageState,observeHttpPageTransition,supersededByVerification,terminalCollectionStop,
    decideInitialProductsWait,decideEmptyQueryResults,emptyResultsWait:null,lastInitialWaitingSnapshot:0,products:new Map(),humanVerificationActive:false,initialProductsWait:null,finalStatus:null,
    QUERY:'remote control',INITIAL_PRODUCTS_TIMEOUT_SECONDS:180,Date,URL,now:()=>new Date().toISOString(),
    sleep:async()=>{},bootstrapPageState:async()=>({...blank,url:'https://www.temu.com/bgn_verification.html',readyState:'loading'}),
    appendEvent:async(type,data)=>events.push({type,...data}),writeSnapshot:async(status,data)=>snapshots.push({status,...data}),
    confirmedLoginRedirect:()=>false});
  const result=await vm.runInContext('class InitialProductsStopped extends Error {}\n'+source.slice(start,end)+
    '\nguardInitialProducts('+JSON.stringify(blank)+',"empty_search_stability")',context);
  assert.equal(result.manual,true);assert.equal(context.httpBlock,null);assert.equal(context.finalStatus,null);
  assert.equal(context.humanVerificationActive,true);
  assert.ok(events.some(e=>e.type==='HTTP_SIGNAL_SUPERSEDED_BY_VERIFICATION'));
  assert.ok(!events.some(e=>e.type.startsWith('STOPPED_')));
  assert.ok(snapshots.every(s=>s.browser_preserved===true));
});

test('homepage sales, review counts and prices are not HTTP error evidence',()=>{
  for(const text of ['已售429件','403条评价','Mouse\n$4.29\n429\n已售\n用户评价',
    '429 sold','403 reviews','编号403 无线鼠标']) {
    const state={...blank,title:'Temu | 探索好物',url:'https://www.temu.com/',text};
    assert.equal(classifyPageState(state),null,text);
    assert.equal(terminalCollectionStop(state,null),null,text);
  }
});

test('explicit HTTP errors remain blocked while challenge URL still takes priority',()=>{
  for(const text of ['HTTP 429','Error 403','403 Forbidden','429\nToo Many Requests','403']) {
    assert.equal(classifyPageState({...blank,text}),'HTTP_BLOCK',text);
  }
  assert.equal(classifyPageState({...blank,title:'403 Forbidden',text:'Request cannot be completed'}),'HTTP_BLOCK');
  assert.equal(classifyPageState({...blank,url:'https://www.temu.com/bgn_verification.html',text:'Error 429'}),'HUMAN_VERIFICATION');
  assert.equal(terminalCollectionStop({...blank,url:'https://www.temu.com/bgn_verification.html',text:'Error 429'},null),null);
});

for(const challenge of [true,false])test(`text-only HTTP guard rechecks once; challenge=${challenge}`,async()=>{
  const source=fs.readFileSync(new URL('../scripts/temu_flower_collect.mjs',import.meta.url),'utf8');
  const start=source.indexOf('async function guardInitialProducts(');
  const end=source.indexOf('\nasync function navigateConfirmed',start);
  const events=[];const snapshots=[];let reads=0;
  const initial={...blank,text:'Error 429',title:'429 Too Many Requests'};
  const current=challenge?{...blank,url:'https://www.temu.com/bgn_verification.html',text:'',title:'Temu'}:initial;
  const context=vm.createContext({httpBlock:null,classify:classifyPageState,
    observeHttpPageTransition,supersededByVerification,terminalCollectionStop,
    decideInitialProductsWait,decideEmptyQueryResults,emptyResultsWait:null,lastInitialWaitingSnapshot:0,products:new Map(),humanVerificationActive:false,initialProductsWait:null,finalStatus:null,
    QUERY:'mouse',INITIAL_PRODUCTS_TIMEOUT_SECONDS:180,Date,URL,now:()=>new Date().toISOString(),
    sleep:async()=>{},bootstrapPageState:async()=>{reads++;return current;},
    appendEvent:async(type,data)=>events.push({type,...data}),writeSnapshot:async(status,data)=>snapshots.push({status,...data}),
    confirmedLoginRedirect:()=>false});
  const promise=vm.runInContext('class InitialProductsStopped extends Error {}\n'+source.slice(start,end)+
    '\nguardInitialProducts('+JSON.stringify(initial)+',"home_search_stability")',context);
  if(challenge) {
    const decision=await promise;assert.equal(decision.manual,true);
    assert.equal(decision.status,'WAITING_CAPTCHA');assert.equal(context.finalStatus,null);
  } else {
    await assert.rejects(promise,/STOPPED_HTTP_BLOCK/);
    const event=events.find(e=>e.type==='STOPPED_HTTP_BLOCK');
    assert.equal(event.page_url,initial.url);assert.equal(event.network_http_status,null);
    assert.equal(event.page_classification,'HTTP_BLOCK');
  }
  assert.equal(reads,1);
  assert.equal(events[0].type,'DOM_HTTP_BLOCK_RECHECK');
});
