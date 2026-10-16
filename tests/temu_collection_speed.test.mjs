import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';
import {expansionObserved} from '../scripts/temu_control_click.mjs';
const source=fs.readFileSync(new URL('../scripts/temu_flower_collect.mjs',import.meta.url),'utf8');
const body=(name,next)=>source.slice(source.indexOf('async function '+name),source.indexOf('async function '+next));

test('first page saves cards and jumps to its real See more instead of gradual scrolling',async()=>{
 const calls=[];const context=vm.createContext({EKEY:'element',seeMoreBottomOffset:null,
  scan:async()=>{calls.push('scan');return {};},classify:()=>null,
  findSeeMoreFromCurrentDom:async()=>{calls.push('position');return {element:'more'};},
  recordCollectionScroll:async()=>assert.fail('visible control does not certify a scroll'),visibleScrollToBottom:async()=>assert.fail('unnecessary gradual scroll')});
 vm.runInContext(body('visibleScrollToLearnedSeeMoreZone','rememberSeeMoreBottomOffset'),context);
 await vm.runInContext('visibleScrollToLearnedSeeMoreZone()',context);
 assert.deepEqual(calls,['scan','position','scan']);
});
test('a long direct jump records the scrolling phase only after actual movement',async()=>{
 for(const moved of [false,true]){
  let probes=0,records=0;const deltas=[];
  const context=vm.createContext({EKEY:'element',round:0,products:new Map(),seeMoreBottomOffset:null,
   findVisibleSeeMore:async()=>++probes===1?null:{element:'more'},
   execute:async(script,args)=>{if(!args)return {top:5000,height:40};deltas.push(args[0]);return moved?4620:0;},
   lightPageState:async()=>({scrollY:0,innerHeight:800}),sleep:async()=>{},appendEvent:async()=>{},recordCollectionScroll:async()=>{records++;}});
  vm.runInContext(source.slice(source.indexOf('async function findSeeMoreFromCurrentDom()'),source.indexOf('class InitialProductsStopped')),context);
  await vm.runInContext('findSeeMoreFromCurrentDom()',context);
  assert.deepEqual(deltas,[4620]);assert.equal(records,moved?1:0);
 }
});
test('verification prevents jumping; an absent real control retains the existing fallback',async()=>{
 for(const blocked of [true,false]){
  let positioned=0,fallback=0;
  const context=vm.createContext({EKEY:'element',seeMoreBottomOffset:null,scan:async()=>({blocked}),classify:s=>s.blocked,
   findSeeMoreFromCurrentDom:async()=>{positioned++;return null;},visibleScrollToBottom:async()=>{fallback++;return {};}});
  vm.runInContext(body('visibleScrollToLearnedSeeMoreZone','rememberSeeMoreBottomOffset'),context);
  await vm.runInContext('visibleScrollToLearnedSeeMoreZone()',context);
  assert.equal(positioned,blocked?0:1);assert.equal(fallback,blocked?0:1);
 }
});
test('expansion continues as soon as new cards settle; delayed loading and blocks are preserved',async()=>{
 for(const mode of ['fast','slow','blocked','no-change']){
  let time=0,polls=0,scans=0;const before={visibleGoodsIds:['1'],visibleGoodsCount:1};
  const context=vm.createContext({Date:{now:()=>time},sleep:async ms=>{time+=ms;},expansionObserved,
   lightPageState:async()=>{polls++;return {visibleGoodsIds:mode==='no-change'?['1']:['1','2'],visibleGoodsCount:mode==='no-change'?1:2,scrollHeight:2000,
    loading:mode==='slow'&&polls<20,blocked:mode==='blocked'};},classify:s=>s.blocked,
   scan:async()=>{scans++;return {scans};}});
  vm.runInContext(body('waitForExpansionToSettle','retrySeeMoreClick'),context);
  context.before=before;await vm.runInContext('waitForExpansionToSettle(1,before)',context);
  if(mode==='fast'){assert.equal(time,750);assert.equal(scans,1);}
  if(mode==='slow'){assert.equal(time,5000);assert.equal(scans,1);}
  if(mode==='blocked'){assert.equal(polls,1);assert.equal(scans,0);}
  if(mode==='no-change'){assert.equal(time,10000);assert.equal(scans,1);}
 }
});
