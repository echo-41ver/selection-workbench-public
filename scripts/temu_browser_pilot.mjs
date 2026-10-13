#!/usr/bin/env node
// Isolated acquisition experiment. Never touches live sessions or production rows.
import fs from 'node:fs/promises';
import path from 'node:path';
import vm from 'node:vm';
import {chromium} from 'playwright';
import {classifyPageState} from './temu_flower_recovery_policy.mjs';
import {inspectSalesSort,selectSalesSort} from './temu_sales_sort.mjs';
import {atomicSnapshot} from './snapshot_atomic.mjs';

const option=(name,fallback)=>{const i=process.argv.indexOf(name);return i<0?fallback:process.argv[i+1];};
const query=option('--query','remote control');
const out=path.resolve(option('--output',path.join('runtime','pilots',new Date().toISOString().replace(/[:.]/g,'-'))));
const minutes=Number(option('--minutes','20'));
const rounds=Number(option('--rounds','20'));
const remote=process.env.TEMU_PILOT_BROWSER_WS;
if(!Number.isFinite(minutes)||minutes<=0||!Number.isSafeInteger(rounds)||rounds<1)throw Error('Invalid pilot limits');
// Existing credentials are neither assumed nor inferred from proxy credentials.
const source=remote?'MANAGED_BROWSER_PILOT':'LOCAL_CHROME_SYSTEM_NETWORK_PILOT';
await fs.mkdir(out,{recursive:true});
const events=path.join(out,'events.jsonl');
const started=Date.now();
const goods=new Map();
let completedRounds=0,manualStarted=null,manualMs=0,lastAction=0;
const emit=async(type,details={})=>fs.appendFile(events,JSON.stringify({at:new Date().toISOString(),type,...details})+'\n');
const save=async(status,extra={})=>{
  await atomicSnapshot(path.join(out,'products.jsonl'),[...goods.values()].map(r=>JSON.stringify(r)).join('\n')+(goods.size?'\n':''));
  await atomicSnapshot(path.join(out,'progress.json'),JSON.stringify({source,query,status,unique_goods:goods.size,
    completed_rounds:completedRounds,elapsed_seconds:Math.round((Date.now()-started)/1000),
    manual_wait_seconds:Math.round((manualMs+(manualStarted?Date.now()-manualStarted:0))/1000),...extra},null,2));
};
const sleep=ms=>new Promise(resolve=>setTimeout(resolve,ms));
const code=await fs.readFile(new URL('./temu_flower_collect.mjs',import.meta.url),'utf8');
const literal=code.match(/const extractScript = (`[\s\S]*?`);/);
if(!literal)throw Error('Existing frontend extractor missing');
const extractor=vm.runInNewContext(literal[1]);
let browser,context;
if(remote){
  browser=await chromium.connectOverCDP(remote,{timeout:30000});
  context=await browser.newContext();
}else{
  const profile=path.join(out,'chrome-profile');
  await fs.mkdir(profile); // Refuse silently reusing a previously tested profile.
  context=await chromium.launchPersistentContext(profile,{channel:'chrome',headless:false});
}
const page=await context.newPage();
page.setDefaultTimeout(15000);
page.on('response',response=>{
  if([403,429].includes(response.status())){
    const u=new URL(response.url());
    void emit('HTTP_ERROR',{endpoint:u.origin+u.pathname,status:response.status()});
  }
});
let sortConfirmed=false;
async function state(){
  const value=await page.evaluate((script)=>new Function(script)({knownIds:[],repairIds:[],observedAt:new Date().toISOString()}),extractor);
  return {...value,block:classifyPageState(value)};
}
async function scan(value){
  if(value.block||!sortConfirmed)return;
  for(const row of value.rows||[])goods.set(row.goods_id,{...row,source,search_query:query,search_sort:{requested:'sales',state:'UI_CONFIRMED',query}});
  await save('RUNNING');
}
async function guard(){
  while(true){
    const value=await state();
    if(value.block==='HUMAN_VERIFICATION'){
      if(!manualStarted){manualStarted=Date.now();await emit('WAITING_CAPTCHA');}
      await save('WAITING_CAPTCHA',{browser_preserved:true});
      await sleep(2000);continue;
    }
    if(manualStarted){manualMs+=Date.now()-manualStarted;manualStarted=null;await emit('VERIFICATION_FINISHED');}
    if(value.block){await save('STOPPED_'+value.block,{browser_preserved:true});throw Error(value.block);}
    return value;
  }
}
try{
  await save('STARTING');
  await emit('PILOT_STARTED',{source,explicit_residential_proxy:false,resource_blocking:false});
  await page.goto('https://www.temu.com/',{waitUntil:'domcontentloaded',timeout:60000});
  await guard();
  const box=page.locator('input[type="search"],input[placeholder*="Search"],input[placeholder*="search"]').first();
  await box.waitFor({state:'visible',timeout:30000});
  await sleep(3000);
  await box.fill(query);
  await sleep(1000);
  await box.press('Enter');
  await emit('QUERY_SUBMITTED',{query});
  let ready=null;
  for(let i=0;i<90;i++){
    ready=await guard();
    if(ready.rows?.length&&new URL(ready.url).searchParams.get('search_key')?.toLowerCase()===query.toLowerCase())break;
    await sleep(2000);
  }
  if(!ready?.rows?.length)throw Error('SEARCH_RESULTS_TIMEOUT');
  const handles=new Map();let next=0;
  const sorting=await selectSalesSort({query,sort:'sales',maxChecks:60,sleep,guard,
    inspect:async()=>{
      const handle=await page.evaluateHandle(`(${inspectSalesSort.toString()})('sales')`);
      const result=await handle.jsonValue();
      for(const field of ['menu','option']){
        const element=(await handle.getProperty(field)).asElement();
        if(element){const id=++next;handles.set(id,element);result[field]=id;}else result[field]=null;
      }
      await handle.dispose();return result;
    },click:async id=>{await handles.get(id).click();},onRetry:details=>emit('SORT_RETRY',details)});
  if(sorting.state!=='UI_CONFIRMED')throw Error('SORT_UNCONFIRMED');
  sortConfirmed=true;await emit('SORT_CONFIRMED');await scan(await guard());
  while(completedRounds<rounds&&Date.now()-started-manualMs<minutes*60000){
    const button=page.getByText(/^See more$/i).first();
    for(let i=0;i<50&&!(await button.isVisible());i++){
      const value=await guard();await scan(value);
      const end=page.getByText(/^No more results[.!]?$/i).first();
      if(await end.isVisible()){
        await sleep(2000);await guard();
        if(await end.isVisible()){await emit('NO_MORE_RESULTS');await save('KEYWORD_COMPLETE');console.log(JSON.stringify({out,status:'KEYWORD_COMPLETE',goods:goods.size}));await context.close();process.exit(0);}
      }
      await page.evaluate(()=>window.scrollBy(0,Math.round(innerHeight*.7)));
      await sleep(1000);
    }
    if(!(await button.isVisible()))throw Error('SEE_MORE_UNAVAILABLE');
    await button.scrollIntoViewIfNeeded();
    await sleep(Math.max(1500,12000-(Date.now()-lastAction)));
    const before=await guard();const ids=new Set((before.rows||[]).map(row=>row.goods_id));
    await button.click();lastAction=Date.now();
    let expanded=false;
    for(let i=0;i<30;i++){
      await sleep(1500);const after=await guard();await scan(after);
      if((after.rows||[]).some(row=>!ids.has(row.goods_id))){expanded=true;break;}
    }
    if(!expanded)throw Error('EXPANSION_NOT_CONFIRMED');
    completedRounds++;await emit('ROUND_COMPLETE',{unique_goods:goods.size,round:completedRounds});await save('RUNNING');
  }
  await save('PILOT_LIMIT_REACHED');console.log(JSON.stringify({out,status:'PILOT_LIMIT_REACHED',goods:goods.size}));
  await context.close();if(browser)await browser.close();
}catch(error){
  // Keep the experiment window available; never touch production profiles.
  await emit('PILOT_STOPPED',{reason:String(error?.message||error).slice(0,100)});
  await save('PILOT_STOPPED',{browser_preserved:true});
  console.log(JSON.stringify({out,status:'PILOT_STOPPED',goods:goods.size}));
  await new Promise(()=>{});
}
