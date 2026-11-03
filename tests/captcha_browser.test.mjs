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

for(const status of ['NOT_CONFIGURED','ZERO_BALANCE','TIMEOUT','SERVICE_FAILED','SUBMIT_UNKNOWN']) {
  test(`${status} retains scene without applying`,async()=>{
    const f=fixture({status});assert.equal(await f.handler(),false);assert.equal(f.applied(),false);
    assert.ok(!f.calls.some(r=>r.action==='claim'));
  });
}

test('unsupported rotation and unrelated slider puzzles never call a paid service',async()=>{
  const handler=createCaptchaHandler({adapter:{inspect:async()=>({kind:'unsupported'})},request:async()=>{throw Error('must not pay');}});
  assert.equal(await handler(),false);
});

test('extractable unknown puzzle tries API first and then preserves it for manual action',async()=>{
  const calls=[],events=[];
  const handler=createCaptchaHandler({scope:'temu/test_only',
    adapter:{inspect:async()=>({kind:'temu_auto'}),images:async()=>['board','reference'],apply:async()=>{throw Error('unknown controls');}},
    request:async r=>{calls.push(r);return r.action==='manual'?{status:'MANUAL_REQUIRED'}:{status:'ANSWER_READY',answer:'[{"x":10,"y":20}]'};},
    onStatus:async s=>events.push(s)});
  assert.equal(await handler(),false);
  assert.equal(calls.length,2);assert.deepEqual(calls[0].images,['board','reference']);
  assert.equal(calls[1].action,'manual');assert.equal(events.at(-1).status,'MANUAL_REQUIRED');
});

test('images finish loading before API submission; stopping that wait makes no request',async()=>{
  let loaded=false,t=0,submissions=0;const events=[];
  const handler=createCaptchaHandler({scope:'temu/test_only',
    adapter:{inspect:async()=>({kind:loaded?'temu_auto':'loading'}),images:async()=>['complete panel','reference']},
    request:async()=>{submissions++;assert.equal(loaded,true);return {status:'SERVICE_FAILED'};},
    onStatus:async r=>events.push(r.status),now:()=>t,sleep:async ms=>{t+=ms;loaded=true;}});
  assert.equal(await handler(),false);assert.equal(submissions,1);assert.deepEqual(events,['PAGE_LOADING','SERVICE_FAILED']);
  const stopped=[];
  const stopHandler=createCaptchaHandler({adapter:{inspect:async()=>({kind:'loading'})},shouldStop:async()=>true,
    request:async()=>{throw Error('must not submit after stop');},onStatus:async r=>stopped.push(r.status)});
  assert.equal(await stopHandler(),false);assert.deepEqual(stopped,['STOPPED']);
});

for(const kind of ['temu_icon','temu_line']) {
  test(`${kind}: map original-image top-left coordinates to the scaled viewport`,async()=>{
    const calls=[];
    const wd=async(method,route,body)=>{
      if(route==='/element')return {'element-6066-11e4-a52e-4f735466cecf':body.value};
      if(route.endsWith('/rect'))return route.includes('background')?{x:100,y:50,width:372.5,height:374.5}:{x:10,y:20,width:83.5,height:83.5};
      calls.push({route,body});
    };
    const challenge={kind,images:[{selector:'background',width:745,height:749},
      ...[1,2,3].map(i=>({selector:'part'+i,width:167,height:167}))]};
    await webdriverCaptchaAdapter(wd,()=>{}).apply(challenge,'[{"x":600,"y":500},{"x":100,"y":200},{"x":20,"y":30}]');
    assert.equal(calls.length,3);
    const actions=calls[0].body.actions[0].actions;
    const target=kind==='temu_icon'?actions[0]:actions.find(a=>a.type==='pointerMove'&&a.duration===700);
    assert.equal(target.x,442);assert.equal(target.y,342);
    assert.ok(actions.some(a=>a.type==='pointerDown')&&actions.some(a=>a.type==='pointerUp'));
  });
}

test('Temu coordinates reject incomplete/out-of-image answers before dragging',()=>{
  assert.deepEqual(parseTemuCoordinates('x=10,y=20|x=30,y=40|x=50,y=60',{width:100,height:100}),[{x:10,y:20},{x:30,y:40},{x:50,y:60}]);
  assert.throws(()=>parseTemuCoordinates('[{"x":200,"y":2}]',{width:100,height:100}));
});

test('WebDriver executes normal text through the existing session client',async()=>{
  const calls=[];const wd=async(method,suffix,body)=>{calls.push({method,suffix,body});return {'element-6066-11e4-a52e-4f735466cecf':body?.value==='input'?'input-id':'submit-id'};};
  await webdriverCaptchaAdapter(wd,()=>{}).apply({kind:'normal',input:'input',submit:'submit'},'ABC123');
  assert.deepEqual(calls.map(c=>c.suffix),['/element','/element/input-id/clear','/element/input-id/value','/element','/element/submit-id/click']);
});
