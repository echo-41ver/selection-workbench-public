// Each official ranking page is a durable unit. Page 2 cannot erase page 1.
function validatePage(rows,page,target) {
  const start=(page-1)*50+1,end=Math.min(page*50,target);
  if(!Array.isArray(rows)) throw new Error('Amazon ranking records missing');
  const sorted=[...rows].sort((a,b)=>a.rank-b.rank);
  if(sorted.length!==end-start+1 || sorted.some((r,i)=>r.rank!==start+i
      || !/^[A-Z0-9]{10}$/.test(r.asin || '') || !r.title?.trim() || !r.product_url || !r.main_image_url)
      || new Set(sorted.map(r=>r.asin)).size!==sorted.length)
    throw new Error(`Amazon ranking page ${page} incomplete: expected ranks ${start}-${end}`);
  return sorted;
}

export async function collectRankedListing({scope,checkpoint,loadPage,save,onProgress=async()=>{},
  maxAttempts=3,sleep=ms=>new Promise(resolve=>setTimeout(resolve,ms))}) {
  if(checkpoint && (checkpoint.schema_version!=='amazon-ranking-pages-v1' || JSON.stringify(checkpoint.scope)!==JSON.stringify(scope)))
    throw new Error('Amazon ranking checkpoint scope mismatch; use --fresh for a new observation');
  const state=checkpoint || {schema_version:'amazon-ranking-pages-v1',scope,pages:{},errors:{}};
  const records=()=>Object.values(state.pages).flatMap(p=>p.records).sort((a,b)=>a.rank-b.rank);
  const persist=async status=>{
    state.status=status;state.updated_at=new Date().toISOString();await save(state);await onProgress({...state,records:records()});
  };
  for(let number=1;number<=Math.ceil(scope.target_count/50);number++) {
    if(state.pages[number]) {validatePage(state.pages[number].records,number,scope.target_count);continue;}
    const url=new URL(scope.source_url);
    if(number===2) url.searchParams.set('pg','2');
    else url.searchParams.delete('pg');
    for(let attempt=1;attempt<=maxAttempts;attempt++) {
      try {
        const rows=validatePage(await loadPage(url.href,number,attempt),number,scope.target_count);
        const prior=new Set(records().map(r=>r.asin));
        if(rows.some(r=>prior.has(r.asin))) throw new Error('Duplicate ASIN across Amazon ranking pages');
        state.pages[number]={records:rows,observed_at:new Date().toISOString()};
        delete state.errors[number];
        await persist('LISTING_PARTIAL');break;
      } catch(error) {
        state.errors[number]={attempt,message:String(error.message).slice(0,500),retryable:error.retryable!==false};
        await persist('LISTING_FAILED');
        if(error.retryable===false || attempt===maxAttempts) throw error;
        await sleep(1000*attempt);
      }
    }
  }
  if(new Set(records().map(r=>r.asin)).size!==scope.target_count) throw new Error('Invalid combined Amazon rank coverage');
  await persist('LISTING_COMPLETE');
  return records();
}
