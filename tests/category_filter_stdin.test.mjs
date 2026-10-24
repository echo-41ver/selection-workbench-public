import test from 'node:test';
import assert from 'node:assert/strict';
import {spawnSync} from 'node:child_process';
import {createHash} from 'node:crypto';
import {fileURLToPath} from 'node:url';

const entry = new URL('../scripts/filter_category_products.mjs', import.meta.url);
const category = {category_name_zh:'浴帘',temu:{include_terms:['浴帘'],
  subject_filter:{mode:'TARGET_BODY_ONLY',reject_mixed_subjects:true,exclude_subject_terms:['浴缸']}}};
const rows = [{goods_id:'CONTROL-1',title:'印花浴帘 浴缸隔断',
  provider_evidence:{goodsName:'中文・日本語・한국어・🛁浴缸',nested:{description:'原始完整字段'}}}];
const payload = value => Buffer.from(JSON.stringify(value));
// Use an actual Readable's individual Buffer chunks in a child importing the
// unchanged entrypoint. Object mode prevents OS coalescing from hiding a split.
const harness = `import {Readable} from 'node:stream';
import {readFileSync} from 'node:fs';
const chunks=JSON.parse(readFileSync(0,'utf8')).map(v=>Buffer.from(v,'base64'));
Object.defineProperty(process,'stdin',{value:Readable.from(chunks)});
await import(${JSON.stringify(entry.href)});`;
function run(chunks, actualPipe=false) {
  const child = spawnSync(process.execPath, actualPipe ? [fileURLToPath(entry)] :
    ['--input-type=module','--eval',harness], {
      input:actualPipe ? Buffer.concat(chunks) : JSON.stringify(chunks.map(c=>c.toString('base64'))),
      encoding:'utf8',maxBuffer:8*1024*1024,timeout:10000});
  assert.equal(child.status,0,child.stderr || child.error?.message);
  return JSON.parse(child.stdout);
}
function originalFields(result, originals) {
  const output = [...result.accepted,...result.excluded,...result.unclassified];
  for (const row of originals) {
    const actual = output.find(r=>r.goods_id===row.goods_id);
    assert.ok(actual,'every original product remains represented');
    for (const key of Object.keys(row)) assert.deepEqual(actual[key],row[key],key+' must retain exact Unicode');
  }
}
for (const split of [1,2]) test(`split inside 缸 byte ${split} preserves exclusion and original fields`,()=>{
  const bytes=payload({category,rows});
  const titleStart=bytes.indexOf(Buffer.from(rows[0].title));
  const cut=titleStart+Buffer.byteLength('印花浴帘 浴')+split;
  const whole=run([bytes]);
  const chunked=run([bytes.subarray(0,cut),bytes.subarray(cut)]);
  assert.deepEqual(chunked,whole);
  assert.equal(chunked.excluded[0].goods_id,'CONTROL-1');
  assert.equal(chunked.excluded[0].category_relevance.reason,'OTHER_SOLD_SUBJECT');
  originalFields(chunked,rows);
});

test('single-byte chunks preserve CJK, emoji and nested provider evidence',()=>{
  const bytes=payload({category,rows});
  const output=run([...bytes].map(value=>Buffer.from([value])));
  assert.deepEqual(output,run([bytes]));
  originalFields(output,rows);
});

test('large streamed provider input and the real OS pipe preserve every original field',()=>{
  const large=Array.from({length:400},(_,i)=>({goods_id:'LARGE-'+i,
    title:i%2?'印花浴帘 浴缸隔断':'透明浴帘',
    provider_evidence:{goodsName:'中文・日本語・한국어・🛁'.repeat(15),nested:{index:i,text:'原始完整字段'}}}));
  const bytes=payload({category,rows:large});
  assert.ok(bytes.length>200000);
  const chunks=[];for(let i=0;i<bytes.length;i+=1537)chunks.push(bytes.subarray(i,i+1537));
  const whole=run([bytes]);
  const fingerprint=value=>createHash('sha256').update(JSON.stringify(value)).digest('hex');
  for(const actualPipe of [false,true]) {
    const result=actualPipe ? run([bytes],true) : run(chunks);
    assert.equal(fingerprint(result),fingerprint(whole));
    assert.equal(result.accepted.length,200);assert.equal(result.excluded.length,200);
    originalFields(result,large);
  }
});
