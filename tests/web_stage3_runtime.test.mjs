import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';
const html=fs.readFileSync(new URL('../web/index.html',import.meta.url),'utf8');
const code=html.slice(html.indexOf('function stage3Button(data)'),html.indexOf('function codexReviewPanel(data)'));
function view(){const nodes=new Map(),get=id=>{if(!nodes.has(id))nodes.set(id,{value:'liquid_d1',style:{},replaceChildren(){},setAttribute(){}});return nodes.get(id);};const context=vm.createContext({$:get,statusFresh:true,busy:false,codexReviewPanel(){},renderStage3Settings(){},stage3ProviderSettings:()=>undefined});vm.runInContext(code,context);return {get,context};}
test('initial page before the first status response initializes controls',()=>{const {get,context}=view();assert.doesNotThrow(()=>vm.runInContext('stage3Button(null)',context));assert.equal(get('start-stage3').disabled,true);});
test('paused running analysis exposes resume while ordinary running disables duplicate start',()=>{const {get,context}=view();context.data={category_key:'task',stage3_control:{running:true,can_start:true,progress:{total:24,pending:12}},stage3_runtime:{paused:true}};vm.runInContext('stage3Button(data)',context);assert.equal(get('start-stage3').disabled,false);assert.match(get('start-stage3').textContent,/继续/);context.data.stage3_runtime.paused=false;vm.runInContext('stage3Button(data)',context);assert.equal(get('start-stage3').disabled,true);});
