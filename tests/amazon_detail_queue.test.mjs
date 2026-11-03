import test from 'node:test';
import {openAmazonDetail} from '../scripts/amazon_browser_recovery.mjs';
import {inspectAmazonPage,sanitizeAmazonDiagnostic} from '../scripts/amazon_page_state.mjs';
import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import path from 'node:path';
import os from 'node:os';
import vm from 'node:vm';
import {runAmazonDetailQueue as runQueue,amazonDetailBlock} from '../scripts/amazon_detail_queue.mjs';
const runAmazonDetailQueue=options=>runQueue({wait:async()=>{},...options});
import {serializedJsonWriter} from '../scripts/collection_checkpoint.mjs';
import {completeDetail} from './fixtures/amazon_detail.mjs';

const card=(asin,rank)=>({asin,rank,title:asin,product_url:`https://www.amazon.com/dp/${asin}`});
const success=completeDetail;
const timeout=row=>({...row,detail_status:'详情页失败：timeout',image_status:'详情页回退',
  detail_error:{code:'DETAIL_TIMEOUT',retryable:true,message:'timeout'}});
const noCheckpoint=async()=>{};

test('repeated missing specifications pause without dropping the item and explicit retry resumes it',async()=>{
  const requests=[];
  const result=await runAmazonDetailQueue({records:[card('A',1),success(card('B',2))],
    shouldStop:()=>requests.length>3,
    onCheckpoint:noCheckpoint,fetchDetail:async(row,attempt)=>{
      requests.push(attempt);
      assert.ok(attempt<=3,'Unchanged missing fields must not loop indefinitely');
      return {...row,detail_status:'详情页待补全',detail_error:{code:'DETAIL_QUALITY_INCOMPLETE',retryable:true,
        message:'详情证据不足：specifications_or_features',missing_fields:['specifications_or_features']}};
    }});
  assert.deepEqual(requests,[1,2,3]);
  assert.equal(result.queue.status,'MANUAL_ACTION_REQUIRED');
  assert.deepEqual(result.queue.remaining_asins,['A']);
  assert.equal(result.records[0].detail_error.retryable,false);
  const resumed=await runAmazonDetailQueue({records:result.records,recheckManual:true,
    onCheckpoint:noCheckpoint,fetchDetail:async(row,attempt)=>{
      assert.equal(row.asin,'A');assert.equal(attempt,4);return success(row);
    }});
  assert.equal(resumed.queue.status,'COMPLETE');
  assert.equal(resumed.requests,1);
});

test('failed details wait behind every first item, then succeed without replacing ranks',async()=>{
  const requests=[],saved=[];
  const result=await runAmazonDetailQueue({records:[card('A',1),card('B',2),card('C',3)],maxAttempts:3,concurrency:1,
    fetchDetail:async(row,attempt)=>{requests.push([row.asin,attempt]);return row.asin==='A'&&attempt===1?timeout(row):success(row);},
    onCheckpoint:async value=>saved.push(structuredClone(value))});
  assert.deepEqual(requests,[['A',1],['B',1],['C',1],['A',2]]);
  assert.deepEqual(result.records.map(row=>[row.asin,row.rank]),[['A',1],['B',2],['C',3]]);
  assert.deepEqual(result.queue.remaining_asins,[]);
  assert.equal(result.retries,1);
  assert.ok(saved.some(state=>state.queue.items.find(item=>item.asin==='A').status==='RETRY_PENDING'));
});

test('concurrent first wave finishes before any failed item is requested twice',async()=>{
  const requests=[];let releaseB;
  const bGate=new Promise(resolve=>{releaseB=resolve;});
  const cStarted=new Promise(resolve=>{
    runAmazonDetailQueue({records:[card('A',1),card('B',2),card('C',3)],maxAttempts:2,concurrency:2,
      fetchDetail:async(row,attempt)=>{
        requests.push([row.asin,attempt]);
        if(row.asin==='B'){await bGate;return success(row);}
        if(row.asin==='C'){resolve();return success(row);}
        return attempt===1?timeout(row):success(row);
      },onCheckpoint:noCheckpoint}).then(result=>{finished(result);},error=>{failed(error);});
  });
  let finished,failed;
  const done=new Promise((resolve,reject)=>{finished=resolve;failed=reject;});
  await cStarted;
  assert.deepEqual(requests,[['A',1],['B',1],['C',1]]);
  releaseB();const result=await done;
  assert.deepEqual(requests.at(-1),['A',2]);assert.deepEqual(result.queue.remaining_asins,[]);
});

test('legacy exhausted rows keep retrying beyond ten while completed rows are preserved',async()=>{
  const requests=[];
  const result=await runAmazonDetailQueue({records:[timeout({...card('A',1),detail_image_attempts:10}),success(card('B',2))],
    concurrency:1,onCheckpoint:noCheckpoint,fetchDetail:async(row,attempt)=>{
      requests.push([row.asin,attempt]);return attempt<13?timeout(row):success(row);
    }});
  assert.deepEqual(requests,[['A',11],['A',12],['A',13]]);
  assert.equal(result.queue.status,'COMPLETE');assert.equal(result.queue.max_attempts,null);
  assert.deepEqual(result.queue.exhausted_asins,[]);
  assert.equal(result.records[0].detail_image_attempts,13);
});

test('persistent failures remain queued with bounded backoff and stop only on user cancellation',async()=>{
  const delays=[],snapshots=[];let cancelled=false;
  const result=await runAmazonDetailQueue({records:[card('A',1)],retryDelayMs:5000,maxRetryDelayMs:6000,
    fetchDetail:async row=>timeout(row),onCheckpoint:async state=>snapshots.push(structuredClone(state)),
    wait:async ms=>{delays.push(ms);if(delays.length===8)cancelled=true;},shouldStop:()=>cancelled});
  assert.equal(result.queue.status,'STOPPED');assert.deepEqual(result.queue.retry_pending_asins,['A']);
  assert.equal(result.requests,2);assert.ok(delays.every(ms=>ms<=1000));
  assert.ok(snapshots.some(state=>state.queue.next_retry_at));assert.deepEqual(result.queue.exhausted_asins,[]);
});

test('checkpoint restart preserves successful records and reserved in-flight attempt count',async()=>{
  const directory=await fs.mkdtemp(path.join(os.tmpdir(),'amazon-detail-queue-'));
  try {
    const file=path.join(directory,'checkpoint.json'),write=serializedJsonWriter(file),requests=[];
    await assert.rejects(runAmazonDetailQueue({records:[card('A',1),card('B',2),card('C',3)],concurrency:1,maxAttempts:3,
      fetchDetail:async(row,attempt)=>{requests.push([row.asin,attempt]);return success(row);},
      onCheckpoint:async state=>{
        await write(state);
        if(state.queue.items.find(item=>item.asin==='B').status==='IN_PROGRESS') throw new Error('simulated process interruption');
      }}),/interruption/);
    const saved=JSON.parse(await fs.readFile(file,'utf8'));
    assert.deepEqual(requests,[['A',1]]);
    assert.equal(saved.queue.items[1].attempts,1);
    const resumed=await runAmazonDetailQueue({records:saved.records,concurrency:1,maxAttempts:3,
      fetchDetail:async(row,attempt)=>{requests.push([row.asin,attempt]);return success(row);},onCheckpoint:write});
    assert.deepEqual(requests,[['A',1],['C',1],['B',2]]);
    assert.deepEqual(resumed.records.map(row=>row.asin),['A','B','C']);
    assert.deepEqual(resumed.queue.remaining_asins,[]);
  } finally {await fs.rm(directory,{recursive:true,force:true});}
});

test('verification and HTTP limits stop only that item and do not retry through a gate',async()=>{
  const blocked=amazonDetailBlock({http_status:200,captcha:true,text:'Continue shopping'});
  assert.equal(blocked.code,'AMAZON_VERIFICATION_REQUIRED');
  for(const status of [403,429])assert.equal(amazonDetailBlock({http_status:status}).retryable,false);
  assert.equal(amazonDetailBlock({http_status:200,captcha:false,text:'normal product'}),null);
  const requests=[];
  const result=await runAmazonDetailQueue({records:[card('A',1),card('B',2)],maxAttempts:3,concurrency:1,
    fetchDetail:async row=>{requests.push(row.asin);return row.asin==='A'?{...timeout(row),detail_error:blocked}:success(row);},onCheckpoint:noCheckpoint});
  assert.deepEqual(requests,['A','B']);assert.deepEqual(result.queue.manual_action_asins,['A']);
  assert.equal(result.queue.status,'MANUAL_ACTION_REQUIRED');assert.equal(result.queue.completed_count,1);
  const resumed=await runAmazonDetailQueue({records:result.records,onCheckpoint:noCheckpoint,
    fetchDetail:async()=>assert.fail('Blocked or successful items must not be requested again')});
  assert.equal(resumed.requests,0);
});

test('legacy eight-attempt item retains history and preserves a real manual gate',async()=>{
  const prior=timeout({...card('A',1),detail_image_attempts:8});
  const requests=[];
  const result=await runAmazonDetailQueue({records:[prior],onCheckpoint:noCheckpoint,
    fetchDetail:async(row,attempt)=>{requests.push(attempt);return {...timeout(row),detail_error:amazonDetailBlock({captcha:true})};}});
  assert.deepEqual(requests,[9]);assert.equal(result.records[0].detail_image_attempts,9);
  assert.deepEqual(result.queue.manual_action_asins,['A']);assert.equal(result.queue.max_attempts,null);
});

test('a thrown detail request is queued while unrelated products still finish',async()=>{
  const requests=[];
  const result=await runAmazonDetailQueue({records:[card('A',1),card('B',2)],maxAttempts:2,concurrency:1,onCheckpoint:noCheckpoint,
    fetchDetail:async(row,attempt)=>{requests.push(row.asin);if(row.asin==='A'&&attempt===1)throw new Error('network timeout');return success(row);}});
  assert.deepEqual(requests,['A','B','A']);assert.deepEqual(result.queue.remaining_asins,[]);
});

test('real collector classifies the verification page before waiting for a title',async()=>{
  const source=await fs.readFile(new URL('../scripts/collect_amazon_search_frontend.mjs',import.meta.url),'utf8');
  const facts=source.slice(source.indexOf('async function detailFacts('),source.indexOf('const browser ='));
  for(const captcha of [true,false]){
    let closed=0,waits=0;
    const page={goto:async()=>({status:()=>200}),url:()=>'',waitForTimeout:async()=>{},close:async()=>{closed++;},
      evaluate:async()=>({captcha,url:'https://www.amazon.com/dp/A',page_title:'Amazon.com',text:captcha?'Continue shopping':'loading'}),
      locator:()=>({first:()=>({waitFor:async()=>{waits++;throw new Error('Timeout 15000ms exceeded');}})})};
    const context=vm.createContext({amazonDetailBlock,openAmazonDetail,inspectAmazonPage,sanitizeAmazonDiagnostic,context:{newPage:async()=>page},record:card('A',1),
      clean:value=>String(value).replace(/\s+/g,' ').trim()});
    vm.runInContext(facts,context);
    const result=await vm.runInContext('detailFacts(context,record,9)',context);
    assert.equal(result.detail_error.code,captcha?'AMAZON_VERIFICATION_REQUIRED':'DETAIL_TIMEOUT');
    assert.equal(result.detail_error.retryable,!captcha);
    assert.equal(waits,captcha?0:2);assert.equal(closed,1);assert.equal(result.detail_image_attempts,9);
  }
});

test('unresolved continuation is surfaced once and does not consume network retry waves',async()=>{
  const requests=[];
  const blocked=amazonDetailBlock({continuation_required:true,continue_shopping:false,http_status:200});
  assert.equal(blocked.code,'AMAZON_CONTINUATION_REQUIRED');assert.equal(blocked.retryable,false);
  const result=await runAmazonDetailQueue({records:[card('A',1),card('B',2)],onCheckpoint:noCheckpoint,
    fetchDetail:async row=>{requests.push(row.asin);return row.asin==='A'?{...timeout(row),detail_error:blocked}:success(row);}});
  assert.deepEqual(requests,['A','B']);assert.equal(result.queue.status,'MANUAL_ACTION_REQUIRED');
  assert.deepEqual(result.queue.manual_action_asins,['A']);assert.equal(result.queue.completed_count,1);
});

test('collector preserves a failed visible continuation reason instead of relabelling it as retryable',async()=>{
  const source=await fs.readFile(new URL('../scripts/collect_amazon_search_frontend.mjs',import.meta.url),'utf8');
  const facts=source.slice(source.indexOf('async function detailFacts('),source.indexOf('const browser ='));
  let closed=0,clicks=0;
  const page={goto:async()=>({status:()=>200}),close:async()=>{closed++;},
    evaluate:async()=>({url:'https://www.amazon.com/checkpoint/continue?token=fixture-sensitive',
      captcha:false,continue_shopping:true,continuation_required:true}),
    waitForNavigation:async()=>({status:()=>200}),getByRole:()=>({click:async()=>{clicks++;}})};
  const context=vm.createContext({amazonDetailBlock,openAmazonDetail,inspectAmazonPage,sanitizeAmazonDiagnostic,
    context:{newPage:async()=>page},record:card('A',1),clean:value=>String(value).replace(/\s+/g,' ').trim()});
  vm.runInContext(facts,context);
  const result=await vm.runInContext('detailFacts(context,record,8)',context);
  assert.equal(result.detail_error.code,'AMAZON_CONTINUATION_REQUIRED');assert.equal(result.detail_error.retryable,false);
  assert.equal(result.detail_image_attempts,8);assert.equal(closed,1);assert.equal(clicks,1);
  assert.doesNotMatch(JSON.stringify(result),/fixture-sensitive/);
});

test('saved checkpoint errors strip navigation tokens while retaining item history',async()=>{
  const prior={...card('A',1),detail_image_attempts:7,detail_error:{code:'AMAZON_CONTINUATION_REQUIRED',retryable:false,
    url:'https://www.amazon.com/checkpoint/continue?token=fixture-sensitive#private',
    message:'Navigation to https://www.amazon.com/checkpoint/continue?token=fixture-sensitive failed'}};
  const result=await runAmazonDetailQueue({records:[prior],onCheckpoint:noCheckpoint,fetchDetail:()=>assert.fail('manual gate must remain paused')});
  const serialized=JSON.stringify(result);
  assert.doesNotMatch(serialized,/fixture-sensitive|#private/);
  assert.equal(result.records[0].detail_image_attempts,7);
  assert.equal(result.records[0].detail_error.url,'https://www.amazon.com/checkpoint/continue');
});

test('old misclassified continue-shopping entry resumes once without resetting attempts',async()=>{
  const prior={...card('A',1),detail_image_attempts:9,detail_error:{code:'AMAZON_VERIFICATION_REQUIRED',retryable:false,
    captcha:true,text:'Click the button below to continue shopping Continue shopping Conditions of Use Privacy Policy'}};
  const attempts=[];
  const result=await runAmazonDetailQueue({records:[prior],onCheckpoint:noCheckpoint,fetchDetail:async(row,attempt)=>{
    attempts.push(attempt);return success(row);
  }});
  assert.deepEqual(attempts,[10]);assert.equal(result.queue.status,'COMPLETE');
});
