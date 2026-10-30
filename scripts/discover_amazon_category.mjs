import { chromium } from 'playwright';
import {existsSync} from 'node:fs';
import path from 'node:path';
import {inspectAmazonPage} from './amazon_page_state.mjs';
import {openAmazonDetail} from './amazon_browser_recovery.mjs';
import {readAmazonDocument} from './amazon_page_read.mjs';
import {amazonBrowserRoute} from './amazon_browser_route.mjs';
const query = process.argv[2]==='--query' ? process.argv[3]?.trim() : null;
if (process.argv[2]==='--query' && (!query || query.length>120)) throw new Error('Invalid product search query');
const url = query ? 'https://www.amazon.com/s?k='+encodeURIComponent(query) : process.argv[2];
const identity = value => { try { const u = new URL(value); const match=u.pathname.match(/\/(?:zgbs|gp\/bestsellers)(?=\/|$)(.*)/); if(u.protocol!=='https:'||!['www.amazon.com','amazon.com'].includes(u.hostname)||u.username||u.password||u.port||!match)return null; const path=match[1].split('/ref=')[0].replace(/^\/+|\/+$/g,''); return !path||/^[A-Za-z0-9_-]+(?:\/\d+)?$/.test(path)?'/zgbs'+(path?'/'+path:''):null; } catch { return null; } };
const valid = value => identity(value)!==null;
if (!query && !valid(url)) throw new Error('Invalid Amazon Best Sellers URL');
let browser;
let response;
let stage = 'browser_launch';
try {
  const installedEdge=process.platform==='win32' ? [process.env['ProgramFiles(x86)'],process.env.ProgramFiles]
    .filter(Boolean).map(directory=>path.join(directory,'Microsoft/Edge/Application/msedge.exe')).find(existsSync) : null;
  browser = await chromium.launch({ headless: true,
    executablePath: process.env.AMAZON_BROWSER_PATH || installedEdge || chromium.executablePath(),
    ...amazonBrowserRoute().launchOptions });
  const context = await browser.newContext({ viewport: { width: 1440, height: 1000 }, locale: 'en-US' });
  const page = await context.newPage();
  stage = 'page_navigation';
  if(query) {
    await openAmazonDetail(page,url,{
      inspect:async()=>({...await inspectAmazonPage(page),http_status:response?.status() ?? null}),
      onResponse:value=>{response=value;},
      readySelector:'#search, [data-component-type="s-search-result"]',
      hasContent:evidence=>!evidence.captcha && /Amazon/.test(evidence.page_title),
    });
    const products=await readAmazonDocument(page,()=>page.evaluate(()=>[...document.querySelectorAll('[data-component-type="s-search-result"][data-asin]')]
      .filter(item=>/^[A-Z0-9]{10}$/.test(item.dataset.asin) && item.querySelector('h2'))
      .slice(0,2).map(item=>({asin:item.dataset.asin,title:item.querySelector('h2').textContent.trim().slice(0,240)}))));
    const candidates=[],failures=[];
    for(const product of products) {
      try {
        await openAmazonDetail(page,'https://www.amazon.com/dp/'+product.asin,{
          inspect:async()=>({...await inspectAmazonPage(page),http_status:response?.status() ?? null}),
          onResponse:value=>{response=value;},
        });
        const links=await readAmazonDocument(page,()=>page.evaluate(()=>[...document.querySelectorAll('a[href]')]
          .filter(a=>/\/(?:zgbs|gp\/bestsellers)(?:\/|$)/.test(new URL(a.href).pathname))
          .map(a=>({url:a.href,text:a.textContent.trim().slice(0,160)}))));
        for(const link of links) {
          const key=identity(link.url);
          if(key && /\/\d+$/.test(key) && !candidates.some(item=>identity(item.url)===key)) candidates.push({...link,product});
        }
      } catch(error) {
        if(error.blocked || error.retryable===false) throw error;
        failures.push({asin:product.asin,message:error.message});
      }
    }
    if(!candidates.length && failures.length) throw new Error('Product rank links unavailable: '+failures.map(item=>item.message).join('; '));
    console.log(JSON.stringify({query,products,candidates,failures}));
  } else {
  await openAmazonDetail(page,url,{
    inspect:async()=>({...await inspectAmazonPage(page),http_status:response?.status() ?? null}),
    onResponse:value=>{response=value;},
    readySelector:'#zg-left-col, [role="tree"], .zg-bdg-ctr, #zg_banner_text, .zg-banner-text',
    hasContent:evidence=>evidence.has_rank_badges || /Best Sellers/i.test(evidence.page_title),
  });
  if (!valid(page.url())) throw Object.assign(new Error('Amazon redirected outside Best Sellers'),{code:'AMAZON_LISTING_NOT_RANKED'});
  stage = 'page_content';
  await page.waitForTimeout(1500);
  const result = await readAmazonDocument(page,()=>page.evaluate(() => ({
    url: location.href,
    title: document.title,
    heading: [...document.querySelectorAll('h1,h2')].map(x => x.textContent.trim()).find(x => /^Best Sellers in /i.test(x)) || document.querySelector('#zg_banner_text, .zg-banner-text, h1')?.textContent?.trim() || '',
    rank_count: document.querySelectorAll('.zg-bdg-ctr').length || document.querySelectorAll('.zg-bdg-text').length,
    blocked: /enter the characters|robot check|solve this puzzle/i.test(document.body.innerText.slice(0, 10000)),
    links: [...new Set([...document.querySelectorAll('#zg-left-col a[href], [role="tree"] a[href]'), ...document.querySelectorAll('a[href]')])].filter(a => /\/(zgbs|gp\/bestsellers)(\/|$)/.test(new URL(a.href).pathname) && a.textContent.trim()).map(a => ({url:a.href, text:a.textContent.trim().slice(0,150)})),
  })));
  if (!valid(result.url)) throw Object.assign(new Error('Amazon redirected outside Best Sellers'),{code:'AMAZON_LISTING_NOT_RANKED'});
  const seen=new Set();
  result.links = result.links.filter(x => {const key=identity(x.url); if(!key||seen.has(key))return false;seen.add(key);return true;}).slice(0, 180);
  if (result.blocked) throw Object.assign(new Error('Amazon verification required'), {blocked:true});
  if (!result.heading || (!result.rank_count && !result.links.length)) throw Object.assign(new Error('Amazon listing content unavailable'),{code:'AMAZON_LISTING_NOT_RANKED'});
  console.log(JSON.stringify(result));
  }
} catch (error) {
  console.error(JSON.stringify({stage, url, exception_type:error.name, message:error.message,
    stack:error.stack, http_status:error.http_status ?? response?.status() ?? (/AMAZON_HTTP_(403|429)/.exec(error.code||'')?.[1] ? Number(/AMAZON_HTTP_(403|429)/.exec(error.code)[1]):null),
    blocked:!!error.blocked || error.retryable===false, code:error.code ?? null}));
  process.exitCode = 1;
} finally { if (browser) await browser.close(); }
