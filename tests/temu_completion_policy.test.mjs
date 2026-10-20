import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';
import {createRawCounter,RAW_COUNT_VERSION} from '../scripts/temu_raw_count.mjs';
const source=fs.readFileSync(new URL('../scripts/temu_flower_collect.mjs',import.meta.url),'utf8');

test('replayed pages below the saved quota keep loading beyond ten no-new rounds',()=>{
 const condition=source.match(/while \((!finalStatus[^\n]+rawCounter\.count < MAX_PRODUCTS)\) \{/)[1];
 const rawCounter=createRawCounter(1200,{version:RAW_COUNT_VERSION,count:880});
 const context=vm.createContext({finalStatus:null,rawCounter,MAX_PRODUCTS:1200,consecutiveNoNewRounds:999,clicksSinceRecovery:999,IDLE_REQUIRED:10});
 for(let n=40;n<1200;n+=40){rawCounter.observe(Array.from({length:n},()=>({goods_id:'same'})),'fresh-document',true);
  assert.equal(vm.runInContext(condition,context),true);}
 rawCounter.observe(Array.from({length:1200},()=>({goods_id:'same'})),'fresh-document',true);
 assert.equal(vm.runInContext(condition,context),false);
});

test('natural exit below quota never manufactures completion, explicit end remains accepted',async()=>{
 const code=source.slice(source.indexOf('finalStatus ||= rawCounter.count'),source.indexOf('if (bidi) await bidi.close();',source.indexOf('finalStatus ||= rawCounter.count')));
 for(const [count,explicit,expected] of [[880,false,'STOPPED_COLLECTION_BELOW_QUOTA'],[600,true,'KEYWORD_COMPLETE'],[1200,false,'KEYWORD_LIMIT_REACHED']]){
  const events=[],snapshots=[];const context=vm.createContext({finalStatus:explicit?'KEYWORD_COMPLETE':null,rawCounter:{count},MAX_PRODUCTS:1200,
   noMoreResultsConfirmed:explicit,round:100,clicks:100,products:new Map(),QUERY:'washer',now:()=>'',
   appendEvent:async(...args)=>events.push(args),writeSnapshot:async(...args)=>snapshots.push(args)});
  await vm.runInContext(`(async()=>{${code}})()`,context);
  assert.equal(context.finalStatus,expected);
  assert.equal(events.length,expected==='STOPPED_COLLECTION_BELOW_QUOTA'?0:1);
  assert.equal(snapshots.at(-1)[0],expected);
  if(expected==='STOPPED_COLLECTION_BELOW_QUOTA')assert.equal(snapshots.at(-1)[1].automatic_retry,true);
 }
});
