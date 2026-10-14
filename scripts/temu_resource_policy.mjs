// Never reject scripts/styles by CDN hostname: the search UI loads them lazily.
export function allowPageImages({documentUrl = '', preparing = false, humanVerification = false} = {}) {
  if (humanVerification) return true;
  try { return /(?:bgn_verification|verification|captcha|verifyCode)/i.test(new URL(documentUrl).pathname); }
  catch { return false; }
}

export function blockImageRequest({url = '', destination = '', accept = '', ...page} = {}) {
  destination = String(destination).toLowerCase();
  // A CDN hostname must never turn functional JavaScript into an image.
  if (['document', 'script', 'style', 'stylesheet', 'worker', 'sharedworker', 'serviceworker'].includes(destination)) return false;
  let host = '', pathname = '';
  try { const parsed = new URL(url); host = parsed.hostname; pathname = parsed.pathname; } catch {}
  // Product thumbnails are expendable even while the initial search is loading.
  // Verification pictures use different paths and retain the existing exemption.
  const imageHost = /(?:^|\.)[a-z-]*img(?:-\d+)?\.kwcdn\.com$/i.test(host);
  if (imageHost && /^\/product\//i.test(pathname)) return true;
  if (allowPageImages(page)) return false;
  if (/(?:captcha|verification|verifycode)/i.test(pathname)) return false;
  return destination === 'image'
    || (!destination && /^image\//i.test(String(accept).trim()))
    || /\.(?:avif|bmp|gif|ico|jpe?g|png|svg|webp)(?:[?#]|$)/i.test(String(url))
    || imageHost;
}

export function nonEssentialRequest(host, destination, saving, verification) {
  if (!saving || verification) return false;
  if (['document', 'script', 'style', 'stylesheet', 'worker', 'sharedworker', 'serviceworker'].includes(destination)) return false;
  return /^(?:pftka-us|thtka-us|adtk-us)\.temu\.com$/i.test(host);
}
