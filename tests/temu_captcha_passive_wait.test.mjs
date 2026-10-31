import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';

const source=fs.readFileSync(new URL('../scripts/temu_flower_collect.mjs',import.meta.url),'utf8');
const execute=source.slice(source.indexOf('async function execute('),source.indexOf('async function atomic('));
const challenge='https://www.temu.com/bgn_verification.html?fixture=1';
const search='https://www.temu.com/search_result.html?search_key=fixture';
function setup(overrides={}) {
 const calls=[],sleeps=[];
 const context=vm.createContext({humanVerificationActive:true,bidiStatus:'ACTIVE',currentDocumentUrl:challenge,
  sleep:async ms=>sleeps.push(ms),wd:async(...args)=>{calls.push(args);return {url:search};},...overrides});
 vm.runInContext(execute,context);
 return {context,calls,sleeps,run:()=>vm.runInContext('execute("return location.href", [], "fixture_probe")',context)};
}

test('a confirmed standalone challenge sends no DOM command before its navigation changes',async()=>{
 const view=setup();let navigationObserved=false;
 view.context.sleep=async()=>{assert.equal(view.calls.length,0);view.context.currentDocumentUrl=search;navigationObserved=true;};
 await view.run();assert.ok(navigationObserved);assert.equal(view.calls.length,1);
 assert.deepEqual(Array.from(view.calls[0].slice(0,2)),['POST','/execute/sync']);
});

test('changing only the challenge query does not release the passive wait',async()=>{
 const view=setup();let samples=0;
 view.context.sleep=async()=>{assert.equal(view.calls.length,0);samples++;
  view.context.currentDocumentUrl=samples===1?challenge.replace('fixture=1','fixture=2'):search;};
 await view.run();assert.ok(samples>=2);assert.equal(view.calls.length,1);
});

test('an embedded same-URL challenge retains a slow probe without declaring success',async()=>{
 const view=setup({currentDocumentUrl:search});
 view.context.wd=async(...args)=>{view.calls.push(args);return {url:search,verificationVisible:true};};
 const state=await view.run();assert.ok(view.sleeps.some(ms=>ms>=5000));
 assert.equal(view.calls.length,1);assert.equal(state.verificationVisible,true);
 assert.equal(view.context.humanVerificationActive,true);
});

test('an unconfirmed challenge URL keeps the ordinary first DOM inspection',async()=>{
 const view=setup({humanVerificationActive:false});await view.run();
 assert.equal(view.calls.length,1);assert.equal(view.sleeps.length,0);
});

test('a disconnected BiDi channel falls back to a slow existing DOM probe',async()=>{
 const view=setup({bidiStatus:'CLOSED'});await view.run();
 assert.ok(view.sleeps.some(ms=>ms>=5000));assert.equal(view.calls.length,1);
});

test('BiDi closing during the passive wait cannot trap the collector in stale navigation',async()=>{
 const view=setup();let closed=false;
 view.context.sleep=async()=>{assert.equal(view.calls.length,0);view.context.bidiStatus='CLOSED';closed=true;};
 await view.run();assert.ok(closed);assert.equal(view.calls.length,1);
});

test('the resumed probe preserves the original browser-control timeout failure',async()=>{
 const failure=new Error('fixture browser-control timeout');
 const view=setup({currentDocumentUrl:search,wd:async()=>{throw failure;}});
 await assert.rejects(view.run(),error=>error===failure);
});
