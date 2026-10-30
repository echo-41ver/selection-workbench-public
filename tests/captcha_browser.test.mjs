// Local fixtures and simulated API answers only. No live-site success claimed.
import test from 'node:test';
import assert from 'node:assert/strict';
import {createCaptchaHandler,parseTemuCoordinates,webdriverCaptchaAdapter} from '../scripts/captcha_browser.mjs';

function fixture({stop=false,changed=false,released=true,status='ANSWER_READY'}={}) {
  const calls=[],events=[];let applied=false,t=0,reads=0,serviceCalls=0;
  const challenge={kind:'normal',images:[{width:120,height:40}],input:'input',submit:'button'};
  const handler=createCaptchaHandler({root:'unused',scope:'temu/test_only',browserIdentity:'same-session',
    adapter:{inspect:async()=>{reads++;return applied&&released?null:challenge;},
      images:async()=>[changed&&reads>1?'replaced-question':'same-question'],
      apply:async(c,answer)=>{assert.equal(answer,'ABC123');applied=true;}},
    request:async r=>{calls.push(r);if(r.action==='claim')return {status:'APPLYING'};
      if(r.action==='verify')return {status:r.passed?'VERIFIED':'REJECTED'};
      serviceCalls++;return serviceCalls===1?{status:'PENDING'}:{status,answer:'ABC123'};},
    onStatus:async s=>events.push(s),shouldStop:async()=>stop&&serviceCalls>0,
    isReleased:async()=>applied&&released,now:()=>t,sleep:async ms=>{t+=ms;}});
  return {handler,calls,events,applied:()=>applied};
}

test('same session/challenge polls, applies and checks website release',async()=>{
    const f=fixture();assert.equal(await f.handler(),true);assert.equal(f.applied(),true);
    assert.equal(f.calls.filter(r=>r.action==='claim').length,1);
    assert.equal(f.calls.at(-1).passed,true);
    assert.equal(f.calls[0].fingerprint,f.calls[1].fingerprint);
    assert.ok(f.events.every(e=>e.session_preserved&&!e.resume_requires_session_rotation));
    assert.ok(!JSON.stringify(f.events).includes('ABC123'));
});

test('an API answer alone is insufficient: no page release is rejected',async()=>{
  const f=fixture({released:false});assert.equal(await f.handler(),false);
  assert.equal(f.calls.at(-1).passed,false);assert.equal(f.events.at(-1).status,'REJECTED');
});

test('stop preserves the paid task and prevents answer execution',async()=>{
  const f=fixture({stop:true});assert.equal(await f.handler(),false);assert.equal(f.applied(),false);
  assert.equal(f.calls.length,1);assert.equal(f.events.at(-1).status,'STOPPED');
});

test('a changed image never receives the old answer',async()=>{
  const f=fixture({changed:true});assert.equal(await f.handler(),false);assert.equal(f.applied(),false);
  assert.ok(!f.calls.some(r=>r.action==='claim'));
});

for(const status of ['NOT_CONFIGURED','ZERO_BALANCE','TIMEOUT','SERVICE_FAILED','SUBMIT_UNKNOWN','BUDGET_LIMIT']) {
  test(`${status} retains scene without applying`,async()=>{
    const f=fixture({status});assert.equal(await f.handler(),false);assert.equal(f.applied(),false);
    assert.ok(!f.calls.some(r=>r.action==='claim'));
  });
}

test('unsupported rotation/slider/line puzzles never call a paid service',async()=>{
  const handler=createCaptchaHandler({adapter:{inspect:async()=>({kind:'unsupported'})},request:async()=>{throw Error('must not pay');}});
  assert.equal(await handler(),false);
});

test('Temu coordinates reject incomplete/out-of-image answers before dragging',()=>{
  assert.deepEqual(parseTemuCoordinates('x=10,y=20|x=30,y=40|x=50,y=60',{width:100,height:100}),[{x:10,y:20},{x:30,y:40},{x:50,y:60}]);
  assert.throws(()=>parseTemuCoordinates('[{"x":200,"y":2}]',{width:100,height:100}));
});

test('WebDriver executes normal text through the existing session client',async()=>{
  const calls=[];const wd=async(method,suffix,body)=>{calls.push({method,suffix,body});return {'element-6066-11e4-a52e-4f735466cecf':body?.value==='input'?'input-id':'submit-id'};};
  await webdriverCaptchaAdapter(wd,()=>{}).apply({kind:'normal',input:'input',submit:'submit'},'ABC123');
  assert.deepEqual(calls.map(c=>c.suffix),['/element','/element/input-id/clear','/element/input-id/value','/element','/element/submit-id/click']);
});
