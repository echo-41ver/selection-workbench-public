import test from 'node:test';
import assert from 'node:assert/strict';
import {createServer} from 'node:http';
import {chromium} from 'playwright';
import {detailComplete} from '../scripts/collection_checkpoint.mjs';
import {qualifyAmazonDetail} from '../scripts/amazon_detail_quality.mjs';
import {inspectAmazonImage} from '../scripts/amazon_public_image.mjs';
import {runAmazonDetailQueue} from '../scripts/amazon_detail_queue.mjs';
import {completeDetail} from './fixtures/amazon_detail.mjs';

const url='https://m.media-amazon.com/images/I/fixture._AC_SX1200_.jpg';
const candidate={url,source:'详情页动态主图'};
const card={asin:'B00FIXTURE',title:'Listing title',main_image_url:'https://m.media-amazon.com/images/I/listing.jpg'};

test('legacy strings, blank specs and evidence for a different image cannot declare completion',()=>{
  assert.equal(detailComplete({...card,detail_status:'详情页已采集',image_status:'详情页主图'}),false);
  assert.equal(detailComplete({...completeDetail(card),specifications:[]}),false);
  assert.equal(detailComplete({...completeDetail(card),main_image_url:url}),false);
  assert.equal(detailComplete({...completeDetail(card),specifications:[{label:'ASIN',value:'B00FIXTURE'}]}),false);
  assert.equal(detailComplete(completeDetail(card)),true);
});

test('actual HTTP bytes and Chromium decode reject HTML, corrupt bytes, tiny images; good image plus evidence completes',async()=>{
  const browser=await chromium.launch({headless:true});
  let server;
  try {
    const context=await browser.newContext();
    const page=await context.newPage();
    const png=async (width,height=width)=>Buffer.from(await page.evaluate(([w,h])=>{const c=document.createElement('canvas');c.width=w;c.height=h;return c.toDataURL().split(',')[1];},[width,height]),'base64');
    const large=await png(900),tiny=await png(32),narrow=await png(345,1460);
    await page.close();
    let mode='html';
    server=createServer((req,res)=>{
      res.writeHead(mode==='http'?503:200,{'content-type':mode==='html'?'text/html':'image/png'});
      res.end(mode==='good'?large:mode==='narrow'?narrow:mode==='tiny'?tiny:Buffer.from('<html>image unavailable</html>'));
    });
    await new Promise(resolve=>server.listen(0,'127.0.0.1',resolve));
    const local=`http://127.0.0.1:${server.address().port}/fixture`;
    const wrapped={newPage:()=>context.newPage(),request:{get:(_url,options)=>context.request.get(local,options)}};
    const inspect=value=>inspectAmazonImage(wrapped,value);
    const detail={title:'Actual detail title',specifications:[{label:'Voltage',value:'20 volts'}]};
    for(mode of ['html','corrupt','tiny','http']) {
      const row=await qualifyAmazonDetail(card,detail,[candidate],inspect);
      assert.equal(detailComplete(row),false,mode);
      assert.deepEqual(row.detail_quality.missing_fields,['verified_detail_image']);
      assert.equal(row.main_image_url,card.main_image_url);
      assert.equal(row.detail_error.retryable,true);
    }
    mode='narrow';
    const narrowRow=await qualifyAmazonDetail(card,detail,[candidate],inspect);
    assert.equal(detailComplete(narrowRow),true);
    assert.equal(narrowRow.detail_quality.image.width,345);
    assert.equal(narrowRow.detail_quality.image.height,1460);
    mode='good';
    const absentSpecs=await qualifyAmazonDetail(card,{title:'Actual detail title',specifications:[]},[candidate],inspect);
    assert.equal(detailComplete(absentSpecs),false);
    assert.deepEqual(absentSpecs.detail_quality.missing_fields,['specifications_or_features']);
    const absentTitle=await qualifyAmazonDetail(card,{specifications:detail.specifications},[candidate],inspect);
    assert.equal(detailComplete(absentTitle),false);
    assert.ok(absentTitle.detail_quality.missing_fields.includes('detail_title'));
    const good=await qualifyAmazonDetail(card,detail,[candidate],inspect);
    assert.equal(detailComplete(good),true);
    assert.equal(good.detail_quality.image.width,900);
    assert.equal(good.detail_quality.image.mime,'image/png');
    const preserved=await qualifyAmazonDetail(good,{title:null,specifications:[],brand:null},[candidate],async()=>assert.fail('Verified image should be reused'));
    assert.equal(detailComplete(preserved),true);
    assert.deepEqual(preserved.specifications,good.specifications);
    await assert.rejects(inspectAmazonImage(wrapped,'https://www.amazon.com/not-an-image.jpg'),/INVALID_AMAZON_IMAGE_URL/);
  } finally {
    if(server) await new Promise(resolve=>server.close(resolve));
    await browser.close();
  }
});

test('quality failure queues behind untouched ranks, and legacy success gets revalidated',async()=>{
  const legacy={...card,detail_status:'详情页已采集',image_status:'详情页主图',detail_image_attempts:1};
  const calls=[];
  const result=await runAmazonDetailQueue({records:[legacy,{...card,asin:'NEW',rank:2}],concurrency:1,maxAttempts:3,
    onCheckpoint:async()=>{},fetchDetail:async(row,attempt)=>{
      calls.push([row.asin,attempt]);
      if(row.asin==='NEW'&&attempt===1)return qualifyAmazonDetail(row,{title:'Detail'},[],async()=>{});
      return completeDetail(row);
    }});
  assert.deepEqual(calls,[['NEW',1],['B00FIXTURE',2],['NEW',2]]);
  assert.equal(result.queue.completed_count,2);
});
