import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';
const html=fs.readFileSync(new URL('../web/index.html',import.meta.url),'utf8');
const code=html.slice(html.indexOf('function opText('),html.indexOf("$('op-refresh').addEventListener"));
function element(){return {children:[],style:{},dataset:{},textContent:'',append(child){this.children.push(child);},replaceChildren(){this.children=[];this.textContent='';}};}
function strings(node){return [node.textContent,...node.children.flatMap(strings)].filter(Boolean);}
function setup(data,change=false){
 const nodes=new Map();const get=id=>{if(!nodes.has(id))nodes.set(id,element());return nodes.get(id);};
 const ctx=vm.createContext({URL,Date,document:{createElement:element},$:get,lastStatus:{category_key:'mouse'},fetch:async url=>{assert.equal(url,'/api/operator');if(change)ctx.lastStatus.category_key='other';return {ok:true,json:async()=>data};}});
 vm.runInContext(code,ctx);return {ctx,get};
}
test('missing collection cannot be rendered as a known negative market finding',async()=>{
 const {ctx,get}=setup({snapshot:{message:'当前证据未齐',products:[]},delivery:{status:'NO_QUALIFIED_PRODUCT',outcome:'DATA_INCOMPLETE',message:'今日采集未完成'}});
 await vm.runInContext('loadOperator()',ctx);
 assert.ok(strings(get('op-products')).includes('数据或核验未完成，暂时无法推荐'));
 assert.ok(!strings(get('op-products')).some(s=>s.includes('今日暂未找到合格商品')));
});
test('legacy empty delivery remains unknown',()=>{
 const {ctx}=setup({});assert.equal(vm.runInContext("opOutcome({status:'NO_QUALIFIED_PRODUCT'})",ctx),'DATA_INCOMPLETE');
});
test('known no-match and duplicate-only results have distinct labels',()=>{
 const {ctx}=setup({});assert.match(vm.runInContext("opOutcomeText({outcome:'NO_MATCH'})",ctx),/已核验范围/);
 assert.match(vm.runInContext("opOutcomeText({outcome:'ALL_RECENTLY_RECOMMENDED'})",ctx),/不重复/);
});
test('evidence card shows absent rating, subset price, provenance and next steps without inventing values',()=>{
 const {ctx,get}=setup({});ctx.item={same_count:1,same_limit:5,why:['严格同款已核验'],next_actions:['补差评原文'],temu:{title:'Mouse',url:'javascript:alert(1)',price:{min:2,max:4,basis:'已采集可售规格子集'},rating:null,review_count:52,observed_at:'2026-10-13T00:00:00Z',observed_at_source:'snapshot.observed_at',evidence:{rating:{status:'MISSING'}}}};
 vm.runInContext("opCard($('card'),item)",ctx);const text=strings(get('card')).join('\n');
 assert.match(text,/评分：未取得；评价数量：52/);assert.match(text,/已采集可售规格子集/);assert.match(text,/非逐条观测时间/);assert.match(text,/数据来源：未记录/);assert.match(text,/补差评原文/);
 assert.ok(!text.includes('打开 TEMU 商品'));
});
test('stale response after changing task cannot display former evidence',async()=>{
 const {ctx,get}=setup({snapshot:{message:'old',products:[]}},true);await vm.runInContext('loadOperator()',ctx);
 assert.match(get('op-status').textContent,/任务已切换/);assert.equal(get('op-products').children.length,0);
});
