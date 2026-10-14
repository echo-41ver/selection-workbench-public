import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';
const html=fs.readFileSync(new URL('../web/index.html',import.meta.url),'utf8');
const code=html.slice(html.indexOf('let stage3SettingsDirty='),html.indexOf('function businessSettings'));

test('four provider modes bind endpoint model protocol and concurrency without retaining key',()=>{
 const nodes=new Map();const get=id=>{if(!nodes.has(id))nodes.set(id,{value:'',hidden:false});return nodes.get(id);};
 const context=vm.createContext({$:get,lastStatus:null});vm.runInContext(code,context);
 const cases=[['liquid_d1','https://api.liquid.ai/decisions/v1/systemone','d1','decisions',4],
 ['multimodal_api','https://api.deepseek.com','deepseek-flash','responses',4],
 ['local_model','http://127.0.0.1:18181/v1/systemone','d1-3b','decisions',1],['local_agent','','gpt-6.1-sol','responses',4]];
 get('stage3-confidence').value='90';
 for(const [mode,url,model,protocol,workers] of cases){get('stage3-mode').value=mode;get('stage3-api-key').value='TEST_ONLY_OLD_KEY';vm.runInContext('chooseStage3Mode()',context);const p=vm.runInContext('stage3ProviderSettings()',context);assert.equal(p.mode,mode);assert.equal(p.base_url,url);assert.equal(p.model,model);assert.equal(p.protocol,protocol);assert.equal(p.workers,workers);assert.equal(get('stage3-api-key').value,'');assert.equal(get('stage3-api-fields').hidden,mode==='local_agent');}
});

test('third-layer click submits a dedicated key and clears the input',()=>{
 const nodes={'start-stage3':{addEventListener:(_event,cb)=>context.click=cb},'stage3-api-key':{value:'TEST_ONLY_VISUAL_KEY'}};
 let submitted;const context=vm.createContext({$:id=>nodes[id],stage3ProviderSettings:()=>({mode:'liquid_d1'}),stage3SettingsDirty:true,action:(path,body)=>submitted={path,body}});
 vm.runInContext(html.split('\n').find(x=>x.startsWith("$('start-stage3').addEventListener")),context);context.click();
 assert.equal(submitted.path,'/api/start-stage3');assert.equal(submitted.body.api_key,'TEST_ONLY_VISUAL_KEY');assert.equal(submitted.body.provider.mode,'liquid_d1');assert.equal(nodes['stage3-api-key'].value,'');
});

test('first render before status arrives preserves the initialization flow',()=>{
 const nodes=new Map();const get=id=>{if(!nodes.has(id))nodes.set(id,{value:'',hidden:false});return nodes.get(id);};
 const context=vm.createContext({$:get});vm.runInContext(code,context);
 assert.doesNotThrow(()=>vm.runInContext('renderStage3Settings(null)',context));
});

test('ordinary page keeps one primary delivery and collapses optional flows',()=>{
 assert.match(html,/\[data-excel\]:not\(\[data-excel="layer123"\]\)/);
 assert.match(html,/<summary>规则生成服务设置/);assert.match(html,/<summary>调整同款规则/);
 assert.match(html,/function renderBusiness\([^\n]*renderSelectionSummary/);
 for(const mode of ['liquid_d1','multimodal_api','local_model','local_agent'])assert.match(html,new RegExp('option value="'+mode+'"'));
 assert.doesNotMatch(html,/对（API /);
 assert.doesNotMatch(html,/中英文界面均可识别/);
 assert.doesNotMatch(html,/id="codex-review-controls"/);
});


test('static click handlers have actual page controls so initialization can reach polling',()=>{
 const ids=new Set([...html.matchAll(/\bid="([^"]+)"/g)].map(x=>x[1]));
 for(const match of html.matchAll(/\$\('([^']+)'\)\.addEventListener/g))assert.ok(ids.has(match[1]),'missing UI control: '+match[1]);
 assert.match(html,/<button[^>]*id="configure-stage3"[^>]*>保存方案与 Key（不启动）<\/button>/);
 assert.doesNotMatch(html,/d1:free/);
});
