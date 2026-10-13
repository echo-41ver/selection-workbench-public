import {classifyPageState} from './temu_flower_recovery_policy.mjs';

// An API rejection can be followed by the site's normal verification redirect.
// Observe that transition without refreshing, resubmitting, or changing session.
export async function observeHttpPageTransition({state, block, readState,
  sleep, now=Date.now, maxWaitMs=10000, pollMs=250, onWaiting=async()=>{}}) {
  if (![403,429].includes(Number(block?.status))) return {state,waitedMs:0,reason:'NO_HTTP_REJECTION'};
  const started=now();
  let current=state;
  let announced=false;
  while (true) {
    const kind=classifyPageState(current);
    if (kind) return {state:current,waitedMs:now()-started,reason:kind};
    if (Number(current?.visibleGoodsCount)>0 || current?.rows?.length>0) {
      return {state:current,waitedMs:now()-started,reason:'VISIBLE_RESULTS'};
    }
    const elapsed=now()-started;
    if (elapsed>=maxWaitMs) return {state:current,waitedMs:elapsed,reason:'TRANSITION_NOT_OBSERVED'};
    if (!announced) {announced=true;await onWaiting({http_status:Number(block.status),max_wait_ms:maxWaitMs});}
    await sleep(Math.min(pollMs,maxWaitMs-elapsed));
    try {current=await readState() || {...current,transientStateUnavailable:true};}
    catch {current={...current,transientStateUnavailable:true};}
  }
}
