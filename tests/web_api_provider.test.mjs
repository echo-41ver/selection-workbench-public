import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';
const html=fs.readFileSync(new URL('../web/index.html',import.meta.url),'utf8');

test('form submits the matching endpoint, model and key, then clears the key',()=>{
 const values={'category':'钢笔','amazon':'20','quantity':'200','worker-count':'4','new-same-limit':'5',
  'bestsellers-url':'','api-key':'TEST_ONLY_CUSTOM_KEY','api-base-url':'http://127.0.0.1:8080/v1','api-model':'test-model'};
 const nodes=Object.fromEntries(Object.entries(values).map(([k,value])=>[k,{value}]));
 let callback,submitted;
 nodes['task-form']={addEventListener:(_event,fn)=>callback=fn};
 const line=html.split('\n').find(s=>s.startsWith("$('task-form').addEventListener('submit'"));
 const context=vm.createContext({$:id=>nodes[id],action:(endpoint,body)=>submitted={endpoint,body}});
 vm.runInContext(line,context);callback({preventDefault(){}});
 assert.equal(submitted.endpoint,'/api/generate');
 assert.equal(submitted.body.api_base_url,values['api-base-url']);
 assert.equal(submitted.body.api_model,'test-model');
 assert.equal(submitted.body.api_key,'TEST_ONLY_CUSTOM_KEY');
 assert.equal(nodes['api-key'].value,'');
});

test('generation retry retains the user-selected provider',()=>{
 let callback,submitted;
 const nodes={'retry-generation':{addEventListener:(_event,fn)=>callback=fn},
  'retry-listing-url':{value:''},'bestsellers-url':{value:''},'api-key':{value:'TEST_ONLY_KEY'},
  'api-base-url':{value:'http://localhost:8080/v1'},'api-model':{value:'test-model'}};
 const context=vm.createContext({$:id=>nodes[id],lastStatus:{active:false,scope:{category:'钢笔',
  amazon_count:20,per_keyword:200,worker_count:4}},action:(endpoint,body)=>submitted=body});
 vm.runInContext(html.split('\n').find(s=>s.startsWith("$('retry-generation').addEventListener")),context);
 callback();assert.equal(submitted.api_base_url,'http://localhost:8080/v1');
 assert.equal(submitted.api_model,'test-model');assert.equal(nodes['api-key'].value,'');
});
