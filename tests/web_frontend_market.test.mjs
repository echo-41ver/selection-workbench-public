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

test('live progress retains all products and names the Excel same-plus-similar selection threshold',()=>{
 const html=fs.readFileSync(new URL('../web/index.html',import.meta.url),'utf8');
 const code=html.slice(html.indexOf('function renderSelectionSummary('),html.indexOf('function businessSettings('));
 function node(){return {children:[],dataset:{},textContent:'',append(...items){this.children.push(...items);},replaceChildren(){this.children=[];}};}
 const box=node();
 const context=vm.createContext({document:{createElement:node},$:()=>box});
 vm.runInContext(code,context);
 context.report={policy:{same_limit:5},products:[
  {asin:'EXCLUDE',title:'not selected',match:{confirmed:1,similar:4}},
  {asin:'SELECT',title:'selected',match:{confirmed:1,similar:3,pending:0}}]};
 context.box=box;vm.runInContext('renderSelectionSummary(report,box)',context);
 const content=element=>element.textContent+element.children.map(content).join(' ');
 assert.match(content(box),/共 2 件 Amazon 商品/);
 assert.match(content(box),/Excel 选品结论只保留同款＋类似款小于 5/);
 assert.match(content(box),/EXCLUDE.*1 4/);
 assert.match(content(box),/SELECT.*1 3/);
});

test('completed acquisition shows completion while the analysis supervisor remains active',()=>{
 const html=fs.readFileSync(new URL('../web/index.html',import.meta.url),'utf8');
 const code=html.slice(html.indexOf('function renderCollectionTotal('),html.indexOf('function render(data)'));
 const elements=new Map();const $=id=>{if(!elements.has(id))elements.set(id,{textContent:'',style:{},setAttribute(){}});return elements.get(id);};
 const context=vm.createContext({$,Number,Math});vm.runInContext(code,context);
 context.data={active:true,collection_complete:true,details:{阶段:{temu:{overall:{percent:100,completed:10,total:10,remaining:0,keyword_count:1,finished_passes:2,total_passes:2,all_passes_complete:true}}}}};
 vm.runInContext('renderCollectionTotal(data)',context);
 assert.equal($('collection-total-label').textContent,'全部采集已完成');
 context.data.collection_complete=false;context.data.details.阶段.temu.overall.all_passes_complete=false;
 vm.runInContext('renderCollectionTotal(data)',context);
 assert.match($('collection-total-eta').textContent,/收尾/);
});
