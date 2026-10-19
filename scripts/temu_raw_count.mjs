// Count frontend card positions before goods-ID deduplication or category gates.
// Repeated scans of one list do not create additional collection work.
export const RAW_COUNT_VERSION='frontend-card-positions-v1';
export function createRawCounter(limit, saved={}) {
 let count=saved.version===RAW_COUNT_VERSION&&Number.isSafeInteger(saved.count)?Math.min(limit,saved.count):0;
 let previous=saved.version===RAW_COUNT_VERSION&&Array.isArray(saved.window_ids)?saved.window_ids:[],
     documentId=saved.document_id??null,offset=Number.isSafeInteger(saved.offset)?saved.offset:0;
 return {
  get count(){return count;},
  snapshot(){return {version:RAW_COUNT_VERSION,count,document_id:documentId,offset,window_ids:previous};},
  observe(cards,documentToken,loadRequested=false){
   const ids=cards.map(row=>String(row.goods_id));
   if(!ids.length)return {count,allowed:new Set()};
   if(documentId!==documentToken){documentId=documentToken;previous=[];offset=0;}
   const same=ids.length===previous.length&&ids.every((id,i)=>id===previous[i]);
   const append=ids.length>=previous.length&&previous.every((id,i)=>id===ids[i]);
   if(previous.length&&!same&&!append){
    let overlap=0;
    for(let n=Math.min(previous.length,ids.length);n>=Math.min(3,previous.length,ids.length);n--)
     if(previous.slice(-n).every((id,i)=>id===ids[i])){overlap=n;break;}
    if(overlap)offset+=previous.length-overlap;
    else if(loadRequested)offset+=previous.length;
    else offset=0; // A refresh/re-render is not another page.
   }
   previous=ids;
   count=Math.max(count,Math.min(limit,offset+ids.length));
   return {count,allowed:new Set(ids.filter((_,i)=>offset+i<limit)),loadConsumed:!same};
  },
 };
}
