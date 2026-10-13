import test from 'node:test';
import assert from 'node:assert/strict';
import {confirmedEmptyQueryResults} from '../scripts/temu_initial_wait_policy.mjs';
const query='amazon basic multi purpose bench scraper';
const page={url:'https://www.temu.com/search_result.html?search_key='+encodeURIComponent(query),
  visibleGoodsCount:0,rows:[],readyState:'complete',text:`没有“${query}”的结果\n搜索结果会根据库存水平和区域市场情况实时更新。`};
test('explicit localized empty results match the exact keyword',()=>{
  assert.equal(confirmedEmptyQueryResults(page,query),true);
  assert.equal(confirmedEmptyQueryResults({...page,text:`No results for "${query}"`},query),true);
});
test('loading, another keyword, real products and human gates are never skipped',()=>{
  for(const patch of [{text:'Loading...'},{text:'没有“different keyword”的结果'},
    {visibleGoodsCount:1},{url:'https://www.temu.com/search_result.html'},
    {url:'https://www.temu.com/bgn_verification.html'},{loginFormVisible:true},
    {text:page.text+' 安全验证 验证码'}])assert.equal(confirmedEmptyQueryResults({...page,...patch},query),false);
});
