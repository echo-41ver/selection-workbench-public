import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';

test('returning to history resumes the selected result after its hidden retry was dropped',async()=>{
 const html=fs.readFileSync(new URL('../web/index.html',import.meta.url),'utf8');
 const element=()=>({hidden:false,children:[],style:{},textContent:'',
  replaceChildren(...items){this.children=items;this.textContent='';},
  append(...items){this.children.push(...items);},insertBefore(item){this.children.unshift(item);},
  scrollIntoView(){},setAttribute(){},addEventListener(){}});
 const nodes=new Map(),get=id=>{if(!nodes.has(id))nodes.set(id,element());return nodes.get(id);};
 const timers=new Map();let nextTimer=0,detailCalls=0;
 const context=vm.createContext({$:get,AbortController,encodeURIComponent,Date,JSON,
  document:{createElement:element,querySelectorAll:()=>[]},location:{hash:'#history'},
  history:{replaceState(_state,_title,hash){context.location.hash=hash;}},placeDraftEditor(){},
  renderFrontendMarket(){},renderBusiness(){},safe:value=>value,
  setTimeout(callback,delay){timers.set(++nextTimer,{callback,delay});return nextTimer;},
  clearTimeout(id){timers.delete(id);},
  fetch:async path=>({ok:true,json:async()=>path==='/api/history'?{ok:true,runs:[]}:
   ++detailCalls===1?{ok:true,history_loading:true}:
    {ok:true,scope:{category:'保存的品类'},phase:'已保存',excel_files:{}}})});
 const source=html.slice(html.indexOf('function showPage(page)'),html.indexOf('async function loadVisualReview('));
 vm.runInContext(source,context);
 await context.openHistory('saved_task');
 assert.match(get('history-detail').textContent,/正在后台读取历史结果/);
 context.showPage('monitor');
 const retry=[...timers.values()].find(timer=>timer.delay===2000);retry.callback();
 assert.equal(detailCalls,1,'hidden history must not keep polling');
 context.showPage('history');
 await new Promise(setImmediate);
 assert.equal(detailCalls,2,'returning must resume without another View Results click');
 assert.equal(get('history-detail').children[0].textContent,'保存的品类 · 历史结果');
 assert.equal(get('history-detail').textContent,'');
});
