// Network diagnostics deliberately omit credentials, query strings and bodies.
export function endpoint(value) {
  try {
    const url = new URL(String(value || ''));
    return /^https?:$/.test(url.protocol) ? url.origin + url.pathname : url.protocol;
  } catch { return 'unknown'; }
}

// Only failures on the selected page's document or actual goods search can
// block collection. A chat/SEO/suggestion failure remains diagnostic evidence.
export function collectionRequest(evidence = {}) {
  if (evidence.selected_context !== true) return false;
  let url;
  try { url = new URL(String(evidence.endpoint || '')); } catch { return false; }
  if (url.protocol !== 'https:' || !/(?:^|\.)temu\.com$/i.test(url.hostname)) return false;
  if (evidence.navigation && evidence.resource_type === 'document') return true;
  return /^\/api\/(?:poppy\/v\d+\/search|search)\/?$/i.test(url.pathname);
}

export function retryAfterSeconds(headers = [], at = Date.now()) {
  const header = headers.find(item => String(item.name).toLowerCase() === 'retry-after');
  const value = header?.value;
  const raw = typeof value === 'string' ? value : value?.type === 'string' ? value.value : null;
  if (typeof raw !== 'string') return null;
  if (/^\s*\d+\s*$/.test(raw)) {
    const seconds = Number(raw);
    return Number.isFinite(seconds) ? seconds : null;
  }
  const deadline = Date.parse(raw);
  return Number.isFinite(deadline) ? Math.max(0, Math.ceil((deadline - at) / 1000)) : null;
}

export function networkEvidence(params, documentUrl, chosenContext) {
  const request = params.request || {};
  return {
    endpoint: endpoint(params.response?.url || request.url),
    document_endpoint: endpoint(documentUrl),
    method: String(request.method || 'UNKNOWN').slice(0, 16),
    resource_type: String(request.destination || 'other').slice(0, 32),
    selected_context: params.context === chosenContext,
    navigation: Boolean(params.navigation),
    status: Number(params.response?.status) || null,
    retry_after_seconds: retryAfterSeconds(params.response?.headers),
    // Error messages may contain complete URLs; retain error codes only.
    error_code: String(params.errorText || '').match(/\b(?:NS_ERROR_[A-Z0-9_]+|ERR_[A-Z0-9_]+)\b/)?.[0] || null,
  };
}

export function diagnosticLimiter(limit = 200, repeatMs = 30000) {
  const seen = new Map();
  let count = 0;
  return (evidence, at = Date.now()) => {
    const key = JSON.stringify(evidence);
    if (count >= limit || (seen.has(key) && at - seen.get(key) < repeatMs)) return false;
    seen.set(key, at); count++;
    return true;
  };
}

export function supersededByVerification(state, block) {
  if (!block || ![403,429].includes(Number(block.status))) return false;
  try {
    const current = new URL(state.url);
    const origin = new URL(block.document_endpoint);
    return current.origin === origin.origin
      && (state.verificationVisible === true || /(?:bgn_verification|verification|captcha|verifyCode)/i.test(current.pathname))
      && origin.pathname === '/search_result.html';
  } catch { return false; }
}

export function supersededBySuccess(evidence, block) {
  return Boolean(block && evidence.status >= 200 && evidence.status < 300
    && collectionRequest(evidence) && evidence.endpoint === block.endpoint);
}
