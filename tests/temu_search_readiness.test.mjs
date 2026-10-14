import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import {chromium} from 'playwright';
import {inspectSearchReadiness} from '../scripts/temu_search_readiness.mjs';
const executablePath=['C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe',chromium.executablePath()].find(p=>fs.existsSync(p));
test('real gray loading skeletons remain loading even with stale empty text', {skip:!executablePath}, async()=>{
 const browser=await chromium.launch({executablePath,headless:true});
 try{
  const page=await browser.newPage();
  await page.setContent('<input type="search" value="keyboard"><p>没有“keyboard”的结果</p>'+Array(5).fill('<div class="unlabelled" style="display:inline-block;width:130px;height:150px;background:rgb(245,245,245)"></div>').join(''));
  const loading=await page.evaluate(inspectSearchReadiness);
  assert.equal(loading.loading,true);assert.equal(loading.loadingPlaceholderCount,5);
  await page.locator('.unlabelled').evaluateAll(nodes=>nodes.forEach(e=>e.remove()));
  const settled=await page.evaluate(inspectSearchReadiness);
  assert.equal(settled.loading,false);assert.equal(settled.searchInputValue,'keyboard');
 }finally{await browser.close();}
});
