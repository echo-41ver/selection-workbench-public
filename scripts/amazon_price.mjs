export function amazonUsdPrice(text) {
  // Amazon.com may render CNY or another delivery-region currency. Never label
  // a bare numeric amount as USD or perform an unrequested exchange conversion.
  const match=String(text || '').trim().match(/^(?:US\s*\$|USD\s*|\$)\s*([\d,]+(?:\.\d{1,2})?)(?![\d.])/i);
  if(!match)return null;
  const amount=Number(match[1].replaceAll(',',''));
  return Number.isFinite(amount) && amount>0 ? amount : null;
}
