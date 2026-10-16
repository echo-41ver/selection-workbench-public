import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';
const html=fs.readFileSync(new URL('../web/index.html',import.meta.url),'utf8');
const code=html.slice(html.indexOf('async function loadVisualReview()'),html.indexOf("$('visual-review').addEventListener"));
function context(response,changed=false){
 const box={children:[],replaceChildren(){this.children=[];this.textContent='';},append(p){this.children.push(p.textContent);}};
 const ctx=vm.createContext({$:()=>box,lastStatus:{category_key:'current'},document:{createElement:()=>({})},Date,
 fetch:async url=>{assert.equal(url,'/api/visual-review');if(changed)ctx.lastStatus.category_key='new';return {ok:true,json:async()=>response};}});
 vm.runInContext(code,ctx);return {ctx,box};
}
test('review shows snapshot source, explicit cause and evidence without posting a paid action',async()=>{
 const {ctx,box}=context({available:true,message:'仅为样例',source:'最近已发布综合 Excel',filename:'结果.xlsx',updated_at:1,examples:[{asin:'A',goods_id:'T',state:'待复核',reason:'需要核对部件排列',evidence:'Amazon 三列；TEMU 侧面被遮挡'}]});
 await vm.runInContext('loadVisualReview()',ctx);
 assert.ok(box.children.some(s=>s.includes('结果.xlsx')));
 assert.ok(box.children.includes('需要核对部件排列'));
 assert.ok(box.children.includes('Amazon 三列；TEMU 侧面被遮挡'));
});
test('task change while loading cannot display old task evidence',async()=>{
 const {ctx,box}=context({available:true,examples:[{reason:'old secret result'}]},true);
 await vm.runInContext('loadVisualReview()',ctx);assert.equal(box.children.length,0);
 assert.match(box.textContent,/任务已切换/);
});
