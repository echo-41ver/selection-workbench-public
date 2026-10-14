import fs from 'node:fs/promises';
import path from 'node:path';
import {atomicSnapshot} from './snapshot_atomic.mjs';
import { classifyPageState } from './temu_flower_recovery_policy.mjs';
import { detailQualityComplete } from './amazon_detail_quality.mjs';

export const detailComplete = row => row?.detail_status === '详情页已采集' && row?.image_status === '详情页主图' && detailQualityComplete(row);
export const amazonCollectionStatus = rows => rows.length > 0 && rows.every(detailComplete) ? 'COMPLETE' : 'PARTIAL_DETAIL_FAILURE';

export function selectAmazonMainImage(candidates) {
  const valid = candidates.filter(item => {
    try { return new URL(item.url).protocol === 'https:'; } catch { return false; }
  });
  // Amazon's data-old-hires is the original image. Its dimensions are often
  // omitted, which must not rank it below a known 679px display rendition.
  const original = valid.find(item => item.source === '详情页原始主图');
  if (original) return original;
  return valid.sort((a, b) => Math.max(...(b.size || [0])) - Math.max(...(a.size || [0])))[0] || null;
}

export function resumableAmazonRecords(checkpoint, scope) {
  if (!checkpoint || checkpoint.status === 'COMPLETE') return null;
  if (checkpoint.schema_version !== 'amazon-detail-checkpoint-v1'
      || JSON.stringify(checkpoint.scope) !== JSON.stringify(scope)) {
    throw new Error('Amazon checkpoint belongs to a different collection scope; use --fresh to start a new observation');
  }
  const rows = checkpoint.records;
  if (!Array.isArray(rows) || rows.length !== scope.target_count
      || rows.some(row => !row.asin || !row.product_url || !row.title)
      || new Set(rows.map(row => row.asin)).size !== rows.length) {
    throw new Error('Amazon checkpoint is incomplete or invalid; refusing to overwrite it');
  }
  return rows;
}

export function serializedJsonWriter(destination, {writeSnapshot=atomicSnapshot}={}) {
  let pending = Promise.resolve();
  return value => {
    const body = JSON.stringify(value, null, 2);
    const write = pending.then(async () => {
      await fs.mkdir(path.dirname(destination), { recursive: true });
      await writeSnapshot(destination, body);
    });
    // The caller receives the real error; subsequent queued writes can recover.
    pending = write.catch(() => {});
    return write;
  };
}

export function missingControlState(previous, count, now, maxChecks, maxWaitMs) {
  // Newly observed products restart the bounded wait; missing controls never
  // become evidence of a complete keyword by themselves.
  const state = !previous || previous.count !== count
    ? { count, since: now, checks: 1 }
    : { ...previous, checks: previous.checks + 1 };
  return { ...state, exhausted: state.checks >= maxChecks || now - state.since >= maxWaitMs };
}

export function inspectSearchEnd() {
  const visible = element => {
    const rect = element.getBoundingClientRect(), style = getComputedStyle(element);
    return rect.width > 0 && rect.height > 0 && style.display !== 'none' && style.visibility !== 'hidden';
  };
  const labels = [...document.querySelectorAll('button,a,div,span,p')]
    .filter(visible).map(element => (element.textContent || '').trim());
  return {
    noMoreResultsVisible: labels.some(text => /^[\s—–·-]*(?:No more results?|没有更多结果|沒有更多結果|无更多结果|無更多結果)[.!。！]?[\s—–·-]*$/i.test(text)),
    seeMorePresent: labels.some(text => /^(?:See more|查看更多|顯示更多|显示更多)$/i.test(text)),
    tryAgainVisible: labels.some(text => /^(?:Try again|重试|重試)$/i.test(text)),
    pageFaultVisible: /^(?:about:neterror|chrome-error:)/i.test(document.documentURI || ''),
  };
}

export function confirmedNoMoreResults(first, second, {query, sortConfirmed, httpBlock} = {}) {
  if (!sortConfirmed || httpBlock || !query) return false;
  const healthy = state => {
    if (!state?.noMoreResultsVisible || state.seeMorePresent || state.tryAgainVisible || state.pageFaultVisible
        || classifyPageState(state) || Number(state.visibleGoodsCount) < 1) return false;
    try {
      const url = new URL(String(state.url || ''));
      return url.protocol === 'https:' && /(?:^|\.)temu\.com$/i.test(url.hostname)
        && url.pathname === '/search_result.html'
        && url.searchParams.get('search_key')?.trim().toLowerCase() === query.trim().toLowerCase();
    } catch { return false; }
  };
  return healthy(first) && healthy(second)
    && Number(first.visibleGoodsCount) === Number(second.visibleGoodsCount);
}

export function terminalCollectionStop(state, httpBlock = null) {
  const block = classifyPageState(state);
  if (block === 'HUMAN_VERIFICATION') return null;
  if (block === 'LOGIN_REQUIRED') return {
    status: 'STOPPED_LOGIN_REQUIRED', automatic_recovery_eligible: true,
    automatic_retry: true, manual_action_required: false,
    resume_requires_session_rotation: true,
  };
  const status = Number(httpBlock?.status);
  const visible = Number(state.visibleGoodsCount) > 0 || state.rows?.length > 0;
  if (!visible && (status === 403 || status === 429)) return {
    status: `STOPPED_HTTP_${status}`, automatic_recovery_eligible: true,
    resume_requires_session_rotation: true,
  };
  if (block === 'ACCESS_DENIED') return { status: 'STOPPED_ACCESS_DENIED', automatic_recovery_eligible: false };
  if (block === 'HTTP_BLOCK') return { status: 'STOPPED_HTTP_BLOCK', automatic_recovery_eligible: false };
  if (block === 'PROXY_OR_NETWORK_FAILURE') return {
    status: 'STOPPED_PROXY_CONNECTION', automatic_recovery_eligible: !httpBlock,
    resume_requires_session_rotation: !httpBlock,
  };
  return null;
}
