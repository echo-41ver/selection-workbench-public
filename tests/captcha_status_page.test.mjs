import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';

const html=fs.readFileSync(new URL('../web/index.html',import.meta.url),'utf8');
const code=html.slice(html.indexOf('function statusTone('),html.indexOf('function runtimeUnion('))+'\n'+html.split('\n').find(l=>l.startsWith('function first('));
for(const state of ['PAGE_LOADING','PENDING','ANSWER_READY','VERIFIED','SERVICE_FAILED','TIMEOUT','REJECTED','MANUAL_REQUIRED','SUBMIT_UNKNOWN']){
 test(`webpage: ${state} has the correct human-action indicator`,()=>{
  const manual=!['PAGE_LOADING','PENDING','ANSWER_READY','VERIFIED'].includes(state);
  const context=vm.createContext({row:{采集状态:'WAITING_HUMAN_VERIFICATION',验证码状态:state,状态:'当前验证码状态',阻断:true,需人工介入:manual}});
  vm.runInContext(code,context);
  assert.equal(vm.runInContext('needsHuman(row)',context),manual);
  assert.equal(vm.runInContext('statusTone(row)',context),manual?'danger':'success');
 });
}
