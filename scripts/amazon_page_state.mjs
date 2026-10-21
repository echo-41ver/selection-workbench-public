// Inspect visible controls, not just the form endpoint: Amazon also uses
// validateCaptcha for its one-button "Continue shopping" interstitial.
export async function inspectAmazonPage(page) {
  return page.evaluate(() => {
    const bodyText=(document.body?.innerText || '').replace(/\s+/g,' ');
    const text=bodyText.slice(0,1200);
    const captcha=Boolean(document.querySelector('#captchacharacters, input[name="cvf_captcha_input"], iframe[src*="recaptcha"], iframe[src*="hcaptcha"]'))
      || /robot check|enter the characters you see|type the characters you see/i.test(bodyText);
    const controlName=button=>String(button.getAttribute('aria-label') ||
      (button.getAttribute('aria-labelledby') || '').split(/\s+/).map(id=>document.getElementById(id)?.textContent || '').join(' ').trim() ||
      button.textContent || button.value || '').replace(/\s+/g,' ').trim();
    const buttons=[...document.querySelectorAll('button, input[type="submit"], input[type="button"]')].filter(button=>
      button.getClientRects().length && /^continue shopping$/i.test(controlName(button)));
    const button=buttons.length===1 ? buttons[0] : null;
    const form=button?.form;
    let sameOrigin=false;
    try {sameOrigin=new URL(form?.action || '').origin===location.origin;} catch {}
    const continuationCopy=/\bClick the button below to continue shopping\b|\bTo continue,? please click the [“"]?Continue shopping[”"]? button below\b/i.test(bodyText);
    const hasProductTitle=Boolean(document.querySelector('#productTitle'));
    const hasRankBadges=Boolean(document.querySelector('.zg-bdg-ctr'));
    const continuationRequired=!hasProductTitle && !hasRankBadges &&
      (continuationCopy || /^\/checkpoint\/continue(?:\/|$)/i.test(location.pathname));
    const continueShopping=!captcha && continuationRequired && Boolean(form && sameOrigin)
      && continuationCopy
      && ![...form.elements].some(element=> !['hidden','submit','button'].includes(element.type));
    return {url:location.origin+location.pathname,page_title:document.title,text,captcha,
      continue_shopping:continueShopping,continuation_required:continuationRequired,
      has_product_title:hasProductTitle,has_rank_badges:hasRankBadges};
  });
}

// Error evidence is persisted in checkpoints and may be displayed to the user.
// Navigation tokens belong only to the live browser, never to diagnostics.
export function sanitizeAmazonDiagnostic(value) {
  const clean=value=>String(value || '').replace(/\u001b\[[0-9;]*m/g,'').replace(/https?:\/\/[^\s"'<>]+/gi,raw=>{
    try {const url=new URL(raw);return url.origin+url.pathname;} catch {return '[URL]';}
  });
  if(typeof value==='string')return clean(value);
  if(!value || typeof value!=='object')return value;
  return Object.fromEntries(Object.entries(value).map(([key,item])=>[key,
    typeof item==='string' ? clean(item) : item]));
}

// Upgrade only the exact interstitial mistakenly marked as a CAPTCHA by older
// collectors. Preserve the cumulative request budget and every other gate.
export function recoverMisclassifiedContinuation(row) {
  const error=row.detail_error;
  if(error?.code!=='AMAZON_VERIFICATION_REQUIRED' || error.continue_shopping===false
    || !/^Click the button below to continue shopping\s+Continue shopping\s+(?:Conditions of Use|Privacy Policy)/i.test(error.text || '')) return row;
  return {...row,detail_error:{...error,code:'AMAZON_CONTINUE_SHOPPING_RECHECK',retryable:true,
    message:'重新检查旧版误判的继续购物入口'}};
}
