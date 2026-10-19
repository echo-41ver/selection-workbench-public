import {classifyPageState} from './temu_flower_recovery_policy.mjs';
export function normalizeSearchText(value) {
  return String(value || '').trim().replace(/\s+/g, ' ').toLowerCase();
}

export function isActiveVerificationState(state = {}) {
  return classifyPageState(state&&typeof state==='object'?state:{})==='HUMAN_VERIFICATION';
}

export function decideSearchSubmission(currentValue, currentUrl, query) {
  const target = normalizeSearchText(query);
  let urlQuery = '';
  try { urlQuery = normalizeSearchText(new URL(String(currentUrl || '')).searchParams.get('search_key')); } catch {}
  if (urlQuery === target) return 'SKIP_ALREADY_TARGET_URL';
  if (normalizeSearchText(currentValue) === target) return 'ENTER_EXISTING_QUERY';
  return 'REPLACE_AND_ENTER';
}

export function isStableEmptySearchSample(state = {}) {
  let url;
  try { url = new URL(String(state.url || '')); } catch { return false; }
  const rect = state.inputRect || {};
  return /^www\.temu\.com$/i.test(url.hostname)
    && url.pathname === '/search_result.html'
    && !url.searchParams.get('search_key')
    && ['interactive', 'complete'].includes(String(state.readyState || '').toLowerCase())
    && state.searchReady === true
    && state.searchEnabled === true
    && Number(rect.width) >= 120
    && Number(rect.height) >= 24;
}

export function isStableExistingSearchSample(state = {}) {
  let url;
  try { url = new URL(String(state.url || '')); } catch { return false; }
  if (url.protocol !== 'https:' || !/^(?:www\.)?temu\.com$/i.test(url.hostname)
      || url.pathname !== '/search_result.html' || !url.searchParams.get('search_key')
      || isActiveVerificationState(state)) return false;
  return isStableEmptySearchSample({...state, url:'https://www.temu.com/search_result.html'});
}

export function isPostVerificationEmptySearchSample(state = {}) {
  let url;
  try { url = new URL(String(state.url || '')); } catch { return false; }
  const rect = state.inputRect || {};
  return /(?:^|\.)temu\.com$/i.test(url.hostname)
    && url.pathname === '/search_result.html'
    && !String(url.searchParams.get('search_key') || '').trim()
    && ['interactive', 'complete'].includes(String(state.readyState || '').toLowerCase())
    && state.searchReady === true
    && state.searchEnabled === true
    && Number(rect.width) >= 120
    && Number(rect.height) >= 24;
}

export function sameSearchInputGeometry(previous = {}, current = {}, tolerance = 2) {
  const a = previous.inputRect || {};
  const b = current.inputRect || {};
  return ['x', 'y', 'width', 'height'].every(key =>
    Number.isFinite(Number(a[key]))
    && Number.isFinite(Number(b[key]))
    && Math.abs(Number(a[key]) - Number(b[key])) <= tolerance);
}

export function bootstrapEntryPlan(mode, currentUrl, query) {
  if (!['home_search', 'empty_search', 'direct_search'].includes(mode)) throw new Error('Unsupported TEMU entry mode');
  let url;
  try { url = new URL(String(currentUrl || '')); } catch {}
  if (url?.protocol === 'https:' && /^(?:www\.)?temu\.com$/i.test(url.hostname)
      && url.pathname === '/search_result.html' && url.searchParams.get('search_key')) {
    return normalizeSearchText(url.searchParams.get('search_key')) === normalizeSearchText(query)
      ? 'CURRENT_TARGET' : 'CURRENT_SEARCH_INPUT';
  }
  return mode;
}

export function isStableHomeSearchSample(state = {}) {
  let url;
  try { url = new URL(String(state.url || '')); } catch { return false; }
  return url.protocol === 'https:' && /^(?:www\.)?temu\.com$/i.test(url.hostname)
    && url.pathname === '/' && !isActiveVerificationState(state)
    && isStableEmptySearchSample({...state, url:'https://www.temu.com/search_result.html'});
}
