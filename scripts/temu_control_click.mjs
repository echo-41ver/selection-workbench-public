// Only dispatch a normal pointer click after viewport hit-testing the control.
export function controlClickPoint(element) {
  const label=e=>/^\s*(See more|查看更多)\s*$/i.test(e?.textContent||'');
  if(!element?.isConnected||!label(element))return {ready:false,reason:'STALE_CONTROL'};
  let target=element;
  for(let e=element,i=0;e&&i<5;e=e.parentElement,i++){
    if(!label(e))break;
    if(e.matches('button,a,[role="button"]')){target=e;break;}
    if(getComputedStyle(e).cursor==='pointer')target=e;
  }
  target.scrollIntoView({block:'center',inline:'nearest',behavior:'instant'});
  const r=target.getBoundingClientRect();
  const x=Math.round((Math.max(0,r.left)+Math.min(innerWidth,r.right))/2);
  const y=Math.round((Math.max(0,r.top)+Math.min(innerHeight,r.bottom))/2);
  if(target.disabled||r.width<=2||r.height<=2||x<0||y<0||x>=innerWidth||y>=innerHeight)
    return {ready:false,reason:'NOT_INTERACTABLE'};
  const top=document.elementFromPoint(x,y);
  if(!top||!(top===target||target.contains(top)))return {ready:false,reason:'OBSCURED'};
  return {ready:true,x,y};
}

export async function clickVisibleControl({element,execute,wd,sleep}) {
  const point=await execute(`return (${controlClickPoint.toString()})(arguments[0]);`,[element]);
  if(!point?.ready)throw new Error(`SEE_MORE_${point?.reason||'UNAVAILABLE'}`);
  // Re-check after the scroll settles. Never reuse coordinates from a prior attempt.
  await sleep(200);
  const stable=await execute(`return (${controlClickPoint.toString()})(arguments[0]);`,[element]);
  if(!stable?.ready)throw new Error(`SEE_MORE_${stable?.reason||'UNAVAILABLE'}`);
  try {
    await wd('POST','/actions',{actions:[{type:'pointer',id:'temu-see-more-mouse',
      parameters:{pointerType:'mouse'},actions:[
        {type:'pointerMove',duration:160,origin:'viewport',x:stable.x,y:stable.y},
        {type:'pause',duration:100},{type:'pointerDown',button:0},
        {type:'pause',duration:100},{type:'pointerUp',button:0}]}]});
  }finally{await wd('DELETE','/actions').catch(()=>{});}
  return {method:'viewport_pointer',x:stable.x,y:stable.y};
}

export function expansionObserved(before,after) {
  const known=new Set(before.visibleGoodsIds||[]);
  return (after.visibleGoodsIds||[]).some(id=>!known.has(id))
    || Number(after.visibleGoodsCount)>Number(before.visibleGoodsCount);
}
