import {usefulSpecifications} from './amazon_detail_quality.mjs';
import {detailComplete} from './collection_checkpoint.mjs';
import {recoverMisclassifiedContinuation,sanitizeAmazonDiagnostic} from './amazon_page_state.mjs';

export function amazonDetailBlock(evidence = {}) {
  if (evidence.captcha || /robot check|enter the characters you see|type the characters you see/i.test(evidence.text || '')) {
    return {code:'AMAZON_VERIFICATION_REQUIRED',retryable:false,message:'Amazon 验证页面，需要人工处理'};
  }
  if ([403,429].includes(evidence.http_status)) {
    return {code:`AMAZON_HTTP_${evidence.http_status}`,retryable:false,message:`Amazon HTTP ${evidence.http_status}，暂停该商品自动重试`};
  }
  if (/\/ap\/signin(?:[/?]|$)/i.test(evidence.url || '')) {
    return {code:'AMAZON_LOGIN_REQUIRED',retryable:false,message:'Amazon 登录页面，需要人工处理'};
  }
  if (evidence.continuation_required && !evidence.continue_shopping) {
    return {code:'AMAZON_CONTINUATION_REQUIRED',retryable:false,message:'Amazon 继续购物入口尚未解除，已保留商品断点，需要人工处理'};
  }
  return null;
}

// Recheck paused gates only after an explicit user continuation.
// Keep its saved error non-retryable until the one request has finished: a crash
// or background restart must not turn this authorization into an automatic loop.
export function scheduleAmazonContinuationRecheck(items, authorized = false, manual = false) {
  const selected = new Set();
  if (!authorized && !manual) return selected;
  for (const item of items.values()) {
    const code=item.last_error?.code;
    if (item.status !== 'MANUAL_ACTION_REQUIRED' || !(authorized && code==='AMAZON_CONTINUATION_REQUIRED'
      || manual && ['AMAZON_CONTINUATION_REQUIRED','AMAZON_LOGIN_REQUIRED','AMAZON_VERIFICATION_REQUIRED',
        'AMAZON_HTTP_403','AMAZON_HTTP_429'].includes(code))) continue;
    item.status = 'PENDING';
    selected.add(item.asin);
  }
  return selected;
}

export async function runAmazonDetailQueue({records,fetchDetail,onCheckpoint,repairImage,concurrency=4,
  retryDelayMs=5000,maxRetryDelayMs=60000,wait=ms=>new Promise(resolve=>setTimeout(resolve,ms)),shouldStop=()=>false,
  recheckContinuation=false,recheckManual=false}) {
  if (!Number.isFinite(retryDelayMs) || retryDelayMs<0 || !Number.isFinite(maxRetryDelayMs) || maxRetryDelayMs<retryDelayMs) throw new Error('Invalid detail retry delay');
  if (!Number.isInteger(concurrency) || concurrency<1 || concurrency>10) throw new Error('detail_concurrency must be 1..10');
  records=records.map(recoverMisclassifiedContinuation).map(row=>row.detail_error
    ? {...row,detail_error:sanitizeAmazonDiagnostic(row.detail_error)} : row);
  // Repair legacy image evidence without discarding the request history.
  if(repairImage) for(let i=0;i<records.length;i++) {
    const row=records[i];
    if(!detailComplete(row) && !row.native_image_recovery_attempted && row.detail_error?.retryable!==false
      && row.detail_quality?.title_verified && usefulSpecifications(row.specifications).length && row.image_candidates?.length) {
      records[i]={...row,native_image_recovery_attempted:true};
      await onCheckpoint({records,queue:null});
      const repaired=await repairImage(records[i]);
      records[i]={...repaired,native_image_recovery_attempted:true};
      await onCheckpoint({records,queue:null});
    }
  }
  const active=new Map(records.map(row=>[row.asin,{...row}]));
  if (active.size!==records.length) throw new Error('Duplicate ASINs in detail queue');
  const items=new Map(records.map(row=>{
    const attempts=Math.max(0,Math.floor(Number(row.detail_image_attempts)||0));
    const status=detailComplete(row)?'COMPLETE':row.detail_error?.retryable===false?'MANUAL_ACTION_REQUIRED'
      :attempts?'RETRY_PENDING':'PENDING';
    return [row.asin,{asin:row.asin,rank:row.rank,attempts,status,last_error:row.detail_error || null}];
  }));
  const continuationRechecks=scheduleAmazonContinuationRecheck(items,recheckContinuation,recheckManual);
  let requests=0,retries=0,stopped=false,nextRetryAt=null;
  const retriedAsins=new Set();
  const snapshot=()=>{
    const current=[...items.values()].map(item=>({...item}));
    const remaining=current.filter(item=>item.status!=='COMPLETE').map(item=>item.asin);
    const pending=current.filter(item=>['PENDING','IN_PROGRESS','RETRY_PENDING'].includes(item.status)).map(item=>item.asin);
    const manual=current.filter(item=>item.status==='MANUAL_ACTION_REQUIRED').map(item=>item.asin);
    return {schema_version:'amazon-detail-queue-v2',max_attempts:null,retry_policy:'UNTIL_COMPLETE_OR_STOPPED',next_retry_at:nextRetryAt,
      status:stopped?'STOPPED':pending.length?'RUNNING':!remaining.length?'COMPLETE':'MANUAL_ACTION_REQUIRED',
      completed_count:current.length-remaining.length,remaining_count:remaining.length,
      items:current,remaining_asins:remaining,retry_pending_asins:pending,
      exhausted_asins:[],manual_action_asins:manual,
      continuation_recheck_asins:[...continuationRechecks]};
  };
  const persist=()=>onCheckpoint({records:[...active.values()],queue:snapshot()});
  const runWave=async pending=>{
    let next=0;
    // A failed item waits until this whole wave finishes. It cannot occupy a
    // retry slot ahead of an unattempted ranked item, even with concurrency.
    const workers=Array.from({length:Math.min(concurrency,pending.length)},async()=>{
      while(next<pending.length){
        if(await shouldStop()){stopped=true;break;}
        const item=pending[next++],row=active.get(item.asin);
        if(item.attempts){retries++;retriedAsins.add(item.asin);}
        item.attempts++;item.status='IN_PROGRESS';requests++;
        active.set(item.asin,{...row,detail_image_attempts:item.attempts});
        // Persist request history before fetching; completed rows stay untouched.
        await persist();
        let value;
        try {value=await fetchDetail(row,item.attempts);}
        catch(error){
          value={...row,detail_status:`详情页失败：${String(error.message).slice(0,160)}`,
            image_status:'详情页回退',detail_error:{code:'DETAIL_REQUEST_FAILED',retryable:true,message:String(error.message).slice(0,300)}};
        }
        value={...row,...value,asin:row.asin,rank:row.rank,product_url:row.product_url,detail_image_attempts:item.attempts};
        if(!detailComplete(value) && !value.detail_error) value.detail_error={code:'DETAIL_INCOMPLETE',retryable:true,message:value.detail_status || '详情或主图不完整'};
        if(continuationRechecks.has(item.asin) && !detailComplete(value) && value.detail_error?.retryable!==false) {
          value={...value,detail_error:{code:row.detail_error.code,retryable:false,
            message:'Amazon 显式重查未完成，已保留断点，等待再次手动继续',
            recheck_error:value.detail_error}};
        }
        if(value.detail_error)value={...value,detail_error:sanitizeAmazonDiagnostic(value.detail_error)};
        active.set(item.asin,value);
        item.last_error=detailComplete(value)?null:value.detail_error;
        item.status=detailComplete(value)?'COMPLETE':value.detail_error?.retryable===false?'MANUAL_ACTION_REQUIRED'
          :'RETRY_PENDING';
        await persist();
      }
    });
    const results=await Promise.allSettled(workers);
    const failure=results.find(result=>result.status==='rejected');
    if(failure) throw failure.reason;
  };
  await persist();
  await runWave([...items.values()].filter(item=>item.status==='PENDING'));
  for(;;){
    const pending=[...items.values()].filter(item=>item.status==='RETRY_PENDING');
    if(stopped || !pending.length) break;
    const attempts=Math.max(...pending.map(item=>item.attempts));
    let delay=Math.min(maxRetryDelayMs,retryDelayMs*2**Math.min(20,Math.max(0,attempts-1)));
    nextRetryAt=new Date(Date.now()+delay).toISOString();await persist();
    while(delay>0){
      if(await shouldStop()){stopped=true;break;}
      const chunk=Math.min(delay,1000);await wait(chunk);delay-=chunk;
    }
    nextRetryAt=null;
    if(await shouldStop())stopped=true;
    if(stopped)break;
    await runWave(pending);
  }
  await persist();
  return {records:[...active.values()],queue:snapshot(),requests,retries,retried_asins:[...retriedAsins]};
}
