// Product copy may contain verification/security words. Require visible controls.
export function inspectHumanVerification(){
 const visible=e=>{if(!e?.getBoundingClientRect)return false;const r=e.getBoundingClientRect(),s=getComputedStyle(e);return r.width>0&&r.height>0&&s.display!=='none'&&s.visibility!=='hidden'&&s.opacity!=='0';};
 const instruction=/verify (?:that )?you are human|complete the verification|security verification|slide to complete|安全验证|安全驗證|人机验证|人機驗證|请完成验证|請完成驗證|拖动滑块|拖動滑塊|验证码|驗證碼/i;
 const frames=[...document.querySelectorAll('iframe[src*="captcha"],iframe[src*="verification"],iframe[title*="captcha" i]')];
 if(frames.some(visible))return true;
 return [...document.querySelectorAll('[role="dialog"],[aria-modal="true"],[id*="captcha" i],[class*="captcha" i],[id*="verify" i],[class*="verify" i]')]
  .some(e=>visible(e)&&instruction.test(e.innerText||'')&&e.querySelector('input,button,[role="slider"],canvas'));
}
