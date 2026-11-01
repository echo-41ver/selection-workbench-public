import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
const source=fs.readFileSync(new URL('../scripts/selection.py',import.meta.url),'utf8');
const script=source.slice(source.indexOf('def ready_after_verification')).match(/'script':r"""([\s\S]*?)"""/)[1];
function ready({url='https://www.temu.com/search_result.html?refer_page_name=bgn_verification',text='No results for ""',enabled=true,goods=false,allowEmpty=true}={}) {
 const element={disabled:!enabled,readOnly:false,getBoundingClientRect:()=>({width:300,height:40})};
 const document={title:'Temu',readyState:'complete',body:{innerText:text},querySelectorAll:s=>s==='input[type="search"]'?[element]:s==='[data-goods-id],a[href*="-g-"]'&&goods?[element]:[]};
 return new Function('location','document','getComputedStyle','URL','target','allow',`return (function(){${script}}) (target,allow);`)({href:url},document,()=>({display:'block',visibility:'visible'}),URL,'keyboard',allowEmpty);
}
test('completed verification can resume from a usable empty search page',()=>assert.equal(ready(),true));
test('ordinary HTTP recovery still requires the target query and goods',()=>assert.equal(ready({allowEmpty:false}),false));
test('visible Chinese and English verification remain paused',()=>{
 for(const text of ['安全验证 请完成验证','安全驗證 請完成驗證','Verify you are human'])assert.equal(ready({text}),false);
 assert.equal(ready({url:'https://www.temu.com/bgn_verification.html'}),false);
});
test('disabled input and ordinary wrong-query recovery remain paused',()=>{
 assert.equal(ready({enabled:false}),false);
 assert.equal(ready({url:'https://www.temu.com/search_result.html?search_key=other',goods:true,allowEmpty:false}),false);
 assert.equal(ready({url:'https://www.temu.com/search_result.html?search_key=keyboard',goods:true}),true);
});

test('cleared verification can re-enter the current query from a usable previous-query page',()=>{
 const previous={url:'https://www.temu.com/search_result.html?search_key=other',goods:true,allowEmpty:true};
 assert.equal(ready(previous),true);
 assert.equal(ready({...previous,enabled:false}),false);
 assert.equal(ready({...previous,text:'Verify you are human'}),false);
});
