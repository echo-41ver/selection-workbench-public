// Test-only Chromium transport. Production collector unchanged; no public network.
import {chromium} from 'playwright';
import fs from 'node:fs/promises';
const directory=process.env.AMAZON_FIXTURE_DIR;
if(!directory) throw new Error('fixture directory required');
const launch=chromium.launch.bind(chromium);
let continued=false;
chromium.launch=async options=>{
  const browser=await launch(options);
  const create=browser.newContext.bind(browser);
  browser.newContext=async options=>{
    const context=await create(options);
    await context.route('**/*',async route=>{
      const url=new URL(route.request().url());
      await fs.appendFile(`${directory}/requests.jsonl`,JSON.stringify({url:url.href})+'\n');
      if(!/\/(?:zgbs|gp\/bestsellers)\//.test(url.pathname)) return route.fulfill({status:500,body:'Unexpected detail request during listing preparation'});
      const page=url.searchParams.get('pg')==='2'?2:1;
      if(page===2 && process.env.AMAZON_FIXTURE_FAIL_PAGE2==='1')
        return route.fulfill({status:503,contentType:'text/html',body:'Temporary failure'});
      if(page===1 && process.env.AMAZON_FIXTURE_CONTINUE==='1' && !continued) {
        continued=true;
        return route.fulfill({status:200,contentType:'text/html',body:
          '<p>Click the button below to continue shopping</p><form action="/zgbs/hi/552794"><button>Continue shopping</button></form>'});
      }
      const body=await fs.readFile(`${directory}/page${page}.html`,'utf8');
      return route.fulfill({status:200,contentType:'text/html',body});
    });
    const newPage=context.newPage.bind(context);
    context.newPage=async()=>{const page=await newPage();page.waitForTimeout=ms=>new Promise(resolve=>setTimeout(resolve,Math.min(ms,5)));return page;};
    return context;
  };
  return browser;
};
