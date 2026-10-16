import {amazonDetailBlock} from './amazon_detail_queue.mjs';
import {readAmazonDocument} from './amazon_page_read.mjs';

// Refresh only ordinary loading/transport errors. Verification is not a load error.
export async function openAmazonDetail(page,url,{inspect,onResponse=()=>{},sleep=ms=>page.waitForTimeout(ms),
    readySelector='#productTitle',hasContent=evidence=>evidence.has_product_title}) {
  let lastError;
  const readEvidence=()=>readAmazonDocument(page,inspect,{sleep});
  for(let load=0;load<2;load++) {
    let response=null,loadError=null;
    try {
      response=load && page.url()===url
        ? await page.reload({waitUntil:'domcontentloaded',timeout:30000})
        : await page.goto(url,{waitUntil:'domcontentloaded',timeout:30000});
      onResponse(response);
    } catch(error) {loadError=error;}
    let evidence=await readEvidence();
    let block=amazonDetailBlock(evidence);
    if(block) throw Object.assign(new Error(block.message),block);
    if(evidence.continue_shopping) {
      // This is a normal visible navigation button, with no CAPTCHA input.
      // Click once; never submit hidden fields or loop on a real challenge.
      [response]=await Promise.all([
        page.waitForNavigation({waitUntil:'domcontentloaded',timeout:30000}),
        page.getByRole('button',{name:'Continue shopping',exact:true}).click({timeout:10000}),
      ]);
      onResponse(response);
      evidence=await readEvidence();
      block=amazonDetailBlock(evidence);
      if(block) throw Object.assign(new Error(block.message),block);
      if(evidence.continue_shopping) throw new Error('Amazon继续购物入口未完成，保留队列等待重试');
      // Amazon may send the ordinary button to its home page. Resume the
      // requested product in this same context, preserving the established session.
      if(!hasContent(evidence)) {
        response=await page.goto(url,{waitUntil:'domcontentloaded',timeout:30000});
        onResponse(response);
        evidence=await readEvidence();
        block=amazonDetailBlock(evidence);
        if(block) throw Object.assign(new Error(block.message),block);
        if(evidence.continue_shopping) throw new Error('Amazon再次显示继续购物入口，保留队列等待重试');
      }
      loadError=null;
    }
    try {
      if(loadError) throw loadError;
      if(!response || response.status()>=400) throw new Error(`HTTP ${response?.status() ?? 'unknown'}`);
      await page.locator(readySelector).first().waitFor({state:'attached',timeout:15000});
      return {response,page_loads:load+1};
    } catch(error) {
      lastError=error;
      // A challenge may appear while waiting for the title, not just at navigation.
      const after=amazonDetailBlock(await readEvidence());
      if(after) throw Object.assign(new Error(after.message),after);
      const transient=/timeout|timed.out|Execution context was destroyed|Cannot find context with specified id|ERR_(?:CONNECTION|NETWORK|INTERNET|NAME_NOT_RESOLVED)|HTTP (?:unknown|5\d\d)/i.test(error.message);
      if(!transient || load===1) throw error;
      await sleep(1000);
    }
  }
  throw lastError;
}

export const openAmazonRanking=(page,url,options)=>openAmazonDetail(page,url,
  {...options,readySelector:'.zg-bdg-ctr',hasContent:evidence=>evidence.has_rank_badges});

// Relaunch only a disconnected browser, never because a website denied access.
export function recoverableAmazonBrowser(launch,contextOptions,maxLaunches=3) {
  let browser=null,context=null,starting=null,launches=0;
  const getContext=async()=>{
    if(browser?.isConnected() && context) return context;
    if(starting) return starting;
    starting=(async()=>{
      if(launches>=maxLaunches) throw new Error('Amazon浏览器连续启动失败，已保存断点');
      launches++;
      context=null;
      browser=await launch();
      try {context=await browser.newContext(contextOptions);}
      catch(error){await browser.close().catch(()=>{});browser=null;throw error;}
      return context;
    })();
    try{return await starting;}finally{starting=null;}
  };
  return {getContext,close:async()=>{if(starting)await starting.catch(()=>{});if(browser)await browser.close();}};
}
