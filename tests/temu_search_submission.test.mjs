import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';
import {chromium} from 'playwright';
import {decideSearchSubmission} from '../scripts/temu_search_bootstrap_policy.mjs';
import {classifyPageState} from '../scripts/temu_flower_recovery_policy.mjs';

const executablePath = [process.env.PLAYWRIGHT_EXECUTABLE_PATH,
  'C:/Program Files/Google/Chrome/Application/chrome.exe',
  'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe', chromium.executablePath()].find(p=>p&&fs.existsSync(p));
const source=fs.readFileSync(new URL('../scripts/temu_flower_collect.mjs',import.meta.url),'utf8');
const start=source.indexOf('async function submitHomeSearch()');
const end=source.indexOf('\nasync function waitForStableEmptySearch',start);
const functionSource=source.slice(start,end);
const EKEY='element-6066-11e4-a52e-4f735466cecf';

test('native submission clicks submit rather than clear; Enter and missing-icon fallback remain usable',
  {skip:!executablePath}, async()=>{
    const browser=await chromium.launch({executablePath,headless:true});
    try {
      const cases=[
        {mode:'icon',query:'qm7 tv remote',initial:'other query',icon:true,expected:'icon'},
        {mode:'icon',query:'qm7 tv remote',initial:'qm7 tv remote',icon:true,expected:'icon'},
        {mode:'enter',query:'qm7 tv remote',initial:'',icon:true,expected:'enter'},
        {mode:'icon',query:'qm7 tv remote',initial:'',icon:false,expected:'enter'},
        {mode:'icon',query:'QM7 TV Remote',initial:'',icon:true,expected:'icon'},
        {mode:'icon',query:'qm7 tv remote',initial:'',icon:true,race:'verification',deferred:'VERIFICATION_PENDING'},
        {mode:'icon',query:'qm7 tv remote',initial:'',icon:true,race:'target',deferred:'SKIP_ALREADY_TARGET_URL'},
        {mode:'icon',query:'qm7 tv remote',initial:'',icon:true,race:'loading',deferred:'PAGE_TRANSITIONING'},
      ];
      for(const c of cases){
        const page=await browser.newPage();
        await page.route('http://fixture.test/**',route=>route.fulfill({contentType:'text/html',body:`
          <form role="search" onsubmit="event.preventDefault();window.submissions++;history.replaceState({},'', '/search_result.html?search_key='+encodeURIComponent(document.querySelector('input').value))">
          <input id="searchInput" type="search" aria-label="Search field" style="width:300px;height:40px">
          <button id="clear" type="button" aria-label="delete" onclick="window.clears++;document.querySelector('input').value=''">×</button>
          ${c.icon?'<div id="submit" role="button" aria-label="Submit search" tabindex="0" onclick="this.closest(\'form\').requestSubmit()">🔍</div>':''}
          </form><script>window.submissions=0;window.clears=0</script>`}));
        await page.goto('http://fixture.test/search_result.html');
        await page.locator('input').fill(c.initial);
        if(c.race) await page.evaluate(({race,query})=>{
          document.querySelector('input').addEventListener('input',()=>{
            if(document.querySelector('input').value!==query)return;
            if(race==='verification')history.replaceState({},'','/bgn_verification.html');
            if(race==='target')history.replaceState({},'','/search_result.html?search_key='+encodeURIComponent(query));
            if(race==='loading')window.pendingNavigation=true;
          });
        },{race:c.race,query:c.query});
        const events=[];
        const execute=(script,args=[])=>page.evaluate(({script,args,EKEY})=>{
          const resolved=args.map(a=>a&&a[EKEY]?document.getElementById(a[EKEY]):a);
          const result=new Function(script).apply(null,resolved);
          for(const key of ['element','button'])if(result?.[key] instanceof Element)result[key]={[EKEY]:result[key].id};
          return result;
        },{script,args,EKEY});
        const wd=async(method,suffix,body)=>{
          if(suffix==='/actions'&&method==='DELETE')return;
          if(suffix==='/actions'){
            const keys={'\uE009':'Control','\uE003':'Backspace','\uE007':'Enter'};
            for(const action of body.actions[0].actions){
              const value=keys[action.value]||action.value;
              if(action.type==='keyDown')await page.keyboard.down(value);
              if(action.type==='keyUp')await page.keyboard.up(value);
            }
            return;
          }
          const match=suffix.match(/^\/element\/([^/]+)\/click$/);
          if(match)return page.locator('#'+match[1]).click();
          throw new Error('Unexpected driver call: '+method+' '+suffix);
        };
        const context=vm.createContext({execute,wd,EKEY,QUERY:c.query,SEARCH_SUBMISSION:c.mode,
          decideSearchSubmission,URL,sleep:async()=>{},appendEvent:async(type,data)=>events.push({type,...data}),
          bootstrapPageState:()=>page.evaluate(()=>({url:location.href,text:document.body.innerText,
            readyState:window.pendingNavigation?'loading':document.readyState,visibleGoodsCount:0})),
          guardInitialProducts:async state=>({manual:classifyPageState(state)==='HUMAN_VERIFICATION'})});
        const result=await vm.runInContext(functionSource+'\nsubmitHomeSearch()',context);
        if(c.race){
          assert.equal(result.submitted,false,JSON.stringify(c));
          assert.equal(result.reason,c.deferred,JSON.stringify(c));
          assert.equal(await page.evaluate(()=>window.submissions),0,'must not submit on a changing page');
          assert.ok(events.some(e=>e.type==='SEARCH_SUBMISSION_DEFERRED'&&e.reason===c.deferred));
          await page.close();
          continue;
        }
        assert.equal(result.submitted,true,JSON.stringify(c));
        assert.equal(result.committed,true,JSON.stringify(c));
        assert.equal(await page.evaluate(()=>window.clears),0,'must never click clear');
        assert.equal(await page.evaluate(()=>window.submissions),1,'exactly one submission');
        assert.equal(events.find(e=>e.type==='SEARCH_SUBMISSION_ACTION_SENT').mode,c.expected);
        assert.equal(new URL(page.url()).searchParams.get('search_key'),c.query);
        await page.close();
      }
    }finally{await browser.close();}
  });
