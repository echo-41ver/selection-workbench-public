import test from 'node:test';
import assert from 'node:assert/strict';
import {openAmazonDetail,recoverableAmazonBrowser} from '../scripts/amazon_browser_recovery.mjs';
const url='https://www.amazon.com/dp/B00ET5VMTU';
function fixture({timeout=false,lateGate=false,http=200}={}) {
  const calls=[];let waits=0;
  const page={url:()=>url,
    goto:async()=>{calls.push('goto');return {status:()=>http};},
    reload:async()=>{calls.push('reload');return {status:()=>200};},
    locator:()=>({first:()=>({waitFor:async()=>{waits++;if(waits===1&&timeout)throw new Error('Timeout 15000ms exceeded');}})})};
  return {page,calls,options:{inspect:async()=>({http_status:waits?200:http,captcha:lateGate&&waits>0}),sleep:async()=>{}}};
}
test('ordinary title timeout refreshes once and obtains the product',async()=>{
  const f=fixture({timeout:true});const result=await openAmazonDetail(f.page,url,f.options);
  assert.deepEqual(f.calls,['goto','reload']);assert.equal(result.page_loads,2);
});
test('HTTP 503 is retried with refresh, but explicit 403 is not',async()=>{
  const f=fixture({http:503});await openAmazonDetail(f.page,url,f.options);assert.deepEqual(f.calls,['goto','reload']);
  const blocked=fixture({http:403});await assert.rejects(openAmazonDetail(blocked.page,url,blocked.options),/403/);
  assert.deepEqual(blocked.calls,['goto']);
});
test('verification appearing during a title wait prevents refresh',async()=>{
  const f=fixture({timeout:true,lateGate:true});await assert.rejects(openAmazonDetail(f.page,url,f.options),/验证/);
  assert.deepEqual(f.calls,['goto']);
});
test('transport timeout before target loads reopens the intended URL only once',async()=>{
  let loads=0;const f=fixture();f.page.url=()=> 'about:blank';
  f.page.goto=async requested=>{assert.equal(requested,url);loads++;throw new Error('net::ERR_CONNECTION_TIMED_OUT');};
  await assert.rejects(openAmazonDetail(f.page,url,f.options),/ERR_CONNECTION/);assert.equal(loads,2);
});
test('browser crash recovery shares a single replacement across parallel workers and is bounded',async()=>{
  const instances=[];
  const manager=recoverableAmazonBrowser(async()=>{
    const b={connected:true,isConnected(){return this.connected;},async newContext(){await new Promise(r=>setTimeout(r,5));return {id:instances.length};},async close(){this.connected=false;}};
    instances.push(b);return b;
  },{},2);
  const first=await manager.getContext();assert.equal(await manager.getContext(),first);
  instances[0].connected=false;
  const [a,b,c]=await Promise.all([manager.getContext(),manager.getContext(),manager.getContext()]);
  assert.equal(a,b);assert.equal(b,c);assert.notEqual(a,first);assert.equal(instances.length,2);
  instances[1].connected=false;await assert.rejects(manager.getContext(),/保存断点/);await manager.close();
});
