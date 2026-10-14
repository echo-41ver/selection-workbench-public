import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';

test('market cards name the all-collected denominator and recent subset',()=>{
 const html=fs.readFileSync(new URL('../web/index.html',import.meta.url),'utf8');
 const code=html.slice(html.indexOf('function renderFrontendMarket(report,'),html.indexOf('function renderCollectionTotal('));
 function node(){return {children:[],append(...items){this.children.push(...items);},insertBefore(item){this.children.splice(1,0,item);},textContent:''};}
 const box=node();box.append(node());
 const report={primary_category_query:'keyboard',all_keywords:{
  sales:{sample_products:1625,top5_displayed_share_approx:18.4,signal:{reason:'Top 5展示销量之和 ÷ 全部1625件去重商品的可读展示销量之和；414件销量未知'}},
  recent:{sample_products:1030,keyword_count:15,signal:{confirmed_sold_lower_bound_percent:70,reason:'最近上架排序的1030件去重商品'}}},
  queries:[{query:'keyboard',sales:{sample_products:1084,top5_displayed_share_approx:14.9},recent:{sample_products:600,with_sales_products:420}}]};
 const context=vm.createContext({document:{createElement:node},marketRenderKey:'',renderFrontendMarketDetails(){},collapseEvidence(){},$:()=>box,lastStatus:{}});
 vm.runInContext(code,context);context.report=report;context.box=box;
 vm.runInContext('renderFrontendMarket(report,box,{})',context);
 const text=element=>element.textContent+element.children.map(text).join(' ');
 const content=text(box);
 assert.match(content,/约 18.4%/);
 assert.match(content,/全部1625件/);
 assert.match(content,/最近上架排序去重 600 件/);
 assert.match(content,/覆盖 1084 件.*同一个全部商品销量分母 ≈ 14.9%/);
 assert.doesNotMatch(content,/已识别自然商品|自然商品不足|单独统计为/);
});
