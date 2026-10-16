// Inspect visible controls, not just the form endpoint: Amazon also uses
// validateCaptcha for its one-button "Continue shopping" interstitial.
export async function inspectAmazonPage(page) {
  return page.evaluate(() => {
    const text=(document.body?.innerText || '').replace(/\s+/g,' ').slice(0,1200);
    const captcha=Boolean(document.querySelector('#captchacharacters, input[name="cvf_captcha_input"], iframe[src*="recaptcha"], iframe[src*="hcaptcha"]'))
      || /robot check|enter the characters you see|type the characters you see/i.test(text);
    const buttons=[...document.querySelectorAll('button')].filter(button=>
      button.getClientRects().length && /^continue shopping$/i.test(button.textContent.trim()));
    const button=buttons.length===1 ? buttons[0] : null;
    const form=button?.form;
    let sameOrigin=false;
    try {sameOrigin=new URL(form?.action || '').origin===location.origin;} catch {}
    const continueShopping=!captcha && Boolean(form && sameOrigin)
      && /^Click the button below to continue shopping\b/i.test(text)
      && ![...form.elements].some(element=> !['hidden','submit','button'].includes(element.type));
    return {url:location.href,page_title:document.title,text,captcha,
      continue_shopping:continueShopping,has_product_title:Boolean(document.querySelector('#productTitle')),
      has_rank_badges:Boolean(document.querySelector('.zg-bdg-ctr'))};
  });
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
