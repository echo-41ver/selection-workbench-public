import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';

const html=fs.readFileSync(new URL('../web/index.html',import.meta.url),'utf8');
const feedbackCode=html.slice(html.indexOf('function feedback('),html.indexOf("$('refresh-market').addEventListener"));
const actionCode=html.slice(html.indexOf('async function action('),html.indexOf("$('retry-generation').addEventListener"));
const queued='继续请求已提交，正在启动后台程序；尚未确认运行';

function fixture(){
 const nodes=new Map(['feedback','global-feedback'].map(id=>[id,{textContent:'',className:''}]));
 const context=vm.createContext({$:id=>nodes.get(id),busy:false,statusFresh:true,lastStatus:null,
  AbortController,updateControls(){},setTimeout(){return 1;},clearTimeout(){},showPage(){},
  poll(){},marketRefreshView:null,beginMarketRefresh(){},renderMarketStatus(){},marketRefreshInProgress(){return false;},
  fetch:async()=>{throw Error('unexpected request');}});
 vm.runInContext(feedbackCode+'\n'+actionCode,context);
 const remember=(id='launch-1',key='task-1')=>{
  context.reply={launch_pending:true,launch_id:id,category_key:key,phase:queued};
  vm.runInContext('feedback(reply.phase);rememberResumeFeedback(reply)',context);
 };
 const render=(state,extra={})=>{
  context.state={category_key:'task-1',resume_launch:{id:'launch-1',state,...extra}};
  vm.runInContext('renderResumeLaunchFeedback(state)',context);
 };
 return {context,nodes,remember,render};
}

test('a queued Resume response resolves from its observed running launch without another request',async()=>{
 const {context,nodes,render}=fixture();let calls=0;
 context.fetch=async endpoint=>{
  calls++;assert.equal(endpoint,'/api/resume');
  return {ok:true,json:async()=>({ok:true,launch_pending:true,launch_id:'launch-1',category_key:'task-1',phase:queued})};
 };
 await vm.runInContext("action('/api/resume',{})",context);
 assert.equal(calls,1);assert.equal(context.busy,false);
 for(const node of nodes.values())assert.equal(node.textContent,queued);
 render('launching');for(const node of nodes.values())assert.equal(node.textContent,queued);
 render('running');
 for(const node of nodes.values()){
  assert.match(node.textContent,/已确认原任务监督进程启动/);
  assert.doesNotMatch(node.textContent,/尚未确认运行/);
  assert.equal(node.className,'notice success');
 }
 assert.equal(calls,1);
 assert.match(html,/function render\(data\)\{if\(typeof renderResumeLaunchFeedback==='function'\)renderResumeLaunchFeedback\(data\)/);
});

test('the pending banner accepts only the same task and launch receipt',()=>{
 const {context,nodes,remember,render}=fixture();remember();
 render('running',{id:'old-launch'});
 for(const node of nodes.values())assert.equal(node.textContent,queued);
 context.state={category_key:'another-task',resume_launch:{id:'launch-1',state:'running'}};
 vm.runInContext('renderResumeLaunchFeedback(state)',context);
 for(const node of nodes.values())assert.equal(node.textContent,queued);
 render('running');for(const node of nodes.values())assert.match(node.textContent,/已确认/);
});

test('launch failure and cancellation replace pending feedback with actual terminal evidence',()=>{
 for(const [state,message,error] of [['failed','监督进程未登记，数据和断点已保留',true],['cancelled','继续启动已取消，数据和断点已保留',false]]){
  const {nodes,remember,render}=fixture();remember();render(state,{message});
  for(const node of nodes.values()){
   assert.equal(node.textContent,message);assert.equal(node.className,'notice '+(error?'danger':'success'));
  }
 }
});

test('a completed launch cannot overwrite a newer Stop or unrelated operation notice',()=>{
 const {context,nodes,remember,render}=fixture();remember();
 vm.runInContext("feedback('已请求停止')",context);render('running');
 for(const node of nodes.values())assert.equal(node.textContent,'已请求停止');
 remember('launch-2');render('running');
 for(const node of nodes.values())assert.equal(node.textContent,queued);
 render('failed',{id:'launch-2',message:'本次启动失败'});
 for(const node of nodes.values())assert.equal(node.textContent,'本次启动失败');
});

test('only still-pending feedback nodes are updated and terminal feedback is applied once',()=>{
 const {nodes,remember,render}=fixture();remember();
 nodes.get('global-feedback').textContent='另一个操作已完成';
 render('running');
 assert.match(nodes.get('feedback').textContent,/已确认/);
 assert.equal(nodes.get('global-feedback').textContent,'另一个操作已完成');
 render('failed',{message:'旧请求被后续记录覆盖'});
 assert.match(nodes.get('feedback').textContent,/已确认/);
});

test('automatic market authorization polling preserves the latest task-operation notice',async()=>{
 const {context,nodes}=fixture();let calls=0;
 context.fetch=async()=>{calls++;return {ok:true,json:async()=>({ok:true,message:'正在自动读取市场数据并更新网页与Excel'})};};
 vm.runInContext("feedback('已请求停止，历史数据和断点保留')",context);
 await vm.runInContext("action('/api/refresh-market',{},false)",context);
 assert.equal(calls,1);
 for(const node of nodes.values())assert.equal(node.textContent,'已请求停止，历史数据和断点保留');
 await vm.runInContext("action('/api/refresh-market',{})",context);
 assert.equal(calls,2);
 for(const node of nodes.values())assert.match(node.textContent,/正在自动读取市场数据/);
});

test('automatic market authorization polling still reports an actual failed request',async()=>{
 const {context,nodes}=fixture();
 context.fetch=async()=>({ok:false,json:async()=>({error:{message:'极鲸云连接失败，请检查实际网络'}})});
 await vm.runInContext("action('/api/refresh-market',{},false)",context);
 for(const node of nodes.values()){
  assert.equal(node.textContent,'极鲸云连接失败，请检查实际网络');
  assert.equal(node.className,'notice danger');
 }
});
