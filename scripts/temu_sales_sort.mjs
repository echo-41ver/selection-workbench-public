// Semantic controls only: no guessed URL sort parameters or fixed coordinates.
// UI_CONFIRMED means the page exposes a selected sales control, not that its
// advertised order or displayed sales quantities have been independently proved.
import fs from 'node:fs/promises';
import path from 'node:path';
import {atomicSnapshot} from './snapshot_atomic.mjs';

export const confirmedSalesEvidence = (value, query, sort='sales') => value?.requested===sort
  && value.state==='UI_CONFIRMED' && String(value.query || '').trim().toLowerCase()===query.trim().toLowerCase();

export async function prepareSalesCheckpoint(directory, query, sort='sales') {
  const names=['products.jsonl','pending_products.jsonl','progress.json'];
  const raw={};
  for (const name of names) raw[name]=await fs.readFile(path.join(directory,name),'utf8')
    .catch(error=>{if(error.code==='ENOENT') return null;throw error;});
  if (names.every(name=>raw[name]===null)) return null;
  const rows=name=>(raw[name] || '').split(/\r?\n/).filter(Boolean).map(line=>JSON.parse(line));
  const products=rows('products.jsonl'), pending=rows('pending_products.jsonl');
  const progress=JSON.parse(raw['progress.json'] || '{}');
  const keep=row=>confirmedSalesEvidence(row.search_sort,query,sort);
  const retained=products.filter(keep), retainedPending=pending.filter(keep);
  if (retained.length===products.length && retainedPending.length===pending.length
      && confirmedSalesEvidence(progress.sales_sort,query,sort)) return null;
  // Copy all original bytes before replacing any live snapshot. Archives are
  // deeper than the pipeline's worker/query/products.jsonl ingestion glob.
  const archive=await fs.mkdtemp(path.join(directory,'sales-sort-archive-'));
  for (const name of names) if(raw[name]!==null) await fs.writeFile(path.join(archive,name),raw[name],'utf8');
  const atomic=async(name,text)=>{
    await atomicSnapshot(path.join(directory,name),text);
  };
  for (const [name,values] of [['products.jsonl',retained],['pending_products.jsonl',retainedPending]]) {
    await atomic(name,values.map(row=>JSON.stringify(row)).join('\n')+(values.length?'\n':''));
  }
  await atomic('progress.json',JSON.stringify({status:'SALES_SORT_RECOLLECTION_REQUIRED',search_query:query,
    sales_sort:{requested:sort,state:'PENDING',query},unique_goods:retained.length,
    round:0,see_more_clicks:0,consecutive_no_new_rounds:0,keyword_complete:false,
    sales_sort_archive:archive},null,2));
  return {archive,retained_goods:retained.length,removed_goods:products.length-retained.length};
}

export function inspectSalesSort(sort='sales') {
  const sales = sort==='recent' ? /^(?:most recent|newest|new arrivals|新品上架|最近上架|最新上架|最新到货|最新到貨|最新商品|上新时间|上新時間)$/i : /^(?:top sales|sales(?::|\s*-)?\s*high to low|(?:销量|銷量)(?:优先|優先|最高|从高到低|從高到低|由高到低|降序)?)$/i;
  const trigger = /^(?:sort by\s*[:：]?\s*(?:default|relevance|recommended|top sales|most recent)?|排序(?:方式)?\s*[:：]?\s*(?:默认|默認|综合|綜合|推荐|推薦|(?:销量|銷量)(?:优先|優先|最高|从高到低|從高到低|由高到低|降序)?|新品上架|最近上架|最新上架|最新到货|最新到貨|最新商品|上新时间|上新時間)?)$/i;
  const selectedTrigger = sort==='recent' ? /^(?:sort by\s*[:：]?\s*most recent|排序(?:方式)?\s*[:：]?\s*(?:新品上架|最近上架|最新上架|最新到货|最新到貨|最新商品|上新时间|上新時間))$/i : /^(?:sort by\s*[:：]?\s*top sales|排序(?:方式)?\s*[:：]?\s*(?:销量|銷量)(?:优先|優先|最高|从高到低|從高到低|由高到低|降序)?)$/i;
  const text = e => {
    const raw=String(e.getAttribute('aria-label') || e.innerText || e.textContent || '').trim();
    // TEMU repeats the trigger in an aria-hidden sizing span and its label.
    // Collapse identical lines only; different choices must remain ambiguous.
    const lines=raw.split(/\r?\n/).map(s=>s.trim().replace(/\s+/g,' ')).filter(Boolean);
    return lines.length>1 && lines.every(s=>s===lines[0]) ? lines[0] : raw.replace(/\s+/g,' ');
  };
  const visible = e => {
    const r=e.getBoundingClientRect(), s=getComputedStyle(e);
    return r.width>2 && r.height>2 && s.display!=='none' && s.visibility!=='hidden'
      && !e.closest('[aria-hidden="true"],[hidden]') && !e.disabled;
  };
  const product = e => Boolean(e.closest('a[href*="goods_id="],a[href*="-g-"],article'));
  const nodes = [...document.querySelectorAll('button,[role="button"],[role="combobox"],[aria-haspopup],select,[role="option"],[role="menuitemradio"],[role="radio"],label')]
    .filter(e => visible(e) && !product(e));
  const triggers = nodes.filter(e => trigger.test(text(e)) ||
    ((e.getAttribute('role')==='combobox' || e.hasAttribute('aria-haspopup')) && /^(default|relevance|recommended|默认|默認|综合|綜合|推荐|推薦)$/i.test(text(e))));
  const options = nodes.filter(e => sales.test(text(e)));
  for (const e of nodes.filter(e=>e.tagName==='SELECT')) {
    for (const option of e.options || []) if (!option.disabled && sales.test(text(option))) options.push(option);
  }
  const selected = [];
  for (const e of nodes) {
    if (e.tagName==='SELECT') {
      const option=e.selectedOptions?.[0];
      if (option && sales.test(text(option))) selected.push({label:text(option), by:'native_selected_option'});
    }
    if (sales.test(text(e)) && (e.getAttribute('aria-selected')==='true' || e.getAttribute('aria-checked')==='true'
        || e.getAttribute('data-state')==='checked')) selected.push({label:text(e), by:'explicit_selected_state'});
    if (selectedTrigger.test(text(e))) {
      selected.push({label:text(e), by:'sort_trigger_label'});
    }
    if ((e.getAttribute('role')==='combobox' || e.hasAttribute('aria-haspopup')) && sales.test(text(e))) {
      selected.push({label:text(e), by:'combobox_value'});
    }
  }
  const ids=[...document.querySelectorAll('a[href]')].map(a =>
    (a.href.match(/[?&](?:goods_id|goodsId)=(\d+)(?:[&#]|$)/i) || a.href.match(/-g-(\d+)\.html/i) || [])[1]).filter(Boolean);
  return {url:location.href, selected_label:selected[0]?.label || null, selected_by:selected[0]?.by || null,
    menu:triggers.length===1 ? triggers[0] : null,
    menu_expanded:triggers.length===1 && triggers[0].getAttribute('aria-expanded')==='true',
    option:options.length===1 ? options[0] : null,
    ambiguous:triggers.length>1 || options.length>1,
    goods_ids:[...new Set(ids)], loading:[...document.querySelectorAll('[aria-busy="true"],[role="progressbar"]')].some(visible)};
}

export function isTargetSearch(state, query) {
  try {
    const u=new URL(state.url);
    return /(^|\.)temu\.com$/i.test(u.hostname) && u.pathname==='/search_result.html'
      && u.searchParams.get('search_key')?.trim().toLowerCase()===query.trim().toLowerCase();
  } catch { return false; }
}

export function confirmsSalesSort(state, query) {
  return isTargetSearch(state, query) && Boolean(state.selected_label && state.selected_by)
    && !state.ambiguous && !state.loading && state.goods_ids?.length>0;
}

export async function clickSalesSortControl({element, execute, click, sleep}) {
  // A second pass starts at the bottom of the previous list. Position the
  // actual semantic control before WebDriver hit-testing; never force a click.
  await execute(`const e=arguments[0];if(!e?.isConnected)throw new Error('SORT_CONTROL_DETACHED');
    e.scrollIntoView({block:'center',inline:'nearest',behavior:'instant'});return true;`, [element]);
  await sleep(250);
  return click(element);
}

export async function selectSalesSort({inspect, click, guard, sleep, query, sort='sales', maxChecks=30,
  maxActionAttempts=3, onRetry=async()=>{}, onAction=async()=>{}}) {
  let menuAttempts=0, optionAttempts=0, previous=null, stable=0, nextActionCheck=0;
  const limit=Math.max(1,Math.min(5,Math.floor(Number(maxActionAttempts)||3)));
  for (let check=0; check<maxChecks; check++) {
    // Existing CAPTCHA/login/stop guards run before each interaction.
    await guard();
    let state;
    try { state=await inspect(); }
    catch {
      await onRetry({phase:'inspect',reason:'页面正在变化，重新定位排序控件',check:check+1});
      await sleep(500);continue;
    }
    if (confirmsSalesSort(state, query)) {
      const fingerprint=JSON.stringify([state.selected_label, state.goods_ids]);
      stable=fingerprint===previous ? stable+1 : 1;
      previous=fingerprint;
      if (stable>=3) return {requested:sort, state:'UI_CONFIRMED', label:state.selected_label,
        confirmed_by:state.selected_by, observed_at:new Date().toISOString(), query,
        menu_attempts:menuAttempts,option_attempts:optionAttempts};
    } else { stable=0; previous=null; }
    if (check>=nextActionCheck && isTargetSearch(state, query) && !state.selected_label && !state.ambiguous && !state.loading) {
      const action=state.option && optionAttempts<limit ? 'option'
        : !state.option && state.menu && state.menu_expanded!==true && menuAttempts<limit ? 'menu' : null;
      if (action) {
        const attempt=action==='option' ? ++optionAttempts : ++menuAttempts;
        await onAction({phase:action,attempt,requested:sort,visible_goods:state.goods_ids?.length||0,
          loading:state.loading===true});
        if (attempt>1) await onRetry({phase:action,attempt,max_attempts:limit,reason:'上次点击尚未确认生效，重新定位后重试'});
        try { await click(state[action]); }
        catch (error) {
          const message=String(error?.message||error);
          const click_error=/stale element|detached/i.test(message)?'DETACHED'
            :/intercept|obscur/i.test(message)?'OBSCURED'
            :/interact|scroll|viewport/i.test(message)?'NOT_INTERACTABLE':'CLICK_FAILED';
          await onRetry({phase:action,attempt,max_attempts:limit,click_error,reason:'控件变化或暂时无法点击，将重新定位'});
        }
        // Do not keep stale element handles or click on every poll. Re-read
        // the selected state before retrying an apparently unsuccessful action.
        nextActionCheck=check+2;
      }
    }
    await sleep(500);
  }
  return {requested:sort, state:'UNCONFIRMED', query,
    menu_attempts:menuAttempts,option_attempts:optionAttempts,
    reason:'自动重新定位和点击重试后，仍未确认目标排序及稳定商品列表；保留断点，不采集普通排序商品'};
}
