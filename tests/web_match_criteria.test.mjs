import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';
const html=fs.readFileSync(new URL('../web/index.html',import.meta.url),'utf8');
const code=html.slice(html.indexOf('let criteriaSelection='),html.indexOf('function stage3Heartbeat'));
function setup(){
 const node=()=>({children:[],dataset:{},style:{},handlers:{},append(...items){this.children.push(...items);},replaceChildren(){this.children=[];},setAttribute(){},addEventListener(name,fn){this.handlers[name]=fn;}});
 const nodes=new Map(),get=id=>{if(!nodes.has(id))nodes.set(id,node());return nodes.get(id);};
 const context=vm.createContext({$:get,document:{createElement:node},updateControls(){},feedback(){},lastStatus:null,
  AbortController,setTimeout,clearTimeout,poll:async()=>{}});
 vm.runInContext(code,context);
 return {get,context};
}
const catalog={revision:'r1',message:'文字第一层，图片第三层',items:[
 {id:'size',kind:'text',label:'尺寸',rule:'明确文字冲突才排除',selected:true,observations:[{asin:'A1',value:'7英寸'},{asin:'A2',value:'8英寸'}]},
 {id:'body',kind:'visual',label:'外壳形状',rule:'比较本体',selected:true,observations:[]} ]};
test('text and image checkboxes are separate and keep per-product evidence',()=>{
 const {get,context}=setup();context.data={draft:{id:'draft',match_criteria:catalog}};
 vm.runInContext('renderMatchCriteria(data)',context);
 assert.equal(get('match-text-rows').children.length,1);assert.equal(get('match-visual-rows').children.length,1);
 const evidence=get('match-text-rows').children[0].children[2].children[0];
 assert.deepEqual(evidence.children.slice(1).map(node=>node.textContent),['A1：7英寸','A2：8英寸']);
 const input=get('match-text-rows').children[0].children[0].children[0];input.checked=false;input.handlers.change();
 vm.runInContext('renderMatchCriteria(data)',context);
 assert.equal(vm.runInContext('criteriaSelection.has("size")',context),false);
 assert.equal(vm.runInContext('criteriaDirty',context),true);
 assert.equal(get('apply-match-criteria').disabled,false);
});
test('applied choices use a versioned request, while running tasks are read only',async()=>{
 const {get,context}=setup();context.lastStatus={draft:{id:'draft',match_criteria:catalog}};
 vm.runInContext('renderMatchCriteria(lastStatus);criteriaSelection.delete("size");criteriaDirty=true',context);
 const requests=[];context.fetch=async(url,options)=>{requests.push({url,body:JSON.parse(options.body)});return {ok:true,json:async()=>({phase:'已保存'})};};
 await vm.runInContext('applyMatchCriteria()',context);
 assert.equal(requests[0].url,'/api/match-criteria');assert.deepEqual(requests[0].body.selected_ids,['body']);
 assert.equal(requests[0].body.revision,'r1');
 context.running={active:true,category_key:'task',match_criteria:catalog};vm.runInContext('renderMatchCriteria(running)',context);
 assert.equal(get('match-visual-rows').children[0].children[0].children[0].disabled,true);
 assert.equal(get('apply-match-criteria').disabled,true);
});
test('Chinese names appear first and extra conditions stay selectable in a folded group',()=>{
 const {get,context}=setup();const value=structuredClone(catalog);
 value.items[0].label='Screen Size';value.items[0].label_zh='屏幕尺寸';
 value.items.push({id:'extra',kind:'text',label:'Refresh Rate',label_zh:'刷新率',display_group:'more',selected:false,observations:[]});
 context.data={draft:{id:'draft',match_criteria:value}};vm.runInContext('renderMatchCriteria(data)',context);
 assert.equal(get('match-text-rows').children[0].children[1].children[0].textContent,'屏幕尺寸');
 assert.equal(get('match-text-more').hidden,false);assert.equal(get('match-text-more-rows').children.length,1);
 const input=get('match-text-more-rows').children[0].children[0].children[0];input.checked=true;input.handlers.change();
 assert.equal(vm.runInContext('criteriaSelection.has("extra")',context),true);
});
