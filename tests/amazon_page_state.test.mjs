import test from 'node:test';
import assert from 'node:assert/strict';
import {chromium} from 'playwright';
import {inspectAmazonPage,recoverMisclassifiedContinuation,sanitizeAmazonDiagnostic} from '../scripts/amazon_page_state.mjs';
import {openAmazonDetail} from '../scripts/amazon_browser_recovery.mjs';
import fs from 'node:fs/promises';
import vm from 'node:vm';
import {createServer} from 'node:http';
import {amazonUsdPrice} from '../scripts/amazon_price.mjs';
import {amazonDetailBlock} from '../scripts/amazon_detail_queue.mjs';
import {selectAmazonMainImage} from '../scripts/collection_checkpoint.mjs';
import {qualifyAmazonDetail} from '../scripts/amazon_detail_quality.mjs';
import {inspectAmazonImage} from '../scripts/amazon_public_image.mjs';
const launchBrowser=()=>chromium.launch({headless:true,
  ...(process.env.AMAZON_BROWSER_PATH?{executablePath:process.env.AMAZON_BROWSER_PATH}:{})});

test('real DOM: ordinary continue button resumes original product after homepage; CAPTCHA remains blocked',async()=>{
  const browser=await launchBrowser();
  try {
    const context=await browser.newContext();
    let continued=false,clicks=0,loads=0;
    await context.route('https://www.amazon.com/**',async route=>{
      const pathname=new URL(route.request().url()).pathname;
      if(pathname==='/errors_page/validateCaptcha') {
        clicks++;continued=true;return route.fulfill({contentType:'text/html',body:'<h1>Amazon home</h1>'});
      }
      if(pathname.startsWith('/dp/'))loads++;
      const body=pathname==='/'?'<h1>Amazon home</h1>':continued?'<h1 id="productTitle">Original product</h1>':
        '<h1>Click the button below to continue shopping</h1><form action="/errors_page/validateCaptcha"><input type="hidden" name="token" value="fixture"><button type="submit">Continue shopping</button></form>';
      await route.fulfill({contentType:'text/html',body});
    });
    const page=await context.newPage();let response;
    const inspect=async()=>({...await inspectAmazonPage(page),http_status:response?.status()});
    await openAmazonDetail(page,'https://www.amazon.com/dp/B00ET5VMTU',{inspect,onResponse:r=>response=r});
    assert.equal(await page.locator('#productTitle').textContent(),'Original product');
    assert.equal(clicks,1);assert.equal(loads,2);
    await page.setContent('<form action="/errors_page/validateCaptcha"><h1>Enter the characters you see</h1><input id="captchacharacters"><button>Continue shopping</button></form>');
    const gate=await inspectAmazonPage(page);assert.equal(gate.captcha,true);assert.equal(gate.continue_shopping,false);
    await page.setContent('<h1>Click the button below to continue shopping</h1><form action="https://example.org/"><button type="submit">Continue shopping</button></form>');
    assert.equal((await inspectAmazonPage(page)).continue_shopping,false);
  } finally {await browser.close();}
});

test('full checkpoint page detects current continuation text after navigation and returns to ranked product',async()=>{
  let continued=false,clicks=0,loads=0;
  const gate=`<nav>${'Amazon navigation '.repeat(100)}</nav>
    <p>To continue, please click the "Continue shopping" button below.</p>
    <form method="post" action="/checkpoint/continue?token=fixture-sensitive"><input type="hidden" name="token" value="fixture-sensitive">
    <input type="submit" aria-labelledby="continue-label"><span id="continue-label">Continue shopping</span></form>`;
  // Serve real redirects on loopback: fulfilled remote 302s can escape route
  // interception, which would make an otherwise offline regression contact Amazon.
  const server=createServer((request,response)=>{
    const pathname=new URL(request.url,'http://fixture').pathname;
    if(pathname==='/checkpoint/continue') {
      if(request.method==='POST') {
        clicks++;continued=true;response.writeHead(302,{location:'/'});return response.end();
      }
      response.setHeader('content-type','text/html');return response.end(gate);
    }
    if(pathname.startsWith('/dp/')) {
      loads++;
      if(!continued){response.writeHead(302,{location:'/checkpoint/continue?token=fixture-sensitive'});return response.end();}
    }
    response.setHeader('content-type','text/html');
    response.end(pathname==='/'?'<h1>Amazon home</h1>':'<h1 id="productTitle">Original ranked keyboard</h1>');
  });
  await new Promise(resolve=>server.listen(0,'127.0.0.1',resolve));
  const origin=`http://127.0.0.1:${server.address().port}`;
  let browser;
  try {
    browser=await launchBrowser();
    const context=await browser.newContext();
    await context.route('**/*',route=>new URL(route.request().url()).origin===origin?route.continue():route.abort());
    const page=await context.newPage();let response;
    const observed=[];
    const inspect=async()=>{const value={...await inspectAmazonPage(page),http_status:response?.status()};observed.push(value);return value;};
    await openAmazonDetail(page,origin+'/dp/B00ET5VMTU',{inspect,onResponse:r=>response=r});
    assert.equal(await page.locator('#productTitle').textContent(),'Original ranked keyboard');
    assert.equal(clicks,1);assert.equal(loads,2);
    assert.ok(observed.some(row=>row.continue_shopping && !/To continue/.test(row.text)));
    assert.doesNotMatch(JSON.stringify(observed),/fixture-sensitive/);
    await page.goto(origin+'/checkpoint/continue?token=fixture-sensitive');
    await page.locator('form').evaluate(form=>{const input=document.createElement('input');input.type='text';form.append(input);});
    const unsafe=await inspectAmazonPage(page);
    assert.equal(unsafe.continue_shopping,false);assert.equal(unsafe.continuation_required,true);
    assert.equal(amazonDetailBlock(unsafe).retryable,false);
    await page.setContent('<p>Check the box to continue. I am human</p><form><input type="checkbox"><button>Continue shopping</button></form>');
    const checkbox=await inspectAmazonPage(page);
    assert.equal(checkbox.continue_shopping,false);assert.equal(amazonDetailBlock(checkbox).retryable,false);
  } finally {if(browser)await browser.close();await new Promise(resolve=>server.close(resolve));}
});

test('migration leaves real CAPTCHA, login, exhausted budget and current detector evidence alone',()=>{
  for(const error of [
    {code:'AMAZON_VERIFICATION_REQUIRED',text:'Enter the characters you see'},
    {code:'AMAZON_LOGIN_REQUIRED',text:'Click the button below to continue shopping Continue shopping Conditions of Use'},
    {code:'AMAZON_VERIFICATION_REQUIRED',continue_shopping:false,text:'Click the button below to continue shopping Continue shopping Conditions of Use'},
  ]) {
    const row={detail_error:error,detail_image_attempts:10};assert.equal(recoverMisclassifiedContinuation(row),row);
  }
});

for(const haul of [false,true]) test(`real detail parser captures ${haul?'Haul':'standard'} specifications and does not mislabel CNY as USD`,async()=>{
  const browser=await launchBrowser();
  try {
    const context=await browser.newContext();
    const imagePage=await context.newPage();
    const imageBytes=Buffer.from(await imagePage.evaluate(()=>{const c=document.createElement('canvas');c.width=c.height=900;return c.toDataURL().split(',')[1];}),'base64');
    await imagePage.close();
    const detailContext={newPage:()=>context.newPage(),request:{get:async()=>({ok:()=>true,status:()=>200,
      headers:()=>({'content-type':'image/png'}),body:async()=>imageBytes,dispose:async()=>{}})}};
    await context.route('https://www.amazon.com/**',route=>route.fulfill({contentType:'text/html',body:`
      <h1 id="productTitle">Drill</h1><div id="corePriceDisplay_desktop_feature_div"><span class="a-offscreen">CNY 600.00</span></div>
      ${haul?`<div id="haul-productFacts-desktop-expander">
        <div class="haul-productFacts-desktop-row"><span class="haul-productFacts-desktop-label">Brand Name:</span><span class="haul-productFacts-desktop-value">DEWALT</span></div>
        <div class="haul-productFacts-desktop-row"><span class="haul-productFacts-desktop-label">Voltage:</span><span class="haul-productFacts-desktop-value">20 volts</span></div>
        <div class="haul-productFacts-desktop-row"><span class="haul-productFacts-desktop-label">Item Weight:</span><span class="haul-productFacts-desktop-value">3.6 pounds</span></div>
      </div>`:'<table class="prodDetTable"><tr><th>Brand</th><td>DEWALT</td></tr><tr><th>Voltage</th><td>20 volts</td></tr><tr><th>Item Weight</th><td>3.6 pounds</td></tr></table>'}
      <table><tr><th>Unrelated recommendation</th><td>100V</td></tr></table>
      <img id="landingImage" data-old-hires="https://images-na.ssl-images-amazon.com/images/I/fixture.jpg">
    `}));
    const source=await fs.readFile(new URL('../scripts/collect_amazon_search_frontend.mjs',import.meta.url),'utf8');
    const facts=source.slice(source.indexOf('async function detailFacts('),source.indexOf('const browser ='));
    const sandbox=vm.createContext({context:detailContext,record:{asin:'B00ET5VMTU',product_url:'https://www.amazon.com/dp/B00ET5VMTU',price_usd:92.09},
      inspectAmazonPage,openAmazonDetail,amazonDetailBlock,amazonUsdPrice,selectAmazonMainImage,sanitizeAmazonDiagnostic,
      qualifyAmazonDetail,inspectAmazonImage,
      isHttps:value=>String(value || '').startsWith('https://'),clean:value=>String(value).trim()});
    vm.runInContext(facts,sandbox);
    const value=await vm.runInContext('detailFacts(context,record)',sandbox);
    assert.equal(value.detail_status,'详情页已采集');assert.equal(value.brand,'DEWALT');
    assert.equal(value.specifications.length,3);assert.equal(value.specifications[1].value,'20 volts');
    assert.equal(value.price_usd,92.09);assert.equal(value.detail_price_text,'CNY 600.00');
  } finally {await browser.close();}
});
