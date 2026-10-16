import {isTargetSearch} from './temu_sales_sort.mjs';

/** Accept only an explicit warehouse badge in this product's visible card. */
export function hasLocalWarehouseLabel(row) {
  if (row?.source !== 'VISIBLE_TEMU_FRONTEND' || typeof row.card_text !== 'string') return false;
  return row.card_text.split(/\r?\n/).some(line =>
    /^(?:Local\s+Warehouse|本地[仓倉](?:[库庫])?)$/iu.test(line.replace(/[\u200B\uFEFF]/g, '').trim()));
}

// Serialized into the real search document by WebDriver. Product badges are
// evidence for a card, never controls for changing the search scope.
export function inspectLocalWarehouse() {
  const label=/^(?:Local\s+Warehouse|本地[仓倉](?:[库庫])?)$/iu;
  const text=e=>{const lines=String(e.getAttribute('aria-label')||e.innerText||e.textContent||'').trim().split(/\r?\n/).map(s=>s.trim().replace(/\s+/g,' ')).filter(Boolean);return lines.every(s=>s===lines[0])?lines[0]||'':lines.join(' ');};
  const visible=e=>{const r=e.getBoundingClientRect(),s=getComputedStyle(e);return r.width>2&&r.height>2&&s.display!=='none'&&s.visibility!=='hidden'&&!e.closest('[hidden],[aria-hidden="true"]')&&!e.disabled;};
  const product=e=>Boolean(e.closest('a[href*="goods_id="],a[href*="-g-"],article'));
  const nodes=[...document.querySelectorAll('button,label,[role="button"],[role="checkbox"],[role="radio"],input[type="checkbox"],span,div')].filter(e=>visible(e)&&!product(e));
  const candidates=[];
  const warehouseText=e=>label.test(text(e).replace(/^(?:ships? from|发货地|發貨地|物流配送)\s*(?:[（(]\d+[）)])?\s*[,，:]\s*/i,''));
  for(const node of nodes.filter(warehouseText)) {
    // Hidden native inputs still expose checked; click their visible label.
    let control=node.control||node.querySelector('input[type="checkbox"],[role="checkbox"]')||node;
    if(control===node && !['BUTTON','INPUT','LABEL'].includes(node.tagName) && !node.getAttribute('role')) {
      control=node.closest('label,button,[role="checkbox"],[role="button"]')||node;
    }
    const stateNode=control;
    const selected=stateNode.checked===true||['aria-checked','aria-pressed','aria-selected'].some(key=>stateNode.getAttribute(key)==='true')||stateNode.getAttribute('data-state')==='checked';
    const explicit=typeof stateNode.checked==='boolean'||['aria-checked','aria-pressed','aria-selected','data-state'].some(key=>stateNode.hasAttribute(key));
    const clickNode=visible(control)?control:node;
    if(!candidates.some(x=>x.control===control))candidates.push({control,element:clickNode,selected,explicit});
  }
  const controls=candidates.filter(x=>x.explicit);
  const chosen=controls.length===1?controls[0]:null;
  const buttons=nodes.filter(e=>['BUTTON','LABEL'].includes(e.tagName)||e.getAttribute('role')==='button');
  const shippingMenus=buttons.filter(e=>/^(?:ships? from|发货地|發貨地|发货地点|發貨地點|物流配送)(?:\s*[（(]\d+[）)])?$/i.test(text(e)));
  const menus=shippingMenus.length?shippingMenus:buttons.filter(e=>/^(?:filters?|筛选|篩選)(?:\s*\(\d+\))?$/i.test(text(e)));
  const dialogs=[...document.querySelectorAll('[role="dialog"]')].filter(visible);
  const applies=buttons.filter(e=>/^(?:apply|apply filters?|show\s+(?:[\d,.]+\+?\s+)?results?|done|应用|应用筛选|确认|确定|完成|查看结果|显示\s*(?:[\d,.]+\+?\s*个?)?结果)(?:\s*\(\d+\))?$/i.test(text(e))&&dialogs.some(d=>d.contains(e)));
  // TEMU keeps the real radio in its mounted filter group when the applied
  // dialog closes. Its checked state remains readable without reopening it.
  const savedChecked=[...document.querySelectorAll('[role="radio"][aria-checked="true"]')]
    .find(e=>warehouseText(e)&&e.closest('[role="group"][id^="filter-group-"]'));
  const filterGroup=(chosen?.control||savedChecked)?.closest('[role="group"][id^="filter-group-"]')?.id?.replace(/^filter-group-/,'')||null;
  const ids=[...document.querySelectorAll('a[href]')].map(a=>(a.href.match(/[?&](?:goods_id|goodsId)=(\d+)(?:[&#]|$)/i)||a.href.match(/-g-(\d+)\.html/i)||[])[1]).filter(Boolean);
  return {url:location.href,selected:chosen?.selected===true||Boolean(savedChecked),explicit:chosen?.explicit===true||Boolean(savedChecked),filter_group:filterGroup,
    option:chosen?.element||null,menu:menus.length===1?menus[0]:null,menu_expanded:menus.length===1&&menus[0].getAttribute('aria-expanded')==='true',
    apply:applies.length===1?applies[0]:null,dialog_open:dialogs.length>0,
    ambiguous:controls.length>1,goods_ids:[...new Set(ids)],
    loading:[...document.querySelectorAll('[aria-busy="true"],[role="progressbar"]')].some(visible)};
}

const filterItems=state=>{try{return new URL(state.url).searchParams.get('filter_items')||null;}catch{return null;}};
export const confirmsLocalWarehouse=(state,query,evidence={})=>isTargetSearch(state,query)
  &&((state.selected===true&&state.explicit===true)||(evidence.state==='UI_CONFIRMED'&&evidence.query===query
    &&evidence.filter_item&&(filterItems(state)||'').split('|').includes(evidence.filter_item)))
  &&!state.ambiguous&&!state.loading&&!state.dialog_open&&state.goods_ids?.length>0;

export async function selectLocalWarehouse({inspect,click,guard,sleep,query,maxChecks=60,onAction=async()=>{}}) {
  const attempts={option:0,menu:0,apply:0};let stable=0,previous=null,nextAction=0,checkedFilters=null;
  for(let check=0;check<maxChecks;check++) {
    await guard();
    let state;try{state=await inspect();}catch{await sleep(500);continue;}
    // The real checked radio supplies this identity; no URL filter value is
    // invented or injected. TEMU may unmount the radio after Show results.
    if(state.selected&&state.explicit&&state.filter_group&&isTargetSearch(state,query)) {
      checkedFilters=(filterItems(state)||'').split('|').find(item=>item.startsWith(state.filter_group+':'))||checkedFilters;
    }
    const evidence={state:'UI_CONFIRMED',query,filter_item:checkedFilters};
    if(confirmsLocalWarehouse(state,query,evidence)) {
      const key=JSON.stringify(state.goods_ids);stable=key===previous?stable+1:1;previous=key;
      if(stable>=3)return {state:'UI_CONFIRMED',query,label:'本地仓',filter_item:checkedFilters,
        confirmed_by:'explicit_checked_state_and_applied_results',observed_at:new Date().toISOString()};
    }else{stable=0;previous=null;}
    if(check>=nextAction&&isTargetSearch(state,query)&&!state.loading&&!state.ambiguous) {
      const applied=checkedFilters&&(filterItems(state)||'').split('|').includes(checkedFilters)&&!state.dialog_open;
      const action=!state.selected&&state.option?'option':state.selected&&state.apply?'apply':!applied&&!state.option&&state.menu&&!state.dialog_open&&!state.menu_expanded?'menu':null;
      if(action&&attempts[action]<3) {
        await guard();await onAction({action,attempt:++attempts[action]});
        try{await click(state[action]);}catch{/* Re-read the real checked state before retrying. */}
        nextAction=check+3;
      }
    }
    await sleep(500);
  }
  return {state:'UNCONFIRMED',query,reason:'未能确认搜索页已勾选本地仓；保留断点，停止采集未筛选商品'};
}
