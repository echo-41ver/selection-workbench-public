import test from 'node:test';
import assert from 'node:assert/strict';
import vm from 'node:vm';
import fs from 'node:fs';
import {controlClickPoint,clickVisibleControl,expansionObserved} from '../scripts/temu_control_click.mjs';

test('off-screen text is centered and the actual button is hit-tested',()=>{
  let top=44;
  const button={isConnected:true,textContent:'See more',matches:()=>true,disabled:false,
    scrollIntoView:()=>{top=260;},getBoundingClientRect:()=>({left:20,right:220,top,bottom:top+40,width:200,height:40}),contains:e=>e===span};
  const span={isConnected:true,textContent:'See more',matches:()=>false,parentElement:button};
  const context=vm.createContext({element:span,innerWidth:800,innerHeight:600,
    getComputedStyle:()=>({cursor:'pointer'}),document:{elementFromPoint:(x,y)=>{assert.equal(y,280);return span;}}});
  const point=vm.runInContext(`(${controlClickPoint.toString()})(element)`,context);
  assert.equal(point.ready,true);assert.equal(point.y,280);
  context.document.elementFromPoint=()=>({textContent:'unrelated overlay'});
  assert.equal(vm.runInContext(`(${controlClickPoint.toString()})(element)`,context).reason,'OBSCURED');
});

test('pointer coordinates are remeasured and obscured controls are never forced',async()=>{
  const calls=[];let reads=0;
  await clickVisibleControl({element:{'element-6066-11e4-a52e-4f735466cecf':'control'},sleep:async()=>{},execute:async()=>({ready:true,x:10,y:++reads===1?20:40}),
    wd:async(method,endpoint,body)=>calls.push({method,endpoint,body})});
  assert.equal(reads,2);
  assert.equal(calls[0].endpoint,'/element/control/click');
  assert.equal(calls.length,1);
  await assert.rejects(()=>clickVisibleControl({element:{},sleep:async()=>{},execute:async()=>({ready:false,reason:'OBSCURED'}),
    wd:async()=>assert.fail('must not click overlay')}),/OBSCURED/);
});

test('expansion requires additional cards or new visible product IDs, not height or saved rows',()=>{
  const before={visibleGoodsCount:40,visibleGoodsIds:['1','2'],scrollHeight:1000};
  assert.equal(expansionObserved(before,{...before,scrollHeight:2000,unique_goods:100}),false);
  assert.equal(expansionObserved(before,{...before,visibleGoodsCount:60}),true);
  assert.equal(expansionObserved(before,{...before,visibleGoodsIds:['2','3']}),true);
});

test('native click uses the freshly resolved parent and never replays an uncertain timeout',async()=>{
 const calls=[];
 await assert.rejects(()=>clickVisibleControl({element:{'element-6066-11e4-a52e-4f735466cecf':'span'},
  sleep:async()=>{},execute:async()=>({ready:true,x:1,y:1,target:{'element-6066-11e4-a52e-4f735466cecf':'button'}}),
  wd:async(method,path)=>{calls.push([method,path]);throw Error('BROWSER_COMMAND_TIMEOUT');}}),/BROWSER_COMMAND_TIMEOUT/);
 assert.deepEqual(calls,[['POST','/element/button/click']]);
});

test('unsuccessful expansion retries never count as completed or saturated rounds',async()=>{
  const source=fs.readFileSync(new URL('../scripts/temu_flower_collect.mjs',import.meta.url),'utf8');
  const code=source.slice(source.indexOf('async function retrySeeMoreClick('),source.indexOf('async function revealSeeMoreAboveFooter('));
  const snapshots=[];
  const context=vm.createContext({failedSeeMoreClicks:0,consecutiveNoNewRounds:9,round:4,finalStatus:null,
    products:new Map(),appendEvent:async()=>{},writeSnapshot:async(status,details)=>snapshots.push({status,...details}),sleep:async()=>{}});
  vm.runInContext(code,context);
  for(let i=0;i<3;i++)await vm.runInContext("retrySeeMoreClick('PRODUCT_LIST_DID_NOT_EXPAND')",context);
  assert.equal(context.round,4);assert.equal(context.consecutiveNoNewRounds,0);
  assert.equal(context.finalStatus,'STOPPED_SEE_MORE_UNAVAILABLE');
  assert.equal(snapshots.at(-1).keyword_complete,false);
});
