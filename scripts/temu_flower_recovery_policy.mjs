export function classifyPageState(data = {}) {
  const url = String(data.url || '');
  const documentURI = String(data.documentURI || url);
  const text = String(data.text || '');
  const hasVisibleProducts = Number(data.visibleGoodsCount) > 0 || (data.rows?.length > 0);
  if (/^about:neterror/i.test(documentURI)
      || /proxy server is refusing connections|unable to connect|server not found|connection timed out/i.test(text)) {
    return 'PROXY_OR_NETWORK_FAILURE';
  }
  let pathname = '';
  try { pathname = new URL(url).pathname; } catch {}
  if (/\/(?:bgn_verification|captcha|challenge|security[-_]?verification)/i.test(pathname)
      || /security verification|verify (?:that )?you are human|complete the verification|安全验证|验证码|拖动滑块/i.test(text)) {
    return 'HUMAN_VERIFICATION';
  }
  if (/\/login(?:\.html)?\/?$/i.test(pathname)
      || data.loginFormVisible === true
      || (!/\/search(?:_result)?(?:\.html)?\/?$/i.test(pathname)
          && /sign in|register/i.test(text) && !hasVisibleProducts)) {
    return 'LOGIN_REQUIRED';
  }
  if (/access denied|request blocked|unusual traffic/i.test(text) && !hasVisibleProducts) {
    return 'ACCESS_DENIED';
  }
  // Counts/prices such as "已售429件" or "403条评价" are normal homepage
  // content, including while its product links have not hydrated yet.
  const explicitHttpError = /\b(?:HTTP(?:\/\d(?:\.\d)?)?\s*(?:error\s*)?|(?:error|status(?:\s+code)?)\s*[:：]?\s*)(?:403|429)\b/i;
  const errorHeading = /^(?:(?:HTTP(?:\/\d(?:\.\d)?)?\s*)?(?:error\s*)?)?(?:403|429)(?:\s*[-:：]?\s*(?:forbidden|too many requests|access denied))?$/i;
  const bodyLines = text.split(/\r?\n/).map(line => line.trim()).filter(Boolean);
  const confirmedError = explicitHttpError.test(text)
    || errorHeading.test(String(data.title || '').trim())
    || bodyLines.some(line => /^(?:403\s+forbidden|429\s+too many requests)$/i.test(line))
    || errorHeading.test(text.trim());
  if (confirmedError && !hasVisibleProducts) {
    return 'HTTP_BLOCK';
  }
  return null;
}

export function didTryAgainProgress(before = {}, after = {}, retryStillVisible = false) {
  return !retryStillVisible
    || Number(after.visibleGoodsCount) > Number(before.visibleGoodsCount)
    || Number(after.scrollHeight) !== Number(before.scrollHeight)
    || String(after.documentURI || after.url) !== String(before.documentURI || before.url);
}
