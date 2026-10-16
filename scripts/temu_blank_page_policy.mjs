export function blankVerificationDecision(state, previous, at) {
  let verification = false;
  try { verification = /(?:bgn_verification|verification|captcha|verifyCode)/i.test(new URL(state.url).pathname); } catch {}
  const blank = verification && state.bodyEmpty === true && state.visibleControls === 0
    && !String(state.text || '').replace(/[\u200b-\u200f\ufeff\s]/g, '') && !state.transientStateUnavailable;
  if (!blank) return {since:null, attempted:previous?.attempted || false, refresh:false};
  const since = previous?.since ?? at;
  const refresh = !previous?.attempted && at - since >= 30000;
  return {since, attempted:Boolean(previous?.attempted || refresh), refresh};
}
