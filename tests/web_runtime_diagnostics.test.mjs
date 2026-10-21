import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';

const html=fs.readFileSync(new URL('../web/index.html',import.meta.url),'utf8');
const code=html.slice(html.indexOf('const secretKey='),html.indexOf('function text(value)'));

test('large market pools keep their count without expanding every row in runtime details',()=>{
 const rows=Array.from({length:5939},(_,i)=>({goods_id:String(i),title:'商品内容'.repeat(200),nested:{image:'data:image/png;base64,'+'a'.repeat(2000)}}));
 const data={category_key:'original',loaded_runtime:{commit:'verified',source_sha256:'actual'},
  collection_sources:{frontend_unique_goods:7,provider_supplement_goods:5932,analysis_unique_goods:5939},
  stage3_control:{running:false,progress:{completed:1}},commercial:{rows},other:{api_key:'hidden-value'}};
 const before=JSON.stringify(data),context=vm.createContext({data});vm.runInContext(code,context);
 const summary=vm.runInContext('runtimeDiagnosticSnapshot(data)',context),serialized=JSON.stringify(summary);
 assert.equal(summary.loaded_runtime.commit,'verified');
 assert.equal(summary.collection_sources.analysis_unique_goods,5939);
 assert.equal(summary.其他状态.commercial.rows.total_items,5939);
 assert.ok(serialized.length<20000);
 assert.ok(!serialized.includes('hidden-value'));
 assert.equal(JSON.stringify(data),before,'only the diagnostic view is summarized');
 assert.match(html,/\$\('raw'\)\.textContent=JSON\.stringify\(runtimeDiagnosticSnapshot\(data\),null,2\)/);
});

test('deep diagnostic state is bounded and masks credentials before summarizing values',()=>{
 const data={category_key:'original',phase:'停止中',active:true,password:'private',
  nested:{a:{b:{c:{d:{api_key:'also-private',large:'x'.repeat(1000000)}}}}}};
 const context=vm.createContext({data});vm.runInContext(code,context);
 const result=vm.runInContext('JSON.stringify(runtimeDiagnosticSnapshot(data))',context);
 assert.ok(result.length<3000);assert.ok(!result.includes('private'));
 assert.match(result,/停止中/);assert.match(result,/已隐藏/);
});
