// Real local Chromium pages; WebDriver transport and paid API are simulated.
import test from 'node:test';
import assert from 'node:assert/strict';
import {chromium} from 'playwright';
import {inspectCaptcha,webdriverCaptchaAdapter,createCaptchaHandler} from '../scripts/captcha_browser.mjs';

test('local text challenge: cropped image, existing session commands, website release and original checkpoint',async()=>{
  const browser=await chromium.launch({headless:true});
  try {
    const page=await browser.newPage();
    await page.setContent(`<form onsubmit="event.preventDefault();if(document.querySelector('input').value==='ABC123')document.body.innerHTML='<div id=items>retained item</div>'">
      <input id=captchacharacters><img width=160 height=50><button type=submit>验证</button></form>`);
    await page.evaluate(()=>{const canvas=document.createElement('canvas');canvas.width=160;canvas.height=50;const c=canvas.getContext('2d');c.fillStyle='white';c.fillRect(0,0,160,50);c.fillStyle='black';c.fillText('fixture',20,30);document.querySelector('img').src=canvas.toDataURL();});
    await page.locator('img').evaluate(image=>image.decode());
    const checkpoint={session:'test-only-existing',goods:['retained-1'],round:37,clicks:12};
    const original=JSON.stringify(checkpoint),calls=[];
    const wd=async(method,route,body)=>{
      calls.push(route);
      if(route==='/element')return {'element-6066-11e4-a52e-4f735466cecf':body.value};
      const [,id,operation]=route.match(/^\/element\/(.*)\/(screenshot|clear|value|click)$/);
      const element=page.locator(id);
      if(operation==='screenshot')return (await element.screenshot({type:'png'})).toString('base64');
      if(operation==='clear')return element.fill('');
      if(operation==='value')return element.fill(body.text);
      return element.click();
    };
    const adapter=webdriverCaptchaAdapter(wd,()=>page.evaluate(inspectCaptcha));
    let submissions=0,t=0;
    const handler=createCaptchaHandler({scope:'temu/test_only',browserIdentity:checkpoint.session,adapter,
      request:async r=>{if(r.action==='claim')return {status:'APPLYING'};
        if(r.action==='verify'){assert.equal(r.passed,true);return {status:'VERIFIED'};}
        submissions++;assert.equal(r.images.length,1);return {status:'ANSWER_READY',answer:'ABC123'};},
      isReleased:async()=>await page.locator('#items').count()===1,sleep:async ms=>{t+=ms;},now:()=>t});
    assert.equal(await handler(),true);assert.equal(submissions,1);
    assert.equal(JSON.stringify(checkpoint),original);
    assert.ok(calls.includes('/element/[data-swb-captcha="input"]/value'));
  } finally {await browser.close();}
});

test('local unsupported slider is recognized and never submitted as a Temu piece task',async()=>{
  const browser=await chromium.launch({headless:true});
  try {
    const page=await browser.newPage();
    await page.setContent('<div class="captcha"><p>拖动滑块完成验证</p><input type="range"></div>');
    assert.equal((await page.evaluate(inspectCaptcha)).kind,'unsupported');
    const handler=createCaptchaHandler({adapter:{inspect:()=>page.evaluate(inspectCaptcha)},request:async()=>{throw Error('must not submit');}});
    assert.equal(await handler(),false);
    await page.setContent('<form><input type="password"><button>登录</button></form>');
    assert.equal(await page.evaluate(inspectCaptcha),null);
    await page.setContent('<h1>HTTP 403 Access Denied</h1>');
    assert.equal(await page.evaluate(inspectCaptcha),null);
  } finally {await browser.close();}
});
