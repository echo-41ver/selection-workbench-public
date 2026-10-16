import test from 'node:test';import assert from 'node:assert/strict';
import {readInitialWindowHandles} from '../scripts/browser_control_bootstrap.mjs';
test('initial connection failure preserves evidence and requests bounded local binding repair',async()=>{
 const error=Object.assign(Error('offline'),{kind:'CONNECTION_FAILED'}),writes=[],events=[];
 await assert.rejects(readInitialWindowHandles(async(method,path)=>{assert.equal(method,'GET');assert.equal(path,'/window/handles');throw error;},
  {readCheckpoint:async()=>({status:'WAITING_HUMAN_VERIFICATION',unique_goods:40,round:3,sales_sort:{state:'UI_CONFIRMED'},manual_action_required:true,confirmed_login_redirect:true,http_status:429}),
   saveCheckpoint:async row=>writes.push(row),appendEvent:async(type,data)=>events.push({type,...data}),now:()=> 'now'}),/offline/);
 assert.equal(writes.length,1);const row=writes[0];assert.equal(row.unique_goods,40);assert.equal(row.round,3);assert.equal(row.sales_sort.state,'UI_CONFIRMED');
 assert.equal(row.status,'STOPPED_BROWSER_CONTROL_FAILURE');assert.equal(row.automatic_retry,true);assert.equal(row.command_execution_unknown,false);assert.equal(row.resume_requires_session_rotation,false);assert.equal(row.http_status,null);assert.equal(row.confirmed_login_redirect,false);assert.equal(events.length,1);
});
test('successful startup and unknown-execution timeout are not rewritten as local connection failures',async()=>{
 const opts={readCheckpoint:()=>assert.fail('must not rewrite'),saveCheckpoint:()=>assert.fail('must not rewrite')};
 assert.deepEqual(await readInitialWindowHandles(async()=>['current'],opts),['current']);
 await assert.rejects(readInitialWindowHandles(async()=>{throw Object.assign(Error('uncertain'),{kind:'TIMEOUT'});},opts),/uncertain/);
});
