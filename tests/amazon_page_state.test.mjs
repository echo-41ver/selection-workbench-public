import test from 'node:test';
import assert from 'node:assert/strict';
import {chromium} from 'playwright';
import {inspectAmazonPage,recoverMisclassifiedContinuation} from '../scripts/amazon_page_state.mjs';
import {openAmazonDetail} from '../scripts/amazon_browser_recovery.mjs';
import fs from 'node:fs/promises';
import vm from 'node:vm';
import {amazonUsdPrice} from '../scripts/amazon_price.mjs';
import {amazonDetailBlock} from '../scripts/amazon_detail_queue.mjs';
import {selectAmazonMainImage} from '../scripts/collection_checkpoint.mjs';
import {qualifyAmazonDetail} from '../scripts/amazon_detail_quality.mjs';
import {inspectAmazonImage} from '../scripts/amazon_public_image.mjs';

test('real DOM: ordinary continue button resumes original product after homepage; CAPTCHA remains blocked',async()=>{
  const browser=await chromium.launch({headless:true});
  try {
    const context=await browser.newContext();
    let continued=false,clicks=0,loads=0;
    await context.route('https://www.amazon.com/**',async route=>{
      const pathname=new URL(route.request().url()).pathname;
      if(pathname==='/errors_page/validateCaptcha') {
        clicks++;continued=true;return route.fulfill({status:302,headers:{location:'https://www.amazon.com/'}});
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

test('migration leaves real CAPTCHA, login, exhausted budget and current detector evidence alone',()=>{
  for(const error of [
    {code:'AMAZON_VERIFICATION_REQUIRED',text:'Enter the characters you see'},
    {code:'AMAZON_LOGIN_REQUIRED',text:'Click the button below to continue shopping Continue shopping Conditions of Use'},
    {code:'AMAZON_VERIFICATION_REQUIRED',continue_shopping:false,text:'Click the button below to continue shopping Continue shopping Conditions of Use'},
  ]) {
    const row={detail_error:error,detail_image_attempts:10};assert.equal(recoverMisclassifiedContinuation(row),row);
  }
});

test('real detail parser captures modern specifications and does not mislabel CNY as USD',async()=>{
  const browser=await chromium.launch({headless:true});
  try {
    const context=await browser.newContext();
    const imagePage=await context.newPage();
    const imageBytes=Buffer.from(await imagePage.evaluate(()=>{const c=document.createElement('canvas');c.width=c.height=900;return c.toDataURL().split(',')[1];}),'base64');
    await imagePage.close();
    const detailContext={newPage:()=>context.newPage(),request:{get:async()=>({ok:()=>true,status:()=>200,
      headers:()=>({'content-type':'image/png'}),body:async()=>imageBytes,dispose:async()=>{}})}};
    await context.route('https://www.amazon.com/**',route=>route.fulfill({contentType:'text/html',body:`
      <h1 id="productTitle">Drill</h1><div id="corePriceDisplay_desktop_feature_div"><span class="a-offscreen">CNY 600.00</span></div>
      <table class="prodDetTable"><tr><th>Brand</th><td>DEWALT</td></tr><tr><th>Voltage</th><td>20 volts</td></tr><tr><th>Item Weight</th><td>3.6 pounds</td></tr></table>
      <table><tr><th>Unrelated recommendation</th><td>100V</td></tr></table>
      <img id="landingImage" data-old-hires="https://images-na.ssl-images-amazon.com/images/I/fixture.jpg">
    `}));
    const source=await fs.readFile(new URL('../scripts/collect_amazon_search_frontend.mjs',import.meta.url),'utf8');
    const facts=source.slice(source.indexOf('async function detailFacts('),source.indexOf('const browser ='));
    const sandbox=vm.createContext({context:detailContext,record:{asin:'B00ET5VMTU',product_url:'https://www.amazon.com/dp/B00ET5VMTU',price_usd:92.09},
      inspectAmazonPage,openAmazonDetail,amazonDetailBlock,amazonUsdPrice,selectAmazonMainImage,
      qualifyAmazonDetail,inspectAmazonImage,
      isHttps:value=>String(value || '').startsWith('https://'),clean:value=>String(value).trim()});
    vm.runInContext(facts,sandbox);
    const value=await vm.runInContext('detailFacts(context,record)',sandbox);
    assert.equal(value.detail_status,'详情页已采集');assert.equal(value.brand,'DEWALT');
    assert.equal(value.specifications.length,3);assert.equal(value.specifications[1].value,'20 volts');
    assert.equal(value.price_usd,92.09);assert.equal(value.detail_price_text,'CNY 600.00');
  } finally {await browser.close();}
});
