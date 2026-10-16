import test from 'node:test';
import assert from 'node:assert/strict';
import {classifyPageState} from '../scripts/temu_flower_recovery_policy.mjs';
import {inspectSearchEnd} from '../scripts/collection_checkpoint.mjs';
test('Chinese access, login and verification prompts retain their distinct states',()=>{
 for(const text of ['安全验证','安全驗證','人机验证','人機驗證','请完成验证','請完成驗證'])
  assert.equal(classifyPageState({url:'https://www.temu.com/search_result.html',text}),'HUMAN_VERIFICATION');
 for(const text of ['登录','登入','注册','註冊'])
  assert.equal(classifyPageState({url:'https://www.temu.com/login.html',text}),'LOGIN_REQUIRED');
 for(const text of ['拒绝访问','拒絕存取','请求已被阻止','請求已遭封鎖','异常流量','異常流量'])
  assert.equal(classifyPageState({text}),'ACCESS_DENIED');
 for(const text of ['代理服务器拒绝连接','代理伺服器拒絕連線','无法连接到服务器','连接超时','連線逾時'])
  assert.equal(classifyPageState({text}),'PROXY_OR_NETWORK_FAILURE');
});
test('Chinese expansion, retry and end markers are never confused',()=>{
 const previousDocument=globalThis.document,previousStyle=globalThis.getComputedStyle;
 globalThis.getComputedStyle=()=>({display:'block',visibility:'visible'});
 try{
  for(const [flag,labels] of [['seeMorePresent',['查看更多','顯示更多','加载更多','載入更多']],
   ['tryAgainVisible',['重试','重試','再试一次','再試一次','重新加载']],
   ['noMoreResultsVisible',['没有更多结果','沒有更多結果']]]){
   for(const text of labels){
    globalThis.document={querySelectorAll:()=>[{textContent:text,getBoundingClientRect:()=>({width:100,height:20})}]};
    assert.equal(inspectSearchEnd()[flag],true,text);
   }
  }
 }finally{globalThis.document=previousDocument;globalThis.getComputedStyle=previousStyle;}
});
