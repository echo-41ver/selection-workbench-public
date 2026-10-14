import test from 'node:test';
import assert from 'node:assert/strict';
import http from 'node:http';
import path from 'node:path';
import {existsSync} from 'node:fs';
import {chromium} from 'playwright';
import {readAmazonDocument} from '../scripts/amazon_page_read.mjs';
import {openAmazonDetail} from '../scripts/amazon_browser_recovery.mjs';

const navigationError=()=>new Error('page.evaluate: Execution context was destroyed, most likely because of a navigation');

test('read follows navigation without reloading and has a finite retry limit',async()=>{
 let reads=0,waits=0;
 const page={isClosed:()=>false,waitForLoadState:async()=>waits++};
 const result=await readAmazonDocument(page,async()=>{if(++reads<3)throw navigationError();return 'current document';},{sleep:async()=>{}});
 assert.equal(result,'current document');assert.equal(reads,3);assert.equal(waits,2);
 reads=0;await assert.rejects(readAmazonDocument(page,async()=>{reads++;throw navigationError();},{sleep:async()=>{}}),/Execution context/);
 assert.equal(reads,3);
});

test('unrelated failures and closed pages never retry',async()=>{
 for(const [closed,error] of [[false,new Error('invalid selector')],[true,navigationError()]]){
  let reads=0;
  await assert.rejects(readAmazonDocument({isClosed:()=>closed},async()=>{reads++;throw error;}),error);
  assert.equal(reads,1);
 }
});

test('a verification page after navigation is still stopped without refresh',async()=>{
 let inspections=0,loads=0;
 const page={url:()=> 'https://www.amazon.com/dp/B000000001',isClosed:()=>false,
  goto:async()=>{loads++;return {status:()=>200};},waitForLoadState:async()=>{}};
 await assert.rejects(openAmazonDetail(page,page.url(),{sleep:async()=>{},inspect:async()=>{
  if(++inspections===1)throw navigationError();return {captcha:true,http_status:200};
 }}),/验证/);
 assert.equal(inspections,2);assert.equal(loads,1);
});

test('actual navigation during evaluate is re-read from the new document',async()=>{
 const server=http.createServer((request,response)=>{
  response.setHeader('Content-Type','text/html');
  response.end(request.url==='/ready'?'<h1>ready document</h1>':'<h1>moving document</h1>');
 });
 await new Promise(resolve=>server.listen(0,'127.0.0.1',resolve));
 const edge=process.platform==='win32'?[process.env['ProgramFiles(x86)'],process.env.ProgramFiles]
  .filter(Boolean).map(p=>path.join(p,'Microsoft/Edge/Application/msedge.exe')).find(existsSync):null;
 let browser;
 try{
  browser=await chromium.launch({headless:true,executablePath:edge || chromium.executablePath()});
  const page=await browser.newPage();await page.goto(`http://127.0.0.1:${server.address().port}/moving`);
  let reads=0;
  const result=await readAmazonDocument(page,()=>{
   reads++;return page.evaluate(()=>new Promise(resolve=>{
    if(location.pathname==='/moving'){
     setTimeout(()=>location.assign('/ready'),20);
     setTimeout(()=>resolve(document.body.innerText),2000);
    }else resolve(document.body.innerText);
   }));
  });
  assert.match(result,/ready document/);assert.equal(reads,2);
 }finally{if(browser)await browser.close();await new Promise(resolve=>server.close(resolve));}
});
