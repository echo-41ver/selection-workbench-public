import { classifyPageState } from './temu_flower_recovery_policy.mjs';

export const DEFAULT_INITIAL_PRODUCTS_TIMEOUT_SECONDS = 180;

export function emptyQueryLabel(text='',query='') {
  const normalize=value=>String(value||'').normalize('NFKC').replace(/[\u200B-\u200D\uFEFF]/g,'').trim().replace(/\s+/g,' ').toLowerCase();
  const chinese=String(text).match(/(?:没有找到|沒有找到|没找到|沒找到|未找到|未能找到|没有|沒有|找不到)\s*[“"「『](.+?)[”"」』]\s*的(?:搜索)?结果|(?:没有找到|沒有找到|没找到|沒找到|未找到|未能找到|没有|沒有|找不到)\s*[“"「『](.+?)[”"」』]\s*的(?:搜尋)?結果/);
  if(chinese)return normalize(chinese[1]||chinese[2])===normalize(query)?normalize(chinese[0]):null;
  const english=String(text).match(/no results for\s*(?:["“]([^"”\r\n]*)["”]|['‘]([^'’\r\n]*)['’]|([^\r\n]+?)(?=,\s*(?:try|please)\s+search(?:ing)?\b|[\r\n]|$))/i);
  if(english)return normalize(english[1]??english[2]??english[3])===normalize(query)?normalize(english[0]):null;
  const generic=String(text).match(/没有搜索结果|沒有搜尋結果|未找到任何(?:相关)?结果|未找到任何(?:相關)?結果|no (?:search )?results found|no results matching|we couldn['’]t find any results/i);
  return generic?normalize(generic[0]):null;
}

export function confirmedEmptyQueryResults(state={},query='') {
  if(state.loading!==false || Number(state.loadingPlaceholderCount)>0 || state.transientStateUnavailable)return false;
  if(classifyPageState(state) || state.loginFormVisible || Number(state.visibleGoodsCount)>0 || state.rows?.length) return false;
  let url;try{url=new URL(String(state.url||''));}catch{return false;}
  const normalize=value=>String(value||'').trim().replace(/\s+/g,' ').toLowerCase();
  if(!/^(?:www\.)?temu\.com$/i.test(url.hostname) || url.pathname!='/search_result.html'
    || !normalize(query) || normalize(url.searchParams.get('search_key'))!==normalize(query))return false;
  if(state.readyState && !['interactive','complete'].includes(state.readyState))return false;
  return emptyQueryLabel(state.text,query)!==null;
}

export function decideEmptyQueryResults({state={},query='',previous=null,nowMs}={}) {
  let target=false;try{target=new URL(state.url).searchParams.get('search_key')?.trim().toLowerCase()===query.trim().toLowerCase();}catch{}
  if(!target)return {clock:null,confirmed:false};
  const targetAt=previous?.targetAt??nowMs;
  const candidate=confirmedEmptyQueryResults(state,query)
    && String(state.searchInputValue||'').trim().toLowerCase()===query.trim().toLowerCase();
  const label=emptyQueryLabel(state.text,query);
  const same=candidate&&previous?.candidate&&previous.label===label;
  const firstAt=same?previous.firstAt:nowMs;
  const samples=candidate?(same?previous.samples+1:1):0;
  const stableMs=candidate?Math.max(0,nowMs-firstAt):0;
  return {clock:{targetAt,firstAt,samples,candidate,label},
    confirmed:candidate&&samples>=3&&stableMs>=15000&&nowMs-targetAt>=30000,
    stableMs,samples};
}

// One clock covers every initial-search/bootstrap loop. Human intervention
// pauses it; changing the search phase must not grant a fresh timeout.
export function decideInitialProductsWait({ pageState = {}, previous = null, nowMs,
  timeoutMs = DEFAULT_INITIAL_PRODUCTS_TIMEOUT_SECONDS * 1000, productsReady = false } = {}) {
  const block = classifyPageState(pageState);
  const manual = block === 'HUMAN_VERIFICATION' || block === 'LOGIN_REQUIRED';
  const elapsed = previous ? Math.max(0, nowMs - previous.observedAtMs) : 0;
  const activeWaitMs = (previous?.activeWaitMs || 0)
    + (manual || previous?.manual ? 0 : elapsed);
  const clock = { observedAtMs: nowMs, activeWaitMs, manual };
  if (block === 'PROXY_OR_NETWORK_FAILURE' || block === 'ACCESS_DENIED') {
    return { clock, stop: true, manual: true, status: `STOPPED_${block}`, reason: block };
  }
  if (manual) {
    return { clock, stop: false, manual: true,
      status: block === 'LOGIN_REQUIRED' ? 'WAITING_LOGIN' : 'WAITING_CAPTCHA', reason: block };
  }
  if (productsReady) return { clock, stop: false, manual: false, status: 'INITIAL_PRODUCTS_READY' };
  if (activeWaitMs >= timeoutMs) {
    return { clock, stop: true, manual: true, status: 'STOPPED_INITIAL_PRODUCTS_TIMEOUT',
      reason: 'INITIAL_PRODUCTS_TIMEOUT' };
  }
  return { clock, stop: false, manual: false, status: 'WAITING_INITIAL_PRODUCTS' };
}
