// A navigation replaces the JavaScript context. Re-read the resulting document;
// never reload a challenge or turn unrelated failures into navigation retries.
export async function readAmazonDocument(page,read,{attempts=3,sleep=ms=>page.waitForTimeout(ms)}={}) {
  for(let attempt=0;attempt<attempts;attempt++) {
    try {return await read();}
    catch(error) {
      const navigating=/Execution context was destroyed|Cannot find context with specified id|Execution context is not available in detached frame/i.test(error.message || '');
      if(!navigating || page.isClosed?.() || attempt===attempts-1)throw error;
      try {await page.waitForLoadState('domcontentloaded',{timeout:3000});}
      catch(waitError) {if(waitError.name!=='TimeoutError')throw waitError;}
      await sleep(200);
    }
  }
}
