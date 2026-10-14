#!/usr/bin/env node
import fs from 'node:fs/promises';
import {atomicSnapshot} from './snapshot_atomic.mjs';
import {createCategoryRelevance, COLLECTION_PROGRESS_VERSION, restoreNoNewRounds} from './category_relevance.mjs';
import {readInitialWindowHandles} from './browser_control_bootstrap.mjs';
import {parseProductCardMetrics} from './product_card_metrics.mjs';
import {sessionRecoveryRequest, captchaNeedsManualVerification} from './temu_session_recovery.mjs';
import {createBrowserControlClient} from './browser_control_request.mjs';
import {clickVisibleControl,expansionObserved} from './temu_control_click.mjs';
import { createOwnedLifecycle, topDocumentUrl } from './temu_bidi_lifecycle.mjs';
import { nonEssentialRequest, allowPageImages, blockImageRequest } from './temu_resource_policy.mjs';
import { networkEvidence, diagnosticLimiter, supersededByVerification, supersededBySuccess, collectionRequest, endpoint } from './temu_network_evidence.mjs';
import {observeHttpPageTransition} from './temu_http_transition.mjs';
import path from 'node:path';
import { classifyPageState as classify, didTryAgainProgress } from './temu_flower_recovery_policy.mjs';
import { bootstrapEntryPlan, isStableHomeSearchSample, decideSearchSubmission, isPostVerificationEmptySearchSample, isStableEmptySearchSample, isStableExistingSearchSample, sameSearchInputGeometry } from './temu_search_bootstrap_policy.mjs';
import { decideInitialProductsWait, DEFAULT_INITIAL_PRODUCTS_TIMEOUT_SECONDS, decideEmptyQueryResults } from './temu_initial_wait_policy.mjs';
import { inspectSearchReadiness } from './temu_search_readiness.mjs';
import { missingControlState, terminalCollectionStop, confirmedNoMoreResults, inspectSearchEnd } from './collection_checkpoint.mjs';
import { inspectSalesSort, confirmsSalesSort, selectSalesSort, prepareSalesCheckpoint, clickSalesSortControl } from './temu_sales_sort.mjs';

const sleep = (ms) => new Promise(r => setTimeout(r, ms));
const rand = (a, b) => Math.floor(a + Math.random() * (b - a + 1));
const now = () => new Date().toISOString();
const ROOT = process.cwd();
const argument = (name, fallback = null) => {
  const index = process.argv.indexOf(name);
  return index >= 0 ? process.argv[index + 1] : fallback;
};
const configPath = path.resolve(argument('--config', path.join(ROOT, 'config', 'categories', 'bath_towel_set.json')));
const AUTO_SESSION_RECOVERY = argument('--auto-session-recovery', 'true') === 'true';
const categoryConfig = JSON.parse(await fs.readFile(configPath, 'utf8'));
const QUERY = String(argument('--query', categoryConfig.temu.base_queries[0])).trim();
if (!QUERY) throw new Error('TEMU query must not be empty');
const SEARCH_SUBMISSION = argument('--search-submit', categoryConfig.temu.search_submit_method || 'icon');
if (!['enter', 'icon'].includes(SEARCH_SUBMISSION)) throw new Error('Unsupported TEMU search submission mode');
const SORT_REQUEST = argument('--sort', 'sales');
if (!['sales','recent'].includes(SORT_REQUEST)) throw new Error('Unsupported TEMU sort');
let salesSort = { requested: SORT_REQUEST, state: 'PENDING' };
let salesSortEstablished = false;
let noMoreResultsConfirmed = false;
let initialProductsPreparing = true;
// Local to this collector session; never restore from a previous checkpoint.
let scrollingCollectionStarted = false;
let humanVerificationActive = false;
const readSalesSort = () => execute(`return (${inspectSalesSort.toString()})(${JSON.stringify(SORT_REQUEST)});`, [], 'read_sort');
const SID_FILE = path.resolve(argument('--session-id-file', path.join(ROOT, 'tmp', 'flower_session_id.txt')));
const BIDI_FILE = path.resolve(argument('--bidi-url-file', path.join(ROOT, 'tmp', 'flower_bidi_url.txt')));
const OUT = path.resolve(argument('--output', path.join(ROOT, 'runs', categoryConfig.category_key, 'temu', `worker_${new Date().toISOString().replace(/[:.]/g, '-')}`)));
const PRODUCTS = path.join(OUT, 'products.jsonl');
const PROGRESS = path.join(OUT, 'progress.json');
const EVENTS = path.join(OUT, 'events.jsonl');
const IMAGE_BACKFILL_QUEUE = path.join(OUT, 'image_backfill_queue.jsonl');
const PENDING_PRODUCTS = path.join(OUT, 'pending_products.jsonl');
const UNCLASSIFIED_PRODUCTS = path.join(OUT, 'unclassified_products.jsonl');
const FILTERED_OFF_TOPIC = path.join(OUT, 'filtered_severe_off_topic.jsonl');
const NETWORK_USAGE = path.join(OUT, 'network_usage.json');
const WD = String(argument('--webdriver-url', 'http://127.0.0.1:4444')).replace(/\/$/, '');
const IDLE_REQUIRED = Number(argument('--idle-rounds', categoryConfig.temu.idle_complete_rounds || 10));
const MAX_PRODUCTS = Number(argument('--max-products', categoryConfig.temu.max_products_per_keyword || 500));
const SEE_MORE_MISSING_CHECKS = Math.max(2, Number(categoryConfig.temu.see_more_missing_checks) || 8);
const SEE_MORE_MISSING_WAIT_MS = Math.max(10000, Number(categoryConfig.temu.see_more_missing_timeout_seconds) * 1000 || 120000);
if (!Number.isSafeInteger(MAX_PRODUCTS) || MAX_PRODUCTS < 1) throw new Error('--max-products must be a positive integer');
const INITIAL_PRODUCTS_TIMEOUT_SECONDS = Number(argument('--initial-products-timeout-seconds',
  categoryConfig.temu.initial_products_timeout_seconds ?? DEFAULT_INITIAL_PRODUCTS_TIMEOUT_SECONDS));
if (!Number.isFinite(INITIAL_PRODUCTS_TIMEOUT_SECONDS) || INITIAL_PRODUCTS_TIMEOUT_SECONDS <= 0) {
  throw new Error('--initial-products-timeout-seconds must be a positive number');
}
const categoryRelevance = createCategoryRelevance(categoryConfig);
const {isSevereOffTopic} = categoryRelevance;
const TRY_AGAIN_STALL_LIMIT = 3;
const TEMU_EMPTY_SEARCH = 'https://www.temu.com/search_result.html';
const TEMU_HOME = 'https://www.temu.com/';
const TEMU_HOME_URL = /^https:\/\/(?:www\.)?temu\.com\/(?:[?#].*)?$/i;
const TEMU_EMPTY_SEARCH_URL = /^https:\/\/(?:www\.)?temu\.com\/search_result\.html(?:\?(?![^#]*search_key=[^&#]+)[^#]*)?(?:#.*)?$/i;
const SAFE_SOURCE = `https://www.temu.com/search_result.html?search_key=${encodeURIComponent(QUERY)}`;
const ENTRY_MODE = argument('--entry-mode', categoryConfig.temu.bootstrap_entry_by_worker?.[path.basename(path.dirname(SID_FILE))]
  || categoryConfig.temu.entry_mode || 'empty_search');
if (!['home_search', 'empty_search', 'direct_search'].includes(ENTRY_MODE)) throw new Error('Unsupported TEMU entry mode');
const SAFE_BOOTSTRAP = 'http://127.0.0.1:17892/';
const BOOTSTRAP_VALIDATE_ONLY = process.env.TEMU_BOOTSTRAP_VALIDATE_ONLY === '1';
const EKEY = 'element-6066-11e4-a52e-4f735466cecf';


await fs.mkdir(OUT, { recursive: true });
const sid = (await fs.readFile(SID_FILE, 'utf8')).trim();
if (!sid) throw new Error('empty WebDriver session id');
const bidiUrl = (await fs.readFile(BIDI_FILE, 'utf8').catch(() => '')).trim()
  || `ws://127.0.0.1:51105/session/${sid}`;

let browserControlCleanup=null;
const wd=createBrowserControlClient({base:WD,session:sid,onTimeout:async error=>{
  // Initial driver commands run before products are loaded. Merge progress only;
  // never replace a complete product checkpoint with uninitialized maps.
  const previous=await fs.readFile(PROGRESS,'utf8').then(JSON.parse).catch(()=>({}));
  const details={status:'STOPPED_BROWSER_COMMAND_TIMEOUT',search_query:QUERY,
    browser_preserved:true,proxy_preserved:true,keyword_complete:false,platform_complete:false,
    coverage_claim:null,completion_basis:null,completion_scope:null,
    stop_reason:'BROWSER_COMMAND_TIMEOUT',updated_at:now(),
    scroll_collection_started:scrollingCollectionStarted,
    collection_phase:scrollingCollectionStarted?'SCROLLING':'BEFORE_SCROLL',
    manual_action_required:true,automatic_retry:false,automatic_recovery_eligible:false,
    resume_requires_session_rotation:false,command_execution_unknown:true,
    command_method:error.method,command_route:error.route,command_label:error.commandLabel||null,finished_at:now(),
    message:'浏览器控制超时，连接保持；恢复后可继续'};
  await atomicSnapshot(PROGRESS,JSON.stringify({...previous,...details},null,2));
  await fs.appendFile(EVENTS,JSON.stringify({at:now(),type:details.status,...details})+'\n','utf8');
  // Remove this collector's owned intercepts and release paused requests before
  // exiting. The browser/session/proxy are not stopped or reopened.
  if(browserControlCleanup){
    let deadline;
    try{await Promise.race([browserControlCleanup(),new Promise(resolve=>{deadline=setTimeout(resolve,15000);})]);}
    catch{} // timeout status is durable even if the control channel cannot clean up
    finally{clearTimeout(deadline);}
  }
  process.exit(2);
}});
async function execute(script, args = [], commandLabel = 'execute_sync') { return wd('POST', '/execute/sync', { script, args }, {commandLabel}); }
async function atomic(file, text) {
  await atomicSnapshot(file,text);
}
async function appendEvent(type, data = {}) {
  await fs.appendFile(EVENTS, `${JSON.stringify({ at: now(), type, ...data })}\n`, 'utf8');
}

const handles = await readInitialWindowHandles(wd,{
  readCheckpoint:()=>fs.readFile(PROGRESS,'utf8').then(JSON.parse).catch(()=>({})),
  saveCheckpoint:row=>atomicSnapshot(PROGRESS,JSON.stringify(row,null,2)),appendEvent,now,
});
let chosen = null;
let blankBootstrapHandle = null;
let temuBootstrapHandle = null;
let localBootstrapHandle = null;
let minimalBootstrapHandle = null;
for (const handle of handles) {
  await wd('POST', '/window', { handle });
  const url = await wd('GET', '/url');
  if (/^https:\/\/(?:www\.)?temu\.com\/search/i.test(url) && new URL(url).searchParams.get('search_key')?.toLowerCase() === QUERY.toLowerCase()) {
    chosen = handle; break;
  }
  // AdsPower/FlowerBrowser may normalize a configured about:blank startup tab
  // to about:newtab. Both are local, network-free bootstrap pages and are safe
  // to accept before the BiDi image interception gate is installed.
  if (/^about:(?:blank|newtab)$/i.test(url)) blankBootstrapHandle = handle;
  if (/^https:\/\/(?:www\.)?temu\.com\//i.test(url)) temuBootstrapHandle = handle;
  if (url.startsWith(SAFE_BOOTSTRAP)) localBootstrapHandle = handle;
  if (/^https:\/\/example\.com\/?/i.test(url)) minimalBootstrapHandle = handle;
}
const bootstrapFromSafeTab = !chosen && handles.length === 1 && Boolean(localBootstrapHandle || blankBootstrapHandle || minimalBootstrapHandle || temuBootstrapHandle);
if (bootstrapFromSafeTab) {
  chosen = localBootstrapHandle || blankBootstrapHandle || minimalBootstrapHandle || temuBootstrapHandle;
  await wd('POST', '/window', { handle: chosen });
}
if (!chosen) throw new Error(`matching current Temu search tab for ${QUERY} not found; only one blank or Temu tab may be bootstrapped safely`);
let currentDocumentUrl = await wd('GET', '/url');

const products = new Map();
const pendingProducts = new Map();
const unclassifiedProducts = new Map();
const filteredSevereOffTopic = new Map();
let round = 0;
let consecutiveNoNewRounds = 0;
let clicks = 0;
let imageBlocked = 0;
let imageCancelRaces = 0;
let trafficSavingMode = false;
let nonEssentialBlocked = 0;
let bidiStatus = 'CONNECTING';
let httpBlock = null;
let startedAt = now();
let seeMoreBottomOffset = null;
let clicksSinceRecovery = IDLE_REQUIRED;
let finalStatus = null;
let consecutiveTryAgainErrors = 0;
let consecutiveTryAgainNoProgress = 0;
let missingSeeMore = null;
let failedSeeMoreClicks = 0;

function enforceConfiguredProductLimit() {
  if (products.size <= MAX_PRODUCTS) return 0;
  const ordered = [...products.entries()].sort(([, a], [, b]) =>
    Number(a.first_seen_ordinal || Number.MAX_SAFE_INTEGER) - Number(b.first_seen_ordinal || Number.MAX_SAFE_INTEGER));
  const overflow = ordered.slice(MAX_PRODUCTS);
  for (const [goodsId] of overflow) products.delete(goodsId);
  return overflow.length;
}

const emptyNetworkTotals = () => ({
  requests: 0, completed: 0, failed: 0, response_bytes_observed: 0,
  responses_without_size: 0,
});
const addNetworkTotals = (target, source = {}) => {
  for (const key of Object.keys(emptyNetworkTotals())) target[key] += Number(source[key]) || 0;
};
const previousNetwork = await fs.readFile(NETWORK_USAGE, 'utf8')
  .then(text => JSON.parse(text)).catch(() => null);
const networkBase = { totals: emptyNetworkTotals(), by_host: {}, by_resource_type: {} };
addNetworkTotals(networkBase.totals, previousNetwork?.totals);
for (const [host, values] of Object.entries(previousNetwork?.by_host || {})) {
  networkBase.by_host[host] = emptyNetworkTotals(); addNetworkTotals(networkBase.by_host[host], values);
}
for (const [type, values] of Object.entries(previousNetwork?.by_resource_type || {})) {
  networkBase.by_resource_type[type] = emptyNetworkTotals(); addNetworkTotals(networkBase.by_resource_type[type], values);
}
const networkCurrent = { totals: emptyNetworkTotals(), by_host: {}, by_resource_type: {} };
const networkRequests = new Map();
const networkBucket = (group, key) => group[key] ||= emptyNetworkTotals();
const addNetworkMetric = (info, key, amount = 1) => {
  networkCurrent.totals[key] += amount;
  networkBucket(networkCurrent.by_host, info.host)[key] += amount;
  networkBucket(networkCurrent.by_resource_type, info.type)[key] += amount;
};
const networkInfo = (request = {}) => {
  let host = 'invalid-host';
  try { host = new URL(String(request.url || '')).hostname.toLowerCase() || 'unknown-host'; } catch {}
  return { host, type: String(request.destination || 'other').toLowerCase().slice(0, 32) || 'other' };
};
const observedResponseBytes = (response = {}, params = {}) => {
  const total = Number(response.bytesReceived ?? params.bytesReceived ?? params.encodedDataLength);
  if (Number.isFinite(total) && total >= 0) return total;
  const body = Number(response.bodySize);
  const headers = Number(response.headersSize);
  if ((Number.isFinite(body) && body >= 0) || (Number.isFinite(headers) && headers >= 0)) {
    return Math.max(0, Number.isFinite(body) ? body : 0) + Math.max(0, Number.isFinite(headers) ? headers : 0);
  }
  return null;
};
const mergedNetworkGroup = (base = {}, current = {}) => {
  const result = {};
  for (const key of new Set([...Object.keys(base), ...Object.keys(current)])) {
    result[key] = emptyNetworkTotals();
    addNetworkTotals(result[key], base[key]); addNetworkTotals(result[key], current[key]);
  }
  return result;
};
function networkUsageSnapshot() {
  const totals = emptyNetworkTotals();
  addNetworkTotals(totals, networkBase.totals); addNetworkTotals(totals, networkCurrent.totals);
  return {
    schema_version: 'temu-browser-network-usage-v1',
    measurement: 'BROWSER_BIDI_RESPONSE_METADATA',
    billing_notice: 'Browser-observed response bytes are diagnostic and may differ from Decodo billable traffic.',
    updated_at: now(), current_process_started_at: startedAt,
    totals,
    by_resource_type: mergedNetworkGroup(networkBase.by_resource_type, networkCurrent.by_resource_type),
    by_host: mergedNetworkGroup(networkBase.by_host, networkCurrent.by_host),
    active_requests: networkRequests.size,
  };
}

const salesCheckpoint = await prepareSalesCheckpoint(OUT, QUERY, SORT_REQUEST);
if (salesCheckpoint) await appendEvent('SALES_SORT_CHECKPOINT_ARCHIVED',salesCheckpoint);
try {
  const previous = (await fs.readFile(PRODUCTS, 'utf8')).split(/\r?\n/).filter(Boolean).map(line => JSON.parse(line));
  for (const row of previous) if (row?.goods_id) {
    if (isSevereOffTopic(row)) filteredSevereOffTopic.set(String(row.goods_id), row);
    else if(categoryRelevance.strict && categoryRelevance.classify(row).state==='UNKNOWN') unclassifiedProducts.set(String(row.goods_id),row);
    else products.set(String(row.goods_id), row);
  }
  const pendingPrevious = (await fs.readFile(PENDING_PRODUCTS, 'utf8').catch(() => '')).split(/\r?\n/).filter(Boolean).map(line => JSON.parse(line));
  for (const row of pendingPrevious) if (row?.goods_id && !products.has(String(row.goods_id))) {
    if (isSevereOffTopic(row)) filteredSevereOffTopic.set(String(row.goods_id), row);
    else if(categoryRelevance.strict && categoryRelevance.classify(row).state==='UNKNOWN') unclassifiedProducts.set(String(row.goods_id),row);
    else pendingProducts.set(String(row.goods_id), row);
  }
  const unclassifiedPrevious=(await fs.readFile(UNCLASSIFIED_PRODUCTS,'utf8').catch(()=> '')).split(/\r?\n/).filter(Boolean).map(line=>JSON.parse(line));
  for(const row of unclassifiedPrevious) if(row?.goods_id && !products.has(String(row.goods_id))) {
    if(isSevereOffTopic(row))filteredSevereOffTopic.set(String(row.goods_id),row);
    else if(categoryRelevance.classify(row).state==='RELEVANT')pendingProducts.set(String(row.goods_id),row);
    else unclassifiedProducts.set(String(row.goods_id),row);
  }
  const filteredPrevious = (await fs.readFile(FILTERED_OFF_TOPIC, 'utf8').catch(() => '')).split(/\r?\n/).filter(Boolean).map(line => JSON.parse(line));
  for (const row of filteredPrevious) if (row?.goods_id) filteredSevereOffTopic.set(String(row.goods_id), row);
  const progress = JSON.parse(await fs.readFile(PROGRESS, 'utf8'));
  round = Number(progress.round) || 0;
  clicks = Number(progress.see_more_clicks) || 0;
  // v1 measured only the rows discovered immediately after a click. Rows that
  // arrived late were first seen while positioning for the next click, so ten
  // v1 "empty" clicks could still increase the global product set. Never carry
  // that invalid saturation counter into the corrected round-based logic.
  // A changed language/identity policy invalidates prior saturation evidence.
  consecutiveNoNewRounds = restoreNoNewRounds(progress, categoryRelevance);
  startedAt = progress.started_at || startedAt;
  seeMoreBottomOffset = Number(progress.see_more_bottom_offset_px) || null;
  clicksSinceRecovery = Number.isFinite(Number(progress.clicks_since_recovery))
    ? Number(progress.clicks_since_recovery) : IDLE_REQUIRED;
} catch {}
enforceConfiguredProductLimit();
async function writeSnapshot(status, extra = {}) {
  const phase = {scroll_collection_started: scrollingCollectionStarted,
    collection_phase: scrollingCollectionStarted ? 'SCROLLING' : 'BEFORE_SCROLL'};
  const sessionRecovery = sessionRecoveryRequest(status, {http_block: httpBlock, ...extra, ...phase}, AUTO_SESSION_RECOVERY);
  const manualCaptcha = captchaNeedsManualVerification(status, phase) ? {
    manual_action_required: true, automatic_retry: false, automatic_recovery_eligible: false,
    resume_requires_session_rotation: false, automatic_resume_pending: false,
  } : {};
  const rows = [...products.values()].sort((a, b) => Number(a.first_seen_ordinal) - Number(b.first_seen_ordinal));
  const missingImages = rows.filter(row => !row.main_image_url && !row.thumbnail_url && !(Array.isArray(row.all_image_urls) && row.all_image_urls.length));
  const pendingRows = [...pendingProducts.values()];
  const unclassifiedRows=[...unclassifiedProducts.values()];
  const relevanceCounts = categoryRelevance.summarize([...rows,...unclassifiedRows]);
  const filteredRows = [...filteredSevereOffTopic.values()];
  await atomic(PRODUCTS, rows.map(x => JSON.stringify(x)).join('\n') + (rows.length ? '\n' : ''));
  await atomic(PENDING_PRODUCTS, pendingRows.map(x => JSON.stringify(x)).join('\n') + (pendingRows.length ? '\n' : ''));
  await atomic(UNCLASSIFIED_PRODUCTS,unclassifiedRows.map(x=>JSON.stringify(x)).join('\n')+(unclassifiedRows.length?'\n':''));
  await atomic(FILTERED_OFF_TOPIC, filteredRows.map(x => JSON.stringify(x)).join('\n') + (filteredRows.length ? '\n' : ''));
  await atomic(IMAGE_BACKFILL_QUEUE, missingImages.map(row => JSON.stringify({
    goods_id: row.goods_id, title: row.title || null, product_url: row.product_url || null,
    first_seen_round: row.first_seen_round ?? null, first_seen_ordinal: row.first_seen_ordinal ?? null,
  })).join('\n') + (missingImages.length ? '\n' : ''));
  await atomic(NETWORK_USAGE, JSON.stringify(networkUsageSnapshot(), null, 2));
  await atomic(PROGRESS, JSON.stringify({
    schema_version: 'temu-flower-existing-v2', status, search_query: QUERY, sales_sort: salesSort,
    site: 'TEMU_US', currency: 'USD', source: 'VISIBLE_TEMU_FRONTEND',
    unique_goods: rows.length, round, see_more_clicks: clicks,
    stop_logic_version: COLLECTION_PROGRESS_VERSION,
    relevance_policy_version: categoryRelevance.version,
    relevance_policy_fingerprint: categoryRelevance.fingerprint,
    unique_relevant_goods: relevanceCounts.relevant,
    unique_unclassified_goods: relevanceCounts.unknown,
    unclassified_products_file: UNCLASSIFIED_PRODUCTS,
    category_subject_filter: categoryRelevance.strict ? 'TARGET_BODY_ONLY' : 'LEGACY_RECALL',
    unique_progress_goods: relevanceCounts.progress,
    filtered_severe_off_topic_goods: filteredRows.length,
    filtered_severe_off_topic_file: FILTERED_OFF_TOPIC,
    no_new_definition: 'NO_NEW_RELEVANT_OR_UNCLASSIFIED_GOODS_FOR_QUERY',
    consecutive_no_new_progress_rounds: consecutiveNoNewRounds,
    consecutive_no_new_relevant_rounds: consecutiveNoNewRounds,
    required_no_new_relevant_rounds: IDLE_REQUIRED,
    consecutive_no_new_rounds: consecutiveNoNewRounds, required_no_new_rounds: IDLE_REQUIRED,
    // Compatibility aliases for the existing read-only dashboard/heartbeat.
    consecutive_no_new_clicks: consecutiveNoNewRounds, required_no_new_clicks: IDLE_REQUIRED,
    clicks_since_recovery: clicksSinceRecovery,
    required_clicks_after_recovery: IDLE_REQUIRED,
    see_more_bottom_offset_px: seeMoreBottomOffset,
    image_coverage: {
      with_image: rows.length - missingImages.length,
      missing_image: missingImages.length,
      coverage_rate: rows.length ? Number(((rows.length - missingImages.length) / rows.length).toFixed(6)) : 0,
      backfill_queue: IMAGE_BACKFILL_QUEUE,
    },
    ingestion_quality_gate: {
      version: 'core-fields-and-image-v1',
      required_for_new_goods: ['title', 'product_url', 'price_usd', 'image_url'],
      pending_incomplete_goods: pendingRows.length,
      pending_file: PENDING_PRODUCTS,
    },
    image_policy: {
      mode: 'BIDI_FAIL_REQUEST', status: bidiStatus, blocked_requests: imageBlocked,
      already_cancelled_before_fail: imageCancelRaces,
    },
    traffic_saver: {
      status: trafficSavingMode ? 'ACTIVE' : 'BOOTSTRAP_ONLY_IMAGES',
      blocked_nonessential_requests: nonEssentialBlocked,
      blocked_host_classes: ['pftka-us.temu.com', 'thtka-us.temu.com', 'adtk-us.temu.com'],
    },
    network_usage_file: NETWORK_USAGE,
    http_block: httpBlock, browser_reused: !bootstrapFromSafeTab, navigated_or_refreshed: bootstrapFromSafeTab,
    started_at: startedAt, updated_at: now(), output_directory: OUT,
    coverage_claim: status === 'KEYWORD_COMPLETE' ? 'KEYWORD_FRONTEND_COMPLETE_BY_STOP_RULE' : 'CURRENT_SESSION_IN_PROGRESS',
    platform_complete: false,
    keyword_complete: ['KEYWORD_COMPLETE','KEYWORD_LIMIT_REACHED','KEYWORD_NO_RESULTS'].includes(status),
    completion_basis: status === 'KEYWORD_COMPLETE' ? (noMoreResultsConfirmed ? 'NO_MORE_RESULTS_CONFIRMED' : 'CONFIGURED_COMPLETE_ROUNDS_WITHOUT_NEW_RELEVANT_OR_UNCLASSIFIED_GOODS') : status === 'KEYWORD_LIMIT_REACHED' ? 'CONFIGURED_PRODUCT_LIMIT_REACHED' : null,
    completion_scope: status === 'KEYWORD_COMPLETE' || status === 'KEYWORD_LIMIT_REACHED' ? 'TEMU_US_VISIBLE_SEARCH_RESULTS_FOR_SINGLE_QUERY' : null,
    max_products: MAX_PRODUCTS,
    ...extra,
    ...phase,
    ...manualCaptcha,
    ...(sessionRecovery || {}),
  }, null, 2));
  if (sessionRecovery) {
    await appendEvent('SESSION_RECOVERY_REQUESTED', {status, ...sessionRecovery, unique_goods: rows.length});
    await boundedShutdown(2);
  }
}

function startBidi() {
  const logNetworkFailure = diagnosticLimiter();
  return new Promise((resolve) => {
    const ws = new WebSocket(bidiUrl);
    let seq = 0;
    const pending = new Map();
    let lifecycle;
    const connectTimer = setTimeout(() => { ws.close(); resolve(null); }, 15000);
    const call = (method, params = {}) => new Promise((ok, bad) => {
      const id = ++seq;
      const timer = setTimeout(() => { pending.delete(id); bad(new Error(`BiDi timeout: ${method}`)); }, 4000);
      pending.set(id, { ok, bad, timer });
      try { ws.send(JSON.stringify({ id, method, params })); }
      catch (error) { clearTimeout(timer); pending.delete(id); bad(error); }
    });
    lifecycle = createOwnedLifecycle({call, sid, context: chosen, file: path.join(OUT, 'bidi-owned-intercepts.json')});
    const cleanup = async () => {
      clearTimeout(connectTimer);
      try { await lifecycle.cleanup(); } finally {
        ws.close();
        for (const p of pending.values()) { clearTimeout(p.timer); p.bad(new Error('BiDi closed')); }
        pending.clear();
      }
    };
    ws.onclose = () => {
      clearTimeout(connectTimer);
      for (const p of pending.values()) { clearTimeout(p.timer); p.bad(new Error('BiDi disconnected')); }
      pending.clear();
    };
    ws.onmessage = async ({ data }) => {
      const m = JSON.parse(String(data));
      if (m.id) {
        const p = pending.get(m.id); if (!p) return;
        clearTimeout(p.timer); pending.delete(m.id); return m.type === 'error' ? p.bad(new Error(JSON.stringify(m))) : p.ok(m.result);
      }
      if (m.type !== 'event') return;
      if (m.method === 'browsingContext.navigationStarted') {
        currentDocumentUrl = topDocumentUrl(currentDocumentUrl, m.params, chosen);
        return;
      }
      if (m.method === 'network.beforeRequestSent') {
        const request = m.params.request || {};
        const requestId = String(request.request || '');
        const info = networkInfo(request);
        if (requestId) {
          networkRequests.set(requestId, info);
          if (networkRequests.size > 20000) networkRequests.delete(networkRequests.keys().next().value);
        }
        addNetworkMetric(info, 'requests');
        if (!m.params?.isBlocked || !lifecycle.owns(m.params)) return;
        lifecycle.track(requestId);
        const accept = (request.headers || []).find(h => String(h.name).toLowerCase() === 'accept')?.value?.value || '';
        const destination = String(request.destination || '').toLowerCase();
        const verificationPage = allowPageImages({documentUrl: currentDocumentUrl,
          preparing: initialProductsPreparing, humanVerification: humanVerificationActive});
        const isImage = blockImageRequest({url: String(request.url || ''), destination, accept,
          documentUrl: currentDocumentUrl, preparing: initialProductsPreparing,
          humanVerification: humanVerificationActive});
        const requestHost = (() => { try { return new URL(request.url).hostname.toLowerCase(); } catch { return ''; } })();
        const isNonEssential = nonEssentialRequest(requestHost, destination, trafficSavingMode && categoryConfig.temu.allow_site_services !== true, verificationPage);
        try {
          await call(!lifecycle.closing && (isImage || isNonEssential) ? 'network.failRequest' : 'network.continueRequest', { request: request.request });
          lifecycle.released(requestId);
          if (isImage) imageBlocked += 1;
          else if (isNonEssential) nonEssentialBlocked += 1;
        } catch (error) {
          // The DOM neutralizer can cancel an image between beforeRequestSent
          // and failRequest. That benign race used to flood events.jsonl and
          // delay the collection loop; count it without writing one event per
          // request.
          if (/no such request/i.test(String(error))) { imageCancelRaces += 1; lifecycle.released(requestId); }
          else await appendEvent('NETWORK_INTERCEPT_ERROR', { image: isImage, message: String(error).slice(0, 500) });
        }
      }
      if (m.method === 'network.responseCompleted') {
        const requestId = String(m.params?.request?.request || '');
        const info = networkRequests.get(requestId) || networkInfo(m.params?.request || {});
        networkRequests.delete(requestId);
        addNetworkMetric(info, 'completed');
        const bytes = observedResponseBytes(m.params?.response || {}, m.params || {});
        if (bytes === null) addNetworkMetric(info, 'responses_without_size');
        else addNetworkMetric(info, 'response_bytes_observed', bytes);
        const status = Number(m.params?.response?.status);
        const url = String(m.params?.response?.url || m.params?.request?.url || '');
        const evidence = networkEvidence(m.params || {}, currentDocumentUrl, chosen);
        if ([403, 429].includes(status) && logNetworkFailure(evidence)) {
          await appendEvent('NETWORK_HTTP_FAILURE', evidence);
        }
        if ([403, 429].includes(status) && collectionRequest(evidence)) {
          httpBlock = { ...evidence, status, observed_at: now() };
        } else if (supersededBySuccess(evidence, httpBlock)) {
          await appendEvent('HTTP_SIGNAL_SUPERSEDED_BY_SUCCESS', {endpoint:evidence.endpoint, previous_status:httpBlock.status});
          httpBlock = null;
        }
      }
      if (m.method === 'network.fetchError') {
        const requestId = String(m.params?.request?.request || '');
        const info = networkRequests.get(requestId) || networkInfo(m.params?.request || {});
        networkRequests.delete(requestId);
        addNetworkMetric(info, 'failed');
        const evidence = networkEvidence(m.params || {}, currentDocumentUrl, chosen);
        if (evidence.error_code && logNetworkFailure(evidence)) await appendEvent('NETWORK_FETCH_FAILURE', evidence);
      }
    };
    ws.onerror = async () => { bidiStatus = 'ERROR'; await cleanup(); await appendEvent('BIDI_ERROR'); resolve(null); };
    ws.onopen = async () => {
      try {
        clearTimeout(connectTimer);
        await lifecycle.recover();
        lifecycle.subscribed(await call('session.subscribe', { contexts: [chosen], events: ['browsingContext.navigationStarted', 'network.beforeRequestSent', 'network.responseCompleted', 'network.fetchError'] }));
        await lifecycle.added(await call('network.addIntercept', {
          contexts: [chosen],
          phases: ['beforeRequestSent'],
          urlPatterns: [{ type: 'pattern', protocol: 'https' }],
        }));
        bidiStatus = 'ACTIVE';
        await appendEvent('IMAGE_BLOCK_ACTIVE', { scope: 'ALL_HTTPS_CLASSIFY_IMAGE_ONLY', cancel_races_suppressed: true });
        resolve({close: cleanup});
      } catch (error) {
        bidiStatus = 'ERROR'; await cleanup(); await appendEvent('BIDI_SETUP_ERROR', { message: String(error).slice(0, 800) }); resolve(null);
      }
    };
  });
}

const extractScript = `
const ctx=arguments[0];
const knownIds=new Set((ctx.knownIds||[]).map(String));
const repairIds=new Set((ctx.repairIds||[]).map(String));
const idFrom=v=>(String(v||'').match(/[?&](?:goods_id|goodsId)=(\\d+)(?:[&#]|$)/i)||String(v||'').match(/-g-(\\d+)\\.html(?:[?#]|$)/i)||[])[1]||null;
const anchors=[...document.querySelectorAll('a[href]')].filter(a=>idFrom(a.href));
const out=[],seen=new Set();
const adObservations=window.__temuAdObservations||(window.__temuAdObservations=new Map());
const cleanUrl=v=>{try{const u=new URL(v,location.href);u.username='';u.password='';u.hash='';u.search='';return u.href}catch{return null}};
for(const a of anchors){
 const id=idFrom(a.href); if(!id||seen.has(id))continue; seen.add(id);
 if(knownIds.has(String(id))&&!repairIds.has(String(id)))continue;
 let card=a,cardDepth=0;
 for(let d=0;d<32&&card.parentElement;d++){
  const p=card.parentElement;
  if(/^(?:BODY|HTML)$/.test(p.tagName||''))break;
  const nodes=[p,...p.querySelectorAll('a[href],[data-goods-id],[data-goodsid]')];
  const ids=new Set(nodes.flatMap(x=>[idFrom(x.href),x.getAttribute?.('data-goods-id'),x.getAttribute?.('data-goodsid')])
    .filter(value=>/^\\d+$/.test(String(value||''))).map(String));
  if(ids.size!==1||!ids.has(id))break;
  card=p;cardDepth++;
 }
 const fullCardText=(card.innerText||a.innerText||'').trim();
 const text=fullCardText.slice(0,10000);
 // Hidden/lazy or truncated cards cannot certify a negative observation.
 const rect=card.getBoundingClientRect(),style=getComputedStyle(card);
 const cardObserved=rect.width>0&&rect.height>0&&rect.top>=0&&rect.left>=0&&rect.bottom<=innerHeight&&rect.right<=innerWidth&&style.display!=='none'&&style.visibility!=='hidden'&&style.opacity!=='0'&&fullCardText.length>0&&fullCardText.length<=10000;
 const adLabel=/^(?:AD|Sponsored|Advertisement|广告|廣告|赞助|贊助)$/i;
 const labels=[fullCardText,...[card,...card.querySelectorAll('*')].flatMap(n=>['aria-label','title'].map(k=>n.getAttribute(k)||''))];
 const hasAdLabel=labels.some(value=>String(value).split(/\\n+/).some(line=>adLabel.test(line.trim())));
 const stamp=Date.parse(ctx.observedAt),signature=location.href+'|'+JSON.stringify(labels);
 const previousAdObservation=adObservations.get(id);
 const settled=cardObserved&&document.readyState==='complete'&&!card.querySelector('[aria-busy="true"],[data-loading="true"]');
 if(!settled)adObservations.delete(id);
 else if(!previousAdObservation||previousAdObservation.signature!==signature)adObservations.set(id,{signature,at:stamp});
 // Absence is recorded only after a second unchanged complete scan >=2s later.
 // Positive labels can be recorded immediately; skeleton/lazy cards stay unknown.
 const stableAbsence=settled&&previousAdObservation?.signature===signature&&Number.isFinite(stamp)&&stamp-previousAdObservation.at>=2000;
 const sponsorship=settled&&(hasAdLabel||stableAbsence)?{is_sponsored:hasAdLabel,sponsorship_observation:'VISIBLE_COMPLETE_CARD_LABEL_SCAN_V1',sponsorship_observed_at:ctx.observedAt}:{};
 const imgs=[...card.querySelectorAll('img')];
 const raw=[];
 for(const i of imgs){
  for(const v of [i.currentSrc,i.getAttribute('src'),i.getAttribute('data-src'),i.getAttribute('data-original'),i.getAttribute('data-lazy-src'),i.getAttribute('data-temu-original-src')])if(v&&!String(v).startsWith('data:'))raw.push(v);
  for(const attr of ['srcset','data-srcset','data-temu-original-srcset'])for(const x of String(i.getAttribute(attr)||'').split(',')){const v=x.trim().split(/\\s+/)[0];if(v&&!String(v).startsWith('data:'))raw.push(v)}
 }
 // Some cards keep lazy image URLs on <source>, wrapper attributes, or CSS
 // backgrounds instead of img.src. Preserve those URLs without fetching them.
 for(const n of [card,...card.querySelectorAll('*')]){
  for(const attr of [...(n.attributes||[])]){
   const value=String(attr.value||'');
   for(const m of value.matchAll(/https?:\\/\\/[^\\s"'<>]+(?:\\.(?:avif|gif|jpe?g|png|webp))(?:[^\\s"'<>]*)?/ig))raw.push(m[0]);
  }
  const bg=getComputedStyle(n).backgroundImage||'';
  for(const m of bg.matchAll(/url\\(["']?(https?:\\/\\/[^)"']+)["']?\\)/ig))raw.push(m[1]);
 }
 const images=[...new Set(raw.map(cleanUrl).filter(x=>x&&/^https?:/i.test(x)))];
 const lines=(a.innerText||text).split(/\\n+/).map(x=>x.trim()).filter(x=>x.length>8&&!/^(Top pick|精选推荐|精選推薦|Quick look|快速查看|Local Warehouse|本地仓库|本地倉庫|Open in new tab\\.?|在新标签页中打开|在新標籤頁中開啟|Free shipping|免费配送|免費配送|平台包邮|平台包郵)$/i.test(x));
 const title=(a.getAttribute('title')||imgs.find(i=>i.alt&&i.alt.length>8)?.alt||lines.sort((x,y)=>y.length-x.length)[0]||null);
 const price=(text.match(/\\$\\s*([0-9]+(?:\\.[0-9]{1,2})?)/)||[])[1]||null;
 const metricLabels=[card,...card.querySelectorAll('*')].filter(n=>{
  if(typeof n.getBoundingClientRect!=='function')return false;
  const r=n.getBoundingClientRect(),s=getComputedStyle(n);
  return r.width>0&&r.height>0&&s.display!=='none'&&s.visibility!=='hidden'&&s.opacity!=='0';
 }).flatMap(n=>['aria-label','title'].map(key=>n.getAttribute(key)||'')).filter(Boolean);
 const metrics=(${parseProductCardMetrics.toString()})(text,metricLabels);
 const score=u=>{const m=String(u).match(/[_-](\\d{2,4})x(\\d{2,4})/);return m?Number(m[1])*Number(m[2]):0};
 const main=[...images].sort((x,y)=>score(y)-score(x))[0]||null;
 out.push({goods_id:id,title,price_usd:price?safeNum(price):null,...metrics,product_url:cleanUrl(a.href),thumbnail_url:images[0]||null,main_image_url:main,all_image_urls:images,card_text:text,card_metrics_version:'explicit-card-metrics-v1',card_ancestor_depth:cardDepth,source:'VISIBLE_TEMU_FRONTEND',...sponsorship,observed_at:ctx.observedAt});
}
function safeNum(v){const n=Number(v);return Number.isFinite(n)?n:null}
const captured=window.__temuCapturedProducts?[...window.__temuCapturedProducts.values()].filter(x=>!knownIds.has(String(x.goods_id))||repairIds.has(String(x.goods_id))):[];
const loginFormVisible=[...document.querySelectorAll('input[type="password"], [role="dialog"] input[type="email"], [role="dialog"] input[autocomplete="username"]')].some(e=>{if(typeof e.getBoundingClientRect!=='function')return false;const r=e.getBoundingClientRect(),s=getComputedStyle(e);return r.width>0&&r.height>0&&s.visibility!=='hidden'&&s.display!=='none'});
return {loginFormVisible,rows:out,captured,visibleGoodsCount:seen.size,url:location.href,documentURI:document.documentURI,title:document.title,text:(document.body?.innerText||'').slice(0,5000),scrollY,innerHeight,scrollHeight:document.documentElement?.scrollHeight ?? 0};`;

const installResponseCaptureScript = `
if(window.__temuResponseCaptureVersion!==2){
 const store=window.__temuCapturedProducts=new Map();
 const clean=v=>String(v||'');
 const isImg=v=>/^https?:\\/\\//i.test(clean(v))&&/(?:kwcdn\\.com|\\.(?:avif|gif|jpe?g|png|webp)(?:[?#]|$))/i.test(clean(v));
 const collectImages=(value,out,depth=0)=>{
  if(depth>5||value==null)return;
  if(typeof value==='string'){if(isImg(value))out.add(clean(value));return;}
  if(Array.isArray(value)){for(const x of value.slice(0,80))collectImages(x,out,depth+1);return;}
  if(typeof value==='object')for(const [k,v] of Object.entries(value)){
   if(/image|thumb|pic|gallery|url/i.test(k)||depth<2)collectImages(v,out,depth+1);
  }
 };
 const walk=(value,depth=0)=>{
  if(depth>10||value==null)return;
  if(Array.isArray(value)){for(const x of value)walk(x,depth+1);return;}
  if(typeof value!=='object')return;
  const id=value.goodsId??value.goods_id??value.goodsID;
  if(id&&/^\\d+$/.test(String(id))){
   const images=new Set();collectImages(value,images);
   if(images.size){
    const old=store.get(String(id))||{goods_id:String(id),images:[]};
    const merged=[...new Set([...old.images,...images])];
    store.set(String(id),{goods_id:String(id),images:merged,title:value.goodsName||value.title||value.goods_name||old.title||null});
   }
  }
  for(const v of Object.values(value))if(v&&typeof v==='object')walk(v,depth+1);
 };
 const captureText=text=>{try{walk(JSON.parse(text))}catch{}};
 const originalFetch=window.fetch;
 window.fetch=async function(...args){const response=await originalFetch.apply(this,args);try{response.clone().text().then(captureText).catch(()=>{})}catch{}return response};
 const xhrProto=window.XMLHttpRequest.prototype;
 const originalSend=xhrProto.send;
 xhrProto.send=function(...args){
  this.addEventListener('load',()=>{try{if(typeof this.responseText==='string')captureText(this.responseText)}catch{}},{once:true});
  return originalSend.apply(this,args);
 };
 window.__temuResponseCaptureInstalled=true;
 window.__temuResponseCaptureVersion=2;
}
return {active:!!window.__temuResponseCaptureInstalled,version:window.__temuResponseCaptureVersion,captured:window.__temuCapturedProducts?.size||0};`;

const neutralizeImagesScript = `
if(!document.documentElement)return {active:false,reason:'DOCUMENT_NOT_READY',images:0,neutralized:0};
if(/(?:bgn_verification|verification|captcha|verifyCode)/i.test(location.pathname)
 || /security verification|complete the verification|slide to complete the puzzle|安全验证|安全驗證|验证码|驗證碼|人机验证|人機驗證|滑块验证|滑塊驗證/i.test(document.body?.innerText||'')){
 if(window.__temuImageNeutralizer?.observer)window.__temuImageNeutralizer.observer.disconnect();
 delete window.__temuImageNeutralizer;
 for(const img of document.images){
  for(const [attr,saved] of [['src','data-temu-original-src'],['srcset','data-temu-original-srcset']]){
   if(img.hasAttribute(saved))img.setAttribute(attr,img.getAttribute(saved));
  }
 }
 return {active:false,reason:'VERIFICATION_PAGE_IMAGES_ALLOWED',images:document.images.length,neutralized:0};
}
if(!window.__temuImageNeutralizer){
 const blank='data:image/gif;base64,R0lGODlhAQABAAD/ACwAAAAAAQABAAACADs=';
 const save=(img,attr,target)=>{const v=img.getAttribute(attr);if(v&&v!==blank&&!img.getAttribute(target))img.setAttribute(target,v)};
 const neutralize=img=>{
  if(!(img instanceof HTMLImageElement))return;
  save(img,'src','data-temu-original-src'); save(img,'srcset','data-temu-original-srcset');
  for(const attr of ['data-src','data-original','data-lazy-src','data-srcset']){
   const v=img.getAttribute(attr); if(v){const target=attr.includes('srcset')?'data-temu-original-srcset':'data-temu-original-src';if(!img.getAttribute(target))img.setAttribute(target,v);img.removeAttribute(attr)}
  }
  if(img.getAttribute('src')!==blank)img.setAttribute('src',blank);
  img.removeAttribute('srcset'); img.setAttribute('loading','lazy');
 };
 document.querySelectorAll('img').forEach(neutralize);
 const observer=new MutationObserver(ms=>{for(const m of ms){if(m.type==='childList')for(const n of m.addedNodes){if(n instanceof HTMLImageElement)neutralize(n);else if(n.querySelectorAll)n.querySelectorAll('img').forEach(neutralize)}else if(m.target instanceof HTMLImageElement)neutralize(m.target)}});
 observer.observe(document.documentElement,{subtree:true,childList:true,attributes:true,attributeFilter:['src','srcset','data-src','data-original','data-lazy-src','data-srcset']});
 window.__temuImageNeutralizer={active:true,observer};
}
return {active:true,images:document.images.length,neutralized:[...document.images].filter(i=>i.hasAttribute('data-temu-original-src')||i.hasAttribute('data-temu-original-srcset')).length};`;

async function ensurePageGuards(logActivation = false) {
  const guardPage = await bootstrapPageState();
  humanVerificationActive = classify(guardPage) === 'HUMAN_VERIFICATION';
  // A CAPTCHA/login transition can replace the whole document. Window-scoped
  // fetch/XHR hooks and MutationObservers disappear on that navigation, so
  // reinstall them at every collection-loop boundary. The scripts are
  // idempotent and cheap when the current document already has the guards.
  const responseCapture = await execute(installResponseCaptureScript, [], 'install_response_capture');
  const neutralizer = initialProductsPreparing ? {active:false,reason:'BOOTSTRAP_RESOURCES_ALLOWED'}
    : await execute(neutralizeImagesScript, [], 'install_image_neutralizer');
  if (logActivation) {
    await appendEvent('SEARCH_RESPONSE_CAPTURE_ACTIVE', responseCapture);
    await appendEvent('DOM_IMAGE_NEUTRALIZER_ACTIVE', neutralizer);
  }
  return { responseCapture, neutralizer };
}

async function scan({commit=!(['sales','recent'].includes(SORT_REQUEST) && initialProductsPreparing)}={}) {
  const repairIds = [
    ...pendingProducts.keys(),
    ...unclassifiedProducts.keys(),
    ...[...products.entries()].filter(([, row]) => row.card_metrics_version!=='explicit-card-metrics-v1' || typeof row.is_sponsored !== 'boolean' || (!row.main_image_url && !row.thumbnail_url && !(Array.isArray(row.all_image_urls) && row.all_image_urls.length))).map(([id]) => id),
  ];
  const data = await execute(extractScript, [{ observedAt: now(), knownIds: [...products.keys()], repairIds }], initialProductsPreparing?'initial_scan':'collect_scan');
  // A login/verification/network page has no sort controls. Return its actual
  // state to the recovery loop before checking sorting or ingesting any rows.
  if (!commit || classify(data)) return data;
  if (['sales','recent'].includes(SORT_REQUEST)) {
    const state=await readSalesSort().catch(() => ({}));
    const confirmed=salesSortEstablished && confirmsSalesSort(state, QUERY);
    salesSort={...salesSort, state:confirmed ? 'UI_CONFIRMED' : 'UNCONFIRMED',
      label:confirmed ? state.selected_label : null, confirmed_by:confirmed ? state.selected_by : null,
      observed_at:now(), query:QUERY};
    if (!confirmed) {
      // Navigation may occur between extraction and reading the sort control.
      const latest=await bootstrapPageState();
      if (classify(latest)) return latest;
      // Verification replaces the document. Its first product render may
      // precede the sort control or finish while that control is still busy.
      // Re-establish real UI evidence instead of stopping on that one read.
      await appendEvent('SORT_RECONFIRM_AFTER_PAGE_TRANSITION', {query:QUERY});
      await prepareSalesSort();
      // A sort action can replace the list: never ingest the pre-sort rows.
      // The next normal scan commits only freshly confirmed rows.
      return scan({commit:false});
    }
    for (const row of data.rows || []) row.search_sort={...salesSort};
  }
  let added = 0;
  let imageBackfilled = 0;
  let promotedFromPending = 0;
  const normalizeImages = row => [...new Set([
    ...(Array.isArray(row.all_image_urls) ? row.all_image_urls : []),
    row.main_image_url, row.thumbnail_url,
  ].filter(v => typeof v === 'string' && /^https?:\/\//i.test(v)))];
  const readyForCommit = row => Boolean(
    row?.title && row?.product_url && Number.isFinite(Number(row?.price_usd)) && normalizeImages(row).length,
  );
  const withNormalizedImages = row => {
    const images = normalizeImages(row);
    return { ...row, thumbnail_url: row.thumbnail_url || images[0] || null,
      main_image_url: row.main_image_url || images[0] || null, all_image_urls: images };
  };
  for (const row of data.rows || []) {
    if (isSevereOffTopic(row)) {
      filteredSevereOffTopic.set(String(row.goods_id), row);
      products.delete(String(row.goods_id));
      pendingProducts.delete(String(row.goods_id));
      unclassifiedProducts.delete(String(row.goods_id));
      continue;
    }
    if(categoryRelevance.strict && categoryRelevance.classify(row).state==='UNKNOWN') {
      const id=String(row.goods_id);
      unclassifiedProducts.set(id,withNormalizedImages({...unclassifiedProducts.get(id),...row,
        search_query:QUERY,site:'TEMU_US',currency:'USD',source_url:SAFE_SOURCE,
        category_relevance:categoryRelevance.classify(row)}));
      products.delete(id);pendingProducts.delete(id);continue;
    }
    unclassifiedProducts.delete(String(row.goods_id));
    const old = products.get(row.goods_id);
    if (!old) {
      const pending = pendingProducts.get(row.goods_id) || {};
      const candidate = withNormalizedImages({ ...pending, ...Object.fromEntries(Object.entries(row).filter(([,v]) => v !== null && v !== '' && !(Array.isArray(v) && !v.length))),
        first_seen_round: pending.first_seen_round ?? round, search_query: QUERY, site: 'TEMU_US', currency: 'USD', source_url: SAFE_SOURCE });
      if (readyForCommit(candidate)) {
        candidate.first_seen_ordinal = products.size + 1;
        products.set(row.goods_id, candidate); pendingProducts.delete(row.goods_id);
        added += 1; if (Object.keys(pending).length) promotedFromPending += 1;
      } else {
        pendingProducts.set(row.goods_id, candidate);
      }
    } else {
      products.set(row.goods_id, withNormalizedImages({ ...old, ...Object.fromEntries(Object.entries(row).filter(([,v]) => v !== null && v !== '' && !(Array.isArray(v) && !v.length))), first_seen_ordinal: old.first_seen_ordinal, first_seen_round: old.first_seen_round }));
    }
  }
  // Response-derived images are merged only into goods already observed in
  // the visible frontend. This prevents unrelated recommendation payloads
  // from creating phantom search results while allowing later appearances to
  // repair earlier image gaps.
  for (const item of data.captured || []) {
    const id = String(item.goods_id);
    const committed = products.get(id);
    const old = committed || pendingProducts.get(id);
    if (!old || !Array.isArray(item.images) || !item.images.length) continue;
    const lackedImage = !old.main_image_url && !old.thumbnail_url && !(Array.isArray(old.all_image_urls) && old.all_image_urls.length);
    const images = [...new Set([...(old.all_image_urls || []), ...item.images])];
    const repaired = withNormalizedImages({
      ...old,
      thumbnail_url: old.thumbnail_url || images[0] || null,
      main_image_url: old.main_image_url || images[0] || null,
      all_image_urls: images,
      image_source: old.main_image_url ? old.image_source : 'SEARCH_RESPONSE_JSON',
    });
    if (committed) {
      products.set(id, repaired);
      if (lackedImage) imageBackfilled += 1;
    } else if (readyForCommit(repaired)) {
      repaired.first_seen_ordinal = products.size + 1;
      products.set(id, repaired); pendingProducts.delete(id);
      added += 1; promotedFromPending += 1;
    } else {
      pendingProducts.set(id, repaired);
    }
  }
  const trimmedToLimit = enforceConfiguredProductLimit();
  return {
    ...data,
    added: Math.max(0, added - trimmedToLimit),
    imageBackfilled,
    promotedFromPending,
    trimmedToLimit,
  };
}

async function lightPageState() {
  return execute(`
    const text=(document.body?.innerText||'').slice(0,5000);
    const visibleGoodsCount=document.querySelectorAll('a[href*="goods_id="],a[href*="goodsId="],a[href*="-g-"]').length;
    const visibleGoodsIds=[...new Set([...document.querySelectorAll('a[href]')].map(a=>(a.href.match(/[?&](?:goods_id|goodsId)=(\\d+)(?:[&#]|$)/i)||a.href.match(/-g-(\\d+)\\.html/i)||[])[1]).filter(Boolean))];
    const loginFormVisible=[...document.querySelectorAll('input[type="password"], [role="dialog"] input[type="email"], [role="dialog"] input[autocomplete="username"]')].some(e=>{if(typeof e.getBoundingClientRect!=='function')return false;const r=e.getBoundingClientRect(),s=getComputedStyle(e);return r.width>0&&r.height>0&&s.visibility!=='hidden'&&s.display!=='none'});
    return {...(${inspectSearchEnd.toString()})(),...(${inspectSearchReadiness.toString()})(),readyState:document.readyState,loginFormVisible,url:location.href,documentURI:document.documentURI,title:document.title,text,rows:visibleGoodsCount?[{}]:[],visibleGoodsCount,visibleGoodsIds,scrollY,innerHeight,scrollHeight:document.documentElement?.scrollHeight ?? 0};
  `);
}

async function recordCollectionScroll() {
  if (scrollingCollectionStarted) return;
  scrollingCollectionStarted = true;
  await appendEvent('SCROLL_COLLECTION_STARTED', {query: QUERY, sort: SORT_REQUEST});
  await writeSnapshot('RUNNING');
}

async function visibleScrollToBottom() {
  let latest = await scan();
  if (classify(latest)) return latest;
  let stable = 0;
  let previousY = null, stalledScrolls = 0, instantRecovery = false, recoveryVerified = false;
  for (let i = 0; i < 80; i++) {
    const s = await lightPageState();
    if (classify(s)) return s;
    stalledScrolls = previousY !== null && Math.abs(s.scrollY - previousY) < 2 ? stalledScrolls + 1 : 0;
    previousY = s.scrollY;
    if (instantRecovery && !recoveryVerified && stalledScrolls === 0) {
      recoveryVerified = true;
      await appendEvent('SCROLL_STALL_RECOVERY_VERIFIED', {scroll_y: s.scrollY, observed_movement: true});
    }
    if (!instantRecovery && stalledScrolls >= 2) {
      instantRecovery = true;
      await appendEvent('SCROLL_STALL_RECOVERY', {scroll_y: s.scrollY, action: 'INSTANT_SCROLL'});
    }
    const scrollBehavior = instantRecovery ? 'instant' : 'smooth';
    const remaining = s.scrollHeight - s.innerHeight - s.scrollY;
    if (remaining <= 30) { stable += 1; if (stable >= 2) return scan(); }
    else stable = 0;
    const step = Math.random() < 0.12 ? -rand(80, 220) : rand(210, 500);
    await execute(`window.scrollBy({top:arguments[0],left:0,behavior:arguments[1]});return true;`, [step, scrollBehavior]);
    await recordCollectionScroll();
    await sleep(rand(950, 1650));
  }
  return scan();
}

async function visibleScrollToLearnedSeeMoreZone() {
  if (!Number.isFinite(seeMoreBottomOffset)) return visibleScrollToBottom();
  const initial = await scan();
  if (classify(initial)) return initial;
  let previousY = null, stalledScrolls = 0, instantRecovery = false, recoveryVerified = false;
  for (let i = 0; i < 80; i++) {
    const s = await lightPageState();
    if (classify(s)) return s;
    stalledScrolls = previousY !== null && Math.abs(s.scrollY - previousY) < 2 ? stalledScrolls + 1 : 0;
    previousY = s.scrollY;
    if (instantRecovery && !recoveryVerified && stalledScrolls === 0) {
      recoveryVerified = true;
      await appendEvent('SCROLL_STALL_RECOVERY_VERIFIED', {scroll_y: s.scrollY, observed_movement: true});
    }
    if (!instantRecovery && stalledScrolls >= 2) {
      instantRecovery = true;
      await appendEvent('SCROLL_STALL_RECOVERY', {scroll_y: s.scrollY, action: 'INSTANT_SCROLL'});
    }
    const scrollBehavior = instantRecovery ? 'instant' : 'smooth';
    const targetY = Math.max(0, s.scrollHeight - seeMoreBottomOffset - s.innerHeight * 0.52);
    const delta = targetY - s.scrollY;
    if (Math.abs(delta) <= 70) return { ...(await scan()), usedLearnedOffset: true };
    const step = Math.sign(delta) * Math.min(Math.abs(delta), rand(210, 480));
    await execute(`window.scrollBy({top:arguments[0],left:0,behavior:arguments[1]});return true;`, [step, scrollBehavior]);
    await recordCollectionScroll();
    await sleep(rand(950, 1600));
  }
  // A learned offset is a hard positioning policy. Even if smooth scrolling
  // did not converge within the bounded loop, callers must not fall back to
  // the footer scanner (which can drag the page to the real bottom).
  return { ...(await scan()), usedLearnedOffset: true };
}

async function rememberSeeMoreBottomOffset(element) {
  if (!element?.[EKEY]) return;
  const measured = await execute(`if(!document.documentElement)return null;const e=arguments[0],r=e.getBoundingClientRect(),h=document.documentElement.scrollHeight;return h-(scrollY+r.top+r.height/2);`, [element]);
  const value = Number(measured);
  if (Number.isFinite(value) && value > 100 && value < 6000) {
    seeMoreBottomOffset = seeMoreBottomOffset == null ? value : seeMoreBottomOffset * 0.75 + value * 0.25;
  }
}

async function humanJitterAtSeeMore() {
  const amount = rand(55, 170) * (Math.random() < 0.5 ? -1 : 1);
  await execute(`window.scrollBy({top:arguments[0],left:0,behavior:'smooth'});return true;`, [amount]);
  await recordCollectionScroll();
  await sleep(rand(650, 1450));
  if (Math.random() < 0.55) {
    await execute(`window.scrollBy({top:arguments[0],left:0,behavior:'smooth'});return true;`, [-Math.round(amount * (0.45 + Math.random() * 0.35))]);
    await sleep(rand(550, 1250));
  }
  return findSeeMore();
}

async function waitForExpansionToSettle(countBefore) {
  const deadline = Date.now() + 10000;
  const minimum = Date.now() + 2800;
  let previousVisibleCount = -1;
  let previousHeight = -1;
  let stablePolls = 0;
  let latest = null;
  while (Date.now() < deadline) {
    await sleep(rand(400, 700));
    latest = await lightPageState();
    const currentVisibleCount = Number(latest?.visibleGoodsCount) || 0;
    const currentHeight = Number(latest?.scrollHeight) || 0;
    if (currentVisibleCount === previousVisibleCount && currentHeight === previousHeight) stablePolls += 1;
    else stablePolls = 0;
    previousVisibleCount = currentVisibleCount;
    previousHeight = currentHeight;
    if (Date.now() >= minimum && stablePolls >= 3) return scan();
    if (classify(latest)) return latest;
  }
  return scan();
}

async function retrySeeMoreClick(reason) {
  failedSeeMoreClicks += 1;
  consecutiveNoNewRounds = 0;
  await appendEvent('SEE_MORE_RETRY',{reason,attempt:failedSeeMoreClicks,round,unique_goods:products.size});
  if(failedSeeMoreClicks>=3) {
    finalStatus='STOPPED_SEE_MORE_UNAVAILABLE';
    await writeSnapshot(finalStatus,{stop_reason:'SEE_MORE_CLICK_WITHOUT_CONFIRMED_EXPANSION',
      click_failures:failedSeeMoreClicks,keyword_complete:false});
  }else{
    await writeSnapshot('WAITING_SEE_MORE_CLICK',{automatic_retry:true,click_failures:failedSeeMoreClicks});
    await sleep(1500);
  }
}

async function revealSeeMoreAboveFooter() {
  await execute(`window.scrollBy({top:arguments[0],left:0,behavior:'smooth'});return true;`, [-rand(480, 920)]);
  await sleep(rand(900, 1600));
  let element = await findSeeMore();
  if (element?.[EKEY]) return element;
  await execute(`window.scrollBy({top:arguments[0],left:0,behavior:'smooth'});return true;`, [-rand(240, 520)]);
  await sleep(rand(700, 1300));
  return findSeeMore();
}

async function findSeeMore() {
  return execute(`
   const vis=e=>{const s=getComputedStyle(e),r=e.getBoundingClientRect();return s.display!=='none'&&s.visibility!=='hidden'&&r.width>2&&r.height>2&&r.bottom>0&&r.top<innerHeight};
   const nodes=[...document.querySelectorAll('button,a,[role=button],div,span')].filter(e=>/^\\s*(See more|查看更多|顯示更多|显示更多|加载更多|載入更多|展開更多|展开更多|Try again|重试|重試|再试一次|再試一次|重新加载|重新載入)\\s*$/i.test(e.textContent||''));
   for(const n of nodes.sort((a,b)=>a.children.length-b.children.length)){
    let candidate=null;
    for(let e=n,i=0;i<5&&e;i++,e=e.parentElement){const s=getComputedStyle(e);if(e.matches('button,a,[role=button]')||e.tabIndex>=0||s.cursor==='pointer'){candidate=e;break}}
    if(!candidate)continue;
    if(!vis(candidate))candidate.scrollIntoView({block:'center',behavior:'auto'});
    if(vis(candidate))return candidate;
   }
   return null;`);
}

async function controlKind(element) {
  if (!element?.[EKEY]) return null;
  return execute(`const t=(arguments[0].textContent||'').trim();return /^\\s*(Try again|重试|重試|再试一次|再試一次|重新加载|重新載入)\\s*$/i.test(t)?'TRY_AGAIN':'SEE_MORE';`, [element]);
}

async function findVisibleTryAgain() {
  return execute(`
    const vis=e=>{const s=getComputedStyle(e),r=e.getBoundingClientRect();return s.display!=='none'&&s.visibility!=='hidden'&&r.width>2&&r.height>2&&r.bottom>0&&r.top<innerHeight};
    const nodes=[...document.querySelectorAll('button,a,[role=button],div,span')].filter(e=>/^\\s*(Try again|重试|重試|再试一次|再試一次|重新加载|重新載入)\\s*$/i.test(e.textContent||'')&&vis(e));
    for(const n of nodes.sort((a,b)=>a.children.length-b.children.length)){
      let candidate=null;
      for(let e=n,i=0;i<5&&e;i++,e=e.parentElement){const s=getComputedStyle(e);if(e.matches('button,a,[role=button]')||e.tabIndex>=0||s.cursor==='pointer'){candidate=e;break}}
      if(candidate&&vis(candidate))return candidate;
      if(vis(n))return n;
    }
    return null;
  `);
}

async function clickTryAgain(element = null) {
  element ||= await findVisibleTryAgain();
  if (!element?.[EKEY] || await controlKind(element) !== 'TRY_AGAIN') return { clicked: false, reason: 'not_visible' };
  try {
    // Click the same visible screen position used by the user. When Temu swaps
    // See more for Try again, the text span can sit beneath a clickable div;
    // a viewport pointer click correctly targets that topmost container.
    const point = await execute(`
      const r=arguments[0].getBoundingClientRect();
      return {x:Math.round(r.left+r.width/2),y:Math.round(r.top+r.height/2),visible:r.width>2&&r.height>2&&r.bottom>0&&r.top<innerHeight};
    `, [element]);
    if (!point?.visible) throw new Error('Try again position is outside the viewport');
    await wd('POST', '/actions', { actions: [{
      type: 'pointer', id: 'temu-retry-mouse', parameters: { pointerType: 'mouse' }, actions: [
        { type: 'pointerMove', duration: rand(140, 260), origin: 'viewport', x: point.x, y: point.y },
        { type: 'pause', duration: rand(90, 180) },
        { type: 'pointerDown', button: 0 },
        { type: 'pause', duration: rand(70, 150) },
        { type: 'pointerUp', button: 0 },
      ],
    }] });
    await wd('DELETE', '/actions').catch(() => {});
    return { clicked: true, method: 'viewport_pointer', point };
  } catch (pointerError) {
    // Last-resort DOM click uses the topmost node at the same screen position.
    const fallback = await execute(`
      const source=arguments[0];
      const isRetry=e=>/^\\s*(Try again|重试|重試|再试一次|再試一次|重新加载|重新載入)\\s*$/i.test(e?.textContent||'');
      if(!source||!isRetry(source))return {clicked:false,reason:'stale'};
      source.scrollIntoView({block:'center',behavior:'auto'});
      const r=source.getBoundingClientRect();
      const top=document.elementFromPoint(r.left+r.width/2,r.top+r.height/2);
      const candidates=[];
      for(const seed of [top,source]){
        for(let e=seed,i=0;e&&i<7;e=e.parentElement,i++){
          if(!candidates.includes(e))candidates.push(e);
        }
      }
      const target=candidates.find(e=>{
        const s=getComputedStyle(e);
        return !e.disabled&&(e.matches('button,a,[role=button]')||e.tabIndex>=0||s.cursor==='pointer'||isRetry(e));
      })||top||source;
      if(typeof target.click==='function')target.click();
      else target.dispatchEvent(new MouseEvent('click',{bubbles:true,cancelable:true,view:window}));
      return {clicked:true,tag:target.tagName,cls:String(target.className||'').slice(0,120)};
    `, [element]);
    if (fallback?.clicked) return { clicked: true, method: 'dom_topmost', fallback, pointer_error: String(pointerError).slice(0, 300) };
    throw pointerError;
  }
}

async function recoverTryAgain(element, metadata = {}) {
  const before = await lightPageState();
  try {
    const result = await clickTryAgain(element);
    if (!result.clicked) throw new Error(`Try again unavailable: ${result.reason}`);
    consecutiveTryAgainErrors = 0;
    httpBlock = null;
    consecutiveNoNewRounds = 0;
    await appendEvent('TRY_AGAIN_CLICKED', { round, unique_goods: products.size, method: result.method, ...metadata });
    await writeSnapshot('RETRYING_NETWORK', {
      retry_control: 'TRY_AGAIN',
      consecutive_try_again_no_progress: consecutiveTryAgainNoProgress,
    });
    await sleep(rand(3000, 5000));
    const after = await scan();
    const retryStillVisible = Boolean((await findVisibleTryAgain())?.[EKEY]);
    const progressed = didTryAgainProgress(before, after, retryStillVisible);
    consecutiveTryAgainNoProgress = progressed ? 0 : consecutiveTryAgainNoProgress + 1;
  } catch (error) {
    consecutiveTryAgainErrors += 1;
    consecutiveTryAgainNoProgress += 1;
    await appendEvent('TRY_AGAIN_CLICK_ERROR', {
      round,
      consecutive_errors: consecutiveTryAgainErrors,
      consecutive_no_progress: consecutiveTryAgainNoProgress,
      message: String(error).slice(0, 500),
      ...metadata,
    });
    await sleep(rand(4000, 7000));
  }
  if (consecutiveTryAgainNoProgress >= TRY_AGAIN_STALL_LIMIT) {
    finalStatus = 'STOPPED_TRY_AGAIN_STALLED';
    await appendEvent(finalStatus, {
      round,
      clicks,
      unique_goods: products.size,
      consecutive_no_progress: consecutiveTryAgainNoProgress,
      automatic_recovery_eligible: true,
    });
    await writeSnapshot(finalStatus, {
      retry_control: 'TRY_AGAIN',
      retry_errors: consecutiveTryAgainErrors,
      consecutive_try_again_no_progress: consecutiveTryAgainNoProgress,
      automatic_recovery_eligible: true,
      resume_requires_session_rotation: true,
    });
    return false;
  }
  await writeSnapshot('WAITING_TRY_AGAIN', {
    automatic_retry: true,
    retry_errors: consecutiveTryAgainErrors,
    consecutive_try_again_no_progress: consecutiveTryAgainNoProgress,
  });
  return true;
}

async function bootstrapPageState() {
  const state = await execute(`
    const visible = element => {
      const r=element.getBoundingClientRect(),s=getComputedStyle(element);
      return r.width>0&&r.height>0&&s.visibility!=='hidden'&&s.display!=='none';
    };
    const inputs=[...document.querySelectorAll('input[type="search"],input[placeholder*="search" i],input[aria-label*="search" i]')].filter(visible);
    const input=inputs[0]||null;
    const rect=input?.getBoundingClientRect();
    const loginFormVisible=[...document.querySelectorAll('input[type="password"], [role="dialog"] input[type="email"], [role="dialog"] input[autocomplete="username"]')].some(e=>{if(typeof e.getBoundingClientRect!=='function')return false;const r=e.getBoundingClientRect(),s=getComputedStyle(e);return r.width>0&&r.height>0&&s.visibility!=='hidden'&&s.display!=='none'});
    return {
      ...(${inspectSearchReadiness.toString()})(),
      loginFormVisible,
      url:location.href,documentURI:document.documentURI,title:document.title,readyState:document.readyState,
      text:(document.body?.innerText||'').slice(0,2000),searchReady:Boolean(input),
      searchEnabled:Boolean(input&&!input.disabled&&!input.readOnly),
      visibleGoodsCount:document.querySelectorAll('a[href*="goods_id="],a[href*="goodsId="],a[href*="-g-"]').length,
      inputRect:rect?{x:rect.x,y:rect.y,width:rect.width,height:rect.height}:null,
    };
  `);
  if (state && typeof state === 'object') return state;
  let currentUrl = '';
  try { currentUrl = String(await wd('GET', '/url') || ''); } catch {}
  return { url: currentUrl, title: '', readyState: '', text: '', searchReady: false, searchEnabled: false, visibleGoodsCount: 0, inputRect: null, transientStateUnavailable: true };
}

async function findVisibleSeeMore() {
  // Unlike findSeeMore(), this probe must never call scrollIntoView. It is
  // used after positioning from the learned bottom offset so an off-screen
  // duplicate cannot jump the browser to the footer.
  return execute(`
   const vis=e=>{const s=getComputedStyle(e),r=e.getBoundingClientRect();return s.display!=='none'&&s.visibility!=='hidden'&&r.width>2&&r.height>2&&r.bottom>0&&r.top<innerHeight};
   const nodes=[...document.querySelectorAll('button,a,[role=button],div,span')].filter(e=>/^\\s*(See more|查看更多|顯示更多|显示更多|加载更多|載入更多|展開更多|展开更多|Try again|重试|重試|再试一次|再試一次|重新加载|重新載入)\\s*$/i.test(e.textContent||''));
   for(const n of nodes.sort((a,b)=>a.children.length-b.children.length)){
    let candidate=null;
    for(let e=n,i=0;i<5&&e;i++,e=e.parentElement){const s=getComputedStyle(e);if(e.matches('button,a,[role=button]')||e.tabIndex>=0||s.cursor==='pointer'){candidate=e;break}}
    if(candidate&&vis(candidate))return candidate;
   }
   return null;`);
}

async function findSeeMoreFromCurrentDom() {
  let element = await findVisibleSeeMore();
  if (element?.[EKEY]) return element;
  // The page height changes after an expansion. A previously learned offset
  // can point above a real, off-screen control; use its current DOM position.
  const control = await execute(`
    const nodes=[...document.querySelectorAll('button,a,[role=button],div,span')].filter(e=>/^\\s*(See more|查看更多|顯示更多|显示更多|加载更多|載入更多|展開更多|展开更多)\\s*$/i.test(e.textContent||''));
    for(const n of nodes.sort((a,b)=>a.children.length-b.children.length)){
      for(let e=n,i=0;i<5&&e;i++,e=e.parentElement){
        const s=getComputedStyle(e),r=e.getBoundingClientRect();
        if((e.matches('button,a,[role=button]')||e.tabIndex>=0||s.cursor==='pointer')&&s.display!=='none'&&s.visibility!=='hidden'&&r.width>2&&r.height>2)
          return {top:r.top,height:r.height};
      }
    }
    return null;`);
  if (control && Number.isFinite(control.top)) {
    const delta = Math.round(control.top + control.height / 2 - 0.5 * (await lightPageState()).innerHeight);
    if (Math.abs(delta) <= 3000) {
      await execute(`window.scrollBy({top:arguments[0],left:0,behavior:'instant'});return true;`, [delta]);
      await sleep(450);
      element = await findVisibleSeeMore();
      if (element?.[EKEY]) {
        await appendEvent('SEE_MORE_REPOSITIONED_FROM_DOM', {round, delta_px:delta, unique_goods:products.size});
        return element;
      }
    }
  }
  if (!element?.[EKEY]) {
    await appendEvent('LEARNED_OFFSET_CONTROL_MISSING', {
      round,
      unique_goods: products.size,
      see_more_bottom_offset_px: Math.round(seeMoreBottomOffset || 0),
      visible_control_in_dom: Boolean(control),
      footer_fallback_disabled: true,
    });
  }
  return element;
}

class InitialProductsStopped extends Error {}
let initialProductsWait = null;
let emptyResultsWait = null;
let lastInitialWaitingSnapshot=0;

function confirmedLoginRedirect(state) {
  if (classify(state) === 'HUMAN_VERIFICATION') return false;
  try { return /^\/login(?:\.html)?$/.test(new URL(String(state?.url || '')).pathname); }
  catch { return false; }
}

async function guardInitialProducts(state, phase) {
  if (!httpBlock && classify(state) === 'HTTP_BLOCK') {
    // A navigation may commit between the DOM sample and its classification.
    // Confirm a text-only error once, without submitting or navigating again.
    const previous=state;
    state=await bootstrapPageState();
    await appendEvent('DOM_HTTP_BLOCK_RECHECK', {phase,
      previous_url:previous.url||null,previous_title:String(previous.title||'').slice(0,200),
      url:state.url||null,title:String(state.title||'').slice(0,200),
      classification:classify(state),network_http_status:null});
  }
  if (httpBlock && [403,429].includes(Number(httpBlock.status)) && !classify(state)
      && !(Number(state.visibleGoodsCount)>0 || state.rows?.length>0)) {
    const transition=await observeHttpPageTransition({state,block:httpBlock,readState:bootstrapPageState,sleep,
      onWaiting:async details=>{
        await appendEvent('HTTP_PAGE_TRANSITION_OBSERVATION', {phase,...details});
        await writeSnapshot('WAITING_EMPTY_SEARCH_STABLE', {phase,stability_reason:'HTTP_PAGE_TRANSITION',
          browser_preserved:true,manual_action_required:false,...details});
      }});
    state=transition.state;
    await appendEvent('HTTP_PAGE_TRANSITION_RESULT', {phase,reason:transition.reason,waited_ms:transition.waitedMs});
  }
  humanVerificationActive = classify(state) === 'HUMAN_VERIFICATION';
  // A displayed challenge replaces the preceding failed search request.
  // Do not reuse that old HTTP status after the human completes verification.
  if (supersededByVerification({...state, verificationVisible:humanVerificationActive}, httpBlock)) {
    await appendEvent('HTTP_SIGNAL_SUPERSEDED_BY_VERIFICATION', { phase, ...httpBlock });
    httpBlock = null;
  }
  // CAPTCHA remains in the same window. Explicit login hands control to the supervisor.
  // Restore the bath-towel collector's page-first decision: a usable product
  // page supersedes a prior request failure; a real login/error page does not.

  const terminal = terminalCollectionStop(state, httpBlock);
  if (terminal && (/^STOPPED_HTTP_/.test(terminal.status) || terminal.status === 'STOPPED_LOGIN_REQUIRED')) {
    finalStatus = terminal.status;
    await appendEvent(finalStatus, { phase, ...terminal, page_url:state.url||null,
      page_title:String(state.title||'').slice(0,200),page_classification:classify(state),
      network_http_status:httpBlock?.status||null });
    await writeSnapshot(finalStatus, { phase, browser_preserved: true, automatic_retry: false, ...terminal, confirmed_login_redirect: confirmedLoginRedirect(state) });
    throw new InitialProductsStopped(finalStatus);
  }
  let targetSearch = false;
  const emptyDecision=decideEmptyQueryResults({state,query:QUERY,previous:emptyResultsWait,nowMs:Date.now()});
  emptyResultsWait=emptyDecision.clock;
  if(products.size===0 && emptyDecision.confirmed) {
    finalStatus='KEYWORD_NO_RESULTS';
    const details={phase,manual_action_required:false,browser_preserved:true,
      completion_basis:'EXPLICIT_EMPTY_QUERY_RESULTS',stop_reason:'EXPLICIT_EMPTY_QUERY_RESULTS',
      completion_scope:'TEMU_US_VISIBLE_SEARCH_RESULTS_FOR_SINGLE_QUERY',finished_at:now(),
      no_results_evidence:{confirmed:true,protocol:'VISIBLE_EMPTY_RESULTS_STABLE_V2',loading:false,
        stable_ms:emptyDecision.stableMs,samples:emptyDecision.samples,query:QUERY,url:state.url,text:String(state.text||'').slice(0,2000),observed_at:now()}};
    await appendEvent(finalStatus,details);await writeSnapshot(finalStatus,details);
    throw new InitialProductsStopped(finalStatus);
  }
  try { targetSearch = new URL(String(state.url || '')).searchParams.get('search_key')?.toLowerCase() === QUERY.toLowerCase(); } catch {}
  const decision = decideInitialProductsWait({ pageState: state, previous: initialProductsWait,
    nowMs: Date.now(), timeoutMs: INITIAL_PRODUCTS_TIMEOUT_SECONDS * 1000,
    productsReady: targetSearch && (Number(state.visibleGoodsCount) > 0 || state.rows?.length > 0) });
  initialProductsWait = decision.clock;
  if(decision.status==='WAITING_INITIAL_PRODUCTS'&&Date.now()-lastInitialWaitingSnapshot>=2000){
    lastInitialWaitingSnapshot=Date.now();
    await writeSnapshot('WAITING_INITIAL_PRODUCTS',{phase,browser_preserved:true,manual_action_required:false,
      initial_products_wait_ms:decision.clock.activeWaitMs,initial_products_timeout_seconds:INITIAL_PRODUCTS_TIMEOUT_SECONDS,
      loading_placeholders:state.loadingPlaceholderCount||0,
      loading_reason:state.loading?'PAGE_LOADING':emptyDecision.clock?.candidate?'CONFIRMING_EMPTY_RESULTS':'WAITING_PRODUCT_CARDS'});
  }
  if (decision.stop) {
    finalStatus = decision.status;
    const details = { phase, reason: decision.reason, stop_reason: decision.reason,
      manual_action_required: decision.reason !== 'INITIAL_PRODUCTS_TIMEOUT', browser_preserved: true,
      automatic_retry: decision.reason === 'INITIAL_PRODUCTS_TIMEOUT',
      confirmed_login_redirect: confirmedLoginRedirect(state),
      initial_products_wait_ms: decision.clock.activeWaitMs,
      initial_products_timeout_seconds: INITIAL_PRODUCTS_TIMEOUT_SECONDS, finished_at: now() };
    await appendEvent(finalStatus, details);
    await writeSnapshot(finalStatus, details);
    throw new InitialProductsStopped(finalStatus);
  }
  return decision;
}

async function navigateConfirmed(url, expected, label) {
  let lastState = null;
  const inspect = async () => {
    lastState = await bootstrapPageState();
    const decision = await guardInitialProducts(lastState, 'bootstrap_navigation');
    if (decision.manual) {
      await appendEvent('BOOTSTRAP_VERIFICATION_HANDOFF', {label, browser_preserved:true});
      return {accepted:true, url:String(lastState.url || '')};
    }
    return {accepted:expected.test(String(lastState.url || '')), url:String(lastState.url || '')};
  };
  for (let attempt = 1; attempt <= 2; attempt += 1) {
    try { await wd('POST', '/url', { url }); }
    catch (error) {
      if(error?.kind==='TIMEOUT')throw error; // uncertain queued command: no inspect or resend
      const current = await inspect();
      if (current.accepted) return current.url;
      await appendEvent('BOOTSTRAP_NAVIGATION_COMMAND_ERROR', {label, attempt, error_type:error.name || 'Error'});
    }
    // Observe readiness, rather than sleeping for a fixed period. A fresh
    // browser may need more than five seconds to commit its first navigation.
    for (let tick = 0; tick < 80; tick += 1) {
      await sleep(250);
      const current = await inspect();
      if (current.accepted) return current.url;
    }
    await appendEvent('BOOTSTRAP_NAVIGATION_RETRY', { label, attempt, destination: url });
  }
  const current = await inspect();
  if (current.accepted) return current.url;
  finalStatus = 'STOPPED_BOOTSTRAP_NAVIGATION';
  const details = {label, phase:'bootstrap_navigation', max_attempts:2,
    last_page:lastState?.url === 'newtab' ? 'newtab' : endpoint(lastState?.url),
    browser_preserved:true, keyword_complete:false, automatic_recovery_eligible:true,
    resume_requires_session_rotation:true};
  await appendEvent(finalStatus, details);
  await writeSnapshot(finalStatus, details);
  throw new InitialProductsStopped(`${label} navigation did not commit`);
}

async function submitHomeSearch() {
  const checkpoint = async (phase, originalUrl = null) => {
    const page = await bootstrapPageState();
    const decision = await guardInitialProducts(page, phase);
    let reason = decision.manual ? 'VERIFICATION_PENDING' : null;
    if (!reason && (page.transientStateUnavailable || page.readyState === 'loading')) reason = 'PAGE_TRANSITIONING';
    if (!reason && decideSearchSubmission('', page.url, QUERY) === 'SKIP_ALREADY_TARGET_URL') reason = 'SKIP_ALREADY_TARGET_URL';
    if (!reason && originalUrl) {
      try {
        const previous = new URL(originalUrl), current = new URL(page.url);
        if (previous.origin !== current.origin || previous.pathname !== current.pathname) reason = 'SEARCH_PAGE_CHANGED';
      } catch { reason = 'PAGE_TRANSITIONING'; }
    }
    if (!reason) return null;
    await appendEvent('SEARCH_SUBMISSION_DEFERRED', {phase, reason});
    return {submitted:false, method:reason === 'SKIP_ALREADY_TARGET_URL' ? reason : 'DEFERRED', reason};
  };
  const initialCheckpoint = await checkpoint('search_submission_before_input');
  if (initialCheckpoint) return initialCheckpoint;
  const state = await execute(`
    const visible = element => {
      const r=element.getBoundingClientRect(),s=getComputedStyle(element);
      return r.width>0&&r.height>0&&s.visibility!=='hidden'&&s.display!=='none';
    };
    const input=[...document.querySelectorAll('input[type="search"],input[placeholder*="search" i],input[aria-label*="search" i]')].find(visible);
    if(!input)return {element:null,value:'',url:location.href,focused:false};
    input.scrollIntoView({block:'center',inline:'center'});
    input.click();
    input.focus();
    input.select();
    return {element:input,value:input.value||'',url:location.href,focused:document.activeElement===input};
  `);
  if (!state?.element?.[EKEY]) return { submitted: false, reason: 'NO_VISIBLE_SEARCH_INPUT' };
  if (!state.focused) return { submitted: false, reason: 'SEARCH_INPUT_NOT_FOCUSED' };
  const plan = decideSearchSubmission(state.value, state.url, QUERY);
  if (plan === 'SKIP_ALREADY_TARGET_URL') return { submitted: false, method: plan };
  const enter = '\uE007';
  const control = '\uE009';
  const backspace = '\uE003';
  const stroke = value => [{ type: 'keyDown', value }, { type: 'keyUp', value }];
  const actions = plan === 'ENTER_EXISTING_QUERY'
    ? []
    : [
        { type: 'keyDown', value: control },
        ...stroke('a'),
        { type: 'keyUp', value: control },
        ...stroke(backspace),
        ...[...QUERY].flatMap(stroke),
      ];
  await wd('POST', '/actions', { actions: [{ type: 'key', id: 'temu-search-keyboard', actions }] });
  await wd('DELETE', '/actions');
  const typed = await execute(`
    const input=arguments[0];
    const visible=e=>{const r=e.getBoundingClientRect(),s=getComputedStyle(e);return r.width>3&&r.height>3&&s.visibility!=='hidden'&&s.display!=='none';};
    const form=input.closest('form[role="search"],form');
    const button=[...(form||input.parentElement||document).querySelectorAll('button,[role="button"],input[type="submit"]')]
      .find(e=>visible(e)&&!e.closest('[aria-hidden="true"]')&&!e.disabled&&e.getAttribute('aria-disabled')!=='true'
        &&(e.getAttribute('type')==='submit'||/^(?:submit search|search|提交搜索请求|提交搜尋請求|提交搜索|提交搜尋|搜索|搜尋)$/i.test((e.getAttribute('aria-label')||e.getAttribute('title')||'').trim())))||null;
    return {value:input.value,focused:document.activeElement===input,button,
      button_info:button?{tag:button.tagName,role:button.getAttribute('role'),label:button.getAttribute('aria-label')}:null};
  `, [state.element]);
  await appendEvent('SEARCH_SUBMISSION_PREPARED', {query:QUERY,mode:SEARCH_SUBMISSION,
    input_value:typed.value,focused:typed.focused,button_info:typed.button_info});
  if (String(typed.value || '').trim().toLowerCase() !== QUERY.toLowerCase()) {
    return {submitted:false,reason:'SEARCH_INPUT_VALUE_MISMATCH',input_value:typed.value};
  }
  // Input and navigation are asynchronous: a challenge or restored target
  // search can arrive while native keys are being sent. Never submit blindly
  // to that new page, even when the previous input/button is still in the DOM.
  const finalCheckpoint = await checkpoint('search_submission_before_click', state.url);
  if (finalCheckpoint) return finalCheckpoint;
  let submissionMode = SEARCH_SUBMISSION;
  if (submissionMode === 'icon' && !typed.button?.[EKEY]) {
    await appendEvent('SEARCH_ICON_UNAVAILABLE', {fallback:null,query:QUERY});
    return {submitted:false,method:'DEFERRED',reason:'SEARCH_ICON_UNAVAILABLE'};
  }
  if (submissionMode === 'icon') {
    await wd('POST', `/element/${typed.button[EKEY]}/click`, {});
  } else {
    await wd('POST', '/actions', {actions:[{type:'key',id:'temu-search-keyboard',actions:stroke(enter)}]});
    await wd('DELETE', '/actions');
  }
  const method = submissionMode === 'icon'
    ? (plan === 'ENTER_EXISTING_QUERY' ? 'CLICK_EXISTING_QUERY' : 'REPLACE_AND_CLICK') : plan;
  await appendEvent('SEARCH_SUBMISSION_ACTION_SENT', {query:QUERY,mode:submissionMode,method});
  for (let tick = 0; tick < 20; tick += 1) {
    await sleep(250);
    const committed = await execute(`
      const input=document.querySelector('input[type="search"]');
      return {url:location.href,value:input?.value||''};
    `);
    try {
      if (new URL(String(committed.url || '')).searchParams.get('search_key')?.toLowerCase() === QUERY.toLowerCase()) {
        return { submitted: true, committed: true, method, previous_value: String(state.value || '').slice(0, 80) };
      }
    } catch {}
  }
  return { submitted: false, committed: false, method, reason: 'SEARCH_URL_DID_NOT_COMMIT' };
}

async function waitForStableEmptySearch(reason = 'INITIAL', reuseSearchPage = false) {
  let stableTicks = 0;
  let targetReadyTicks = 0;
  let previous = null;
  let disabledSince = null;
  let disabledReloaded = false;
  while (true) {
    const state = await bootstrapPageState();
    const decision = await guardInitialProducts(state, 'empty_search_stability');
    if (decision.manual) {
      await writeSnapshot(decision.status === 'WAITING_LOGIN' ? 'WAITING_LOGIN' : 'WAITING_EMPTY_SEARCH_VERIFICATION', { browser_preserved: true, manual_action_required: true, stability_reason: reason });
      stableTicks = 0; targetReadyTicks = 0; previous = null;
      disabledSince = null;
      await sleep(500);
      continue;
    }
    // A failed page asset can leave the real search box disabled indefinitely.
    // Reload this empty page once, retaining the same session and timeout budget.
    // The guard above owns verification/HTTP blocks; never reload those pages.
    const disabledEmpty = !classify(state) && !httpBlock && state.searchEnabled === false
      && !Number(state.visibleGoodsCount)
      && isStableEmptySearchSample({...state, searchEnabled:true});
    disabledSince = disabledEmpty ? (disabledSince ?? Date.now()) : null;
    if (!disabledReloaded && disabledSince !== null && Date.now()-disabledSince >= 30000) {
      disabledReloaded = true;
      await appendEvent('DISABLED_SEARCH_BOX_RELOAD', {reason:'EMPTY_SEARCH_INPUT_DISABLED', same_session:true, max_reloads:1});
      await navigateConfirmed(TEMU_EMPTY_SEARCH, TEMU_EMPTY_SEARCH_URL, 'DISABLED_EMPTY_SEARCH');
      stableTicks = 0; previous = null; disabledSince = null;
      continue;
    }
    let targetSearch = false;
    try { targetSearch = new URL(String(state.url || '')).searchParams.get('search_key')?.toLowerCase() === QUERY.toLowerCase(); } catch {}
    if (targetSearch && Number(state.visibleGoodsCount) > 0) targetReadyTicks += 1;
    else targetReadyTicks = 0;
    if (targetReadyTicks >= 3) {
      await appendEvent('TARGET_SEARCH_STABLE', { reason, stable_checks: targetReadyTicks, visible_goods: state.visibleGoodsCount, recovered_or_manual: true });
      return { ...state, targetAlreadyReady: true };
    }
    if (targetSearch) {
      stableTicks = 0; previous = null;
      await writeSnapshot('WAITING_TARGET_SEARCH_RESULTS', { browser_preserved: true, manual_action_required: false, stability_reason: reason, target_ready_checks: targetReadyTicks });
      await sleep(500);
      continue;
    }
    const usable = isStableEmptySearchSample(state) || (reuseSearchPage && isStableExistingSearchSample(state));
    if (usable && previous && sameSearchInputGeometry(previous, state)) stableTicks += 1;
    else stableTicks = usable ? 1 : 0;
    if (stableTicks >= 3) {
      await appendEvent('EMPTY_SEARCH_STABLE', { reason, stable_checks: stableTicks, input_rect: state.inputRect });
      return state;
    }
    previous = state;
    await writeSnapshot('WAITING_EMPTY_SEARCH_STABLE', { browser_preserved: true, manual_action_required: false, stability_reason: reason, stable_checks: stableTicks });
    await sleep(500);
  }
}

async function waitForPostVerificationSearchBox() {
  let usableTicks = 0;
  let stableTicks = 0;
  let targetReadyTicks = 0;
  let previous = null;
  let emptySearchAnnounced = false;
  const started = Date.now();
  while (true) {
    const state = await bootstrapPageState();
    const decision = await guardInitialProducts(state, 'post_verification_search_box');
    if (decision.manual) {
      usableTicks = 0; stableTicks = 0; targetReadyTicks = 0; previous = null;
      await writeSnapshot(decision.status === 'WAITING_LOGIN' ? 'WAITING_LOGIN' : 'WAITING_SEARCH_VERIFICATION', { browser_preserved: true, manual_action_required: true });
      await sleep(500);
      continue;
    }
    let targetSearch = false;
    try { targetSearch = new URL(String(state.url || '')).searchParams.get('search_key')?.toLowerCase() === QUERY.toLowerCase(); } catch {}
    if (targetSearch && Number(state.visibleGoodsCount) > 0) targetReadyTicks += 1;
    else targetReadyTicks = 0;
    if (targetReadyTicks >= 3) {
      await appendEvent('TARGET_SEARCH_STABLE', { reason: 'POST_VERIFICATION_MANUAL_TARGET', stable_checks: targetReadyTicks, visible_goods: state.visibleGoodsCount, recovered_or_manual: true });
      return { ...state, targetAlreadyReady: true };
    }
    if (targetSearch) {
      await writeSnapshot('WAITING_TARGET_SEARCH_RESULTS', { browser_preserved: true, manual_action_required: false, stability_reason: 'POST_VERIFICATION_MANUAL_TARGET', target_ready_checks: targetReadyTicks });
      await sleep(500);
      continue;
    }
    const usable = isPostVerificationEmptySearchSample(state);
    if (usable && !emptySearchAnnounced) {
      emptySearchAnnounced = true;
      await appendEvent('POST_VERIFICATION_EMPTY_SEARCH_DETECTED', {
        url: state.url,
        input_value_ignored: true,
        extra_query_params_ignored: true,
      });
    }
    usableTicks = usable ? usableTicks + 1 : 0;
    stableTicks = usable && previous && sameSearchInputGeometry(previous, state) ? stableTicks + 1 : usable ? 1 : 0;
    if (stableTicks >= 3 || (usableTicks >= 3 && Date.now() - started >= 3000)) {
      const readiness = stableTicks >= 3 ? 'GEOMETRY_STABLE' : 'VISIBLE_ENABLED_FALLBACK';
      await appendEvent('POST_VERIFICATION_SEARCH_BOX_READY', { readiness, usable_checks: usableTicks, stable_checks: stableTicks, input_rect: state.inputRect });
      return state;
    }
    previous = state;
    await writeSnapshot('WAITING_POST_VERIFICATION_SEARCH_BOX', { browser_preserved: true, manual_action_required: false, usable_checks: usableTicks, stable_checks: stableTicks });
    await sleep(500);
  }
}

async function bootstrapThroughEmptySearch({ navigate = true, reason = 'INITIAL_EMPTY_SEARCH', reuseSearchPage = false } = {}) {
  // Fresh environments use the empty search page; keyword changes reuse results.
  const current = await bootstrapPageState();
  if (!navigate && decideSearchSubmission('', current.url, QUERY) === 'SKIP_ALREADY_TARGET_URL'
      && Number(current.visibleGoodsCount) > 0) return;
  if (!reuseSearchPage && (navigate || !TEMU_EMPTY_SEARCH_URL.test(String(current.url || '')))) {
    await appendEvent('EMPTY_SEARCH_BOOTSTRAP_NAVIGATION', { destination: TEMU_EMPTY_SEARCH, image_policy: bidiStatus });
    await navigateConfirmed(TEMU_EMPTY_SEARCH, TEMU_EMPTY_SEARCH_URL, 'TEMU_EMPTY_SEARCH');
  }
  const stableState = reason === 'POST_VERIFICATION_EMPTY_SEARCH'
    ? await waitForPostVerificationSearchBox()
    : await waitForStableEmptySearch(reason, reuseSearchPage);
  if (stableState.targetAlreadyReady) return;
  let submitted;
  try { submitted = await submitHomeSearch(); }
  catch (error) { submitted = { submitted: false, method: 'ACTION_ERROR', message: String(error).slice(0, 300) }; }
  await appendEvent('EMPTY_SEARCH_QUERY_SUBMITTED', { query: QUERY, ...submitted });
  let retried = false;
  let verificationInterruptedSearch = false;
  let targetReadyTicks = 0;
  while (true) {
    const state = await bootstrapPageState();
    const decision = await guardInitialProducts(state, 'search_submission');
    if (decision.manual) {
      verificationInterruptedSearch = true;
      targetReadyTicks = 0;
      await writeSnapshot(decision.status === 'WAITING_LOGIN' ? 'WAITING_LOGIN' : 'WAITING_SEARCH_VERIFICATION', { browser_preserved: true, manual_action_required: true });
      await sleep(500);
      continue;
    }
    let targetSearch = false;
    try { targetSearch = new URL(String(state.url || '')).searchParams.get('search_key')?.toLowerCase() === QUERY.toLowerCase(); } catch {}
    if (targetSearch && Number(state.visibleGoodsCount) > 0) targetReadyTicks += 1;
    else targetReadyTicks = 0;
    if (targetReadyTicks >= 3) {
      await appendEvent('TARGET_SEARCH_STABLE', { stable_checks: targetReadyTicks, visible_goods: state.visibleGoodsCount });
      return;
    }
    if (verificationInterruptedSearch && !targetSearch && /^https:\/\/(?:www\.)?temu\.com\/search_result\.html(?:[?#].*)?$/i.test(String(state.url || ''))) {
      verificationInterruptedSearch = false;
      if (reuseSearchPage) {
        await execute(`window.scrollTo({top:0,left:0,behavior:'instant'});return true;`);
        await waitForStableEmptySearch('POST_VERIFICATION_CURRENT_SEARCH', true);
        submitted = await submitHomeSearch();
        await appendEvent('CURRENT_SEARCH_RESUBMITTED_AFTER_VERIFICATION', {query:QUERY,...submitted});
        continue;
      }
      await appendEvent('SEARCH_STATE_RESET_AFTER_VERIFICATION', { destination: TEMU_EMPTY_SEARCH });
      await navigateConfirmed(TEMU_EMPTY_SEARCH, TEMU_EMPTY_SEARCH_URL, 'POST_VERIFICATION_EMPTY_SEARCH');
      const recoveredState = await waitForPostVerificationSearchBox();
      if (recoveredState.targetAlreadyReady) return;
      submitted = await submitHomeSearch();
      await appendEvent('EMPTY_SEARCH_QUERY_RESUBMITTED_AFTER_VERIFICATION', { query: QUERY, ...submitted });
      continue;
    }
    if (!retried && !targetSearch && /^https:\/\/(?:www\.)?temu\.com\/search_result\.html(?:[?#].*)?$/i.test(String(state.url || ''))) {
      retried = true;
      if (reuseSearchPage) {
        await execute(`window.scrollTo({top:0,left:0,behavior:'instant'});return true;`);
        await waitForStableEmptySearch('UNCOMMITTED_CURRENT_SEARCH', true);
        await submitHomeSearch();
        await appendEvent('CURRENT_SEARCH_RESUBMITTED', {query:QUERY});
        continue;
      }
      await navigateConfirmed(TEMU_EMPTY_SEARCH, TEMU_EMPTY_SEARCH_URL, 'UNCOMMITTED_EMPTY_SEARCH');
      const retryState = await waitForStableEmptySearch('UNCOMMITTED_SEARCH_RETRY');
      if (retryState.targetAlreadyReady) return;
      try { await submitHomeSearch(); } catch {}
      continue;
    }
    await sleep(1500);
  }
}

async function bootstrapThroughHome() {
  let state = await bootstrapPageState();
  if (!TEMU_HOME_URL.test(String(state.url || ''))) {
    await appendEvent('HOME_SEARCH_BOOTSTRAP_NAVIGATION', {destination:TEMU_HOME});
    await navigateConfirmed(TEMU_HOME,TEMU_HOME_URL,'TEMU_HOME_SEARCH');
  }
  let previous=null, stableChecks=0;
  while (true) {
    state=await bootstrapPageState();
    const decision=await guardInitialProducts(state,'home_search_stability');
    const usable=!decision.manual && !state.loginFormVisible && isStableHomeSearchSample(state);
    stableChecks=usable && previous && sameSearchInputGeometry(previous,state) ? stableChecks+1 : usable ? 1 : 0;
    previous=state;
    if (stableChecks>=3) break;
    await writeSnapshot('WAITING_HOME_SEARCH_STABLE',{manual_action_required:decision.manual,stable_checks:stableChecks});
    await sleep(500);
  }
  const submitted=await submitHomeSearch();
  await appendEvent('HOME_SEARCH_QUERY_SUBMITTED',{query:QUERY,...submitted});
}

const bidi = await startBidi();
browserControlCleanup=bidi?.close || null;
let shuttingDown = false;
async function boundedShutdown(code, error) {
  if (shuttingDown) return;
  shuttingDown = true;
  const deadline = setTimeout(() => process.exit(code), 14000);
  try { if (error) console.error(String(error)); if (bidi) await bidi.close(); }
  finally { clearTimeout(deadline); process.exit(code); }
}
process.once('SIGINT', () => void boundedShutdown(130));
process.once('SIGTERM', () => void boundedShutdown(143));
process.once('uncaughtException', error => void boundedShutdown(1, error));
process.once('unhandledRejection', error => void boundedShutdown(1, error));
const stopFile = argument('--stop-file');
if (stopFile) {
  const stopPoll = setInterval(() => {
    fs.access(stopFile).then(() => boundedShutdown(2)).catch(() => {});
  }, 250);
  stopPoll.unref();
}
if (!bidi || bidiStatus !== 'ACTIVE') {
  await appendEvent('IMAGE_BLOCK_GATE_FAILED', { bidi_status: bidiStatus, navigation_performed: false });
  throw new Error(`image blocking gate is not ACTIVE (${bidiStatus}); refusing TEMU navigation`);
}
await appendEvent('IMAGE_BLOCK_GATE_PASSED', { bidi_status: bidiStatus, navigation_performed: false });
if (BOOTSTRAP_VALIDATE_ONLY) {
  await writeSnapshot('IMAGE_BLOCK_GATE_READY', { navigation_performed: false, bootstrap_validate_only: true });
  await bidi.close();
  console.log(JSON.stringify({ status: 'IMAGE_BLOCK_GATE_READY', output: OUT, bidi_status: bidiStatus }));
  process.exit(0);
}
async function waitForInitialProducts() {
  initialProductsWait = { observedAtMs: Date.now(), activeWaitMs: 0, manual: false };
  // A resumed collector may find the user's existing verification page.
  // Bind interception, but preserve that page until the user completes it.
  let resumePage = await bootstrapPageState();
  while (classify(resumePage) === 'HUMAN_VERIFICATION') {
    await guardInitialProducts(resumePage, 'resume_verification');
    await writeSnapshot('WAITING_SEARCH_VERIFICATION', { browser_preserved: true, manual_action_required: true });
    await sleep(500);
    resumePage = await bootstrapPageState();
  }
  await guardInitialProducts(resumePage, 'resume_current_page');
  if (!bootstrapFromSafeTab && isPostVerificationEmptySearchSample(resumePage) && !classify(resumePage)) {
    await execute(`window.scrollTo({top:0,left:0,behavior:'instant'});return true;`);
    await appendEvent('RESTORED_EMPTY_SEARCH_RESUBMIT', {query:QUERY,same_profile:true});
    await bootstrapThroughEmptySearch({navigate:false,reason:'POST_VERIFICATION_EMPTY_SEARCH'});
  }
  if (bootstrapFromSafeTab) {
    const entryPlan=bootstrapEntryPlan(ENTRY_MODE,resumePage.url,QUERY);
    await appendEvent('BOOTSTRAP_ENTRY_SELECTED',{requested:ENTRY_MODE,plan:entryPlan});
    if (entryPlan==='CURRENT_SEARCH_INPUT') {
      await execute(`window.scrollTo({top:0,left:0,behavior:'instant'});return true;`);
      await appendEvent('NEXT_KEYWORD_CURRENT_SEARCH_ENTRY', {query:QUERY,previous_url:resumePage.url,navigation_performed:false});
      await bootstrapThroughEmptySearch({navigate:false,reuseSearchPage:true,reason:'NEXT_KEYWORD_CURRENT_PAGE'});
    } else if (entryPlan==='direct_search') {
      await appendEvent('DIRECT_SEARCH_BOOTSTRAP_NAVIGATION',{destination:SAFE_SOURCE});
      await navigateConfirmed(SAFE_SOURCE,/^https:\/\/(?:www\.)?temu\.com\/search_result\.html(?:[?#].*)?$/i,'TEMU_DIRECT_SEARCH');
    } else if (entryPlan==='home_search') {
      await bootstrapThroughHome();
    } else if (entryPlan!=='CURRENT_TARGET') {
    const blankDwellMs = rand(4000, 7000);
    await appendEvent('BLANK_BOOTSTRAP_DWELL', { wait_ms: blankDwellMs, reason: 'AVOID_IMMEDIATE_TEMU_NAVIGATION' });
    await sleep(blankDwellMs);
    await appendEvent('BLANK_BOOTSTRAP_NAVIGATION', { destination: TEMU_EMPTY_SEARCH, image_policy: bidiStatus });
    await bootstrapThroughEmptySearch();
    }
  }
  await ensurePageGuards(true);
  let initial = await scan();
  await guardInitialProducts(initial, 'initial_load');
  let initialCaptchaSeen = false;
  for (let attempt = 0; attempt < 15 && /temu\.com\/search/i.test(initial.url) && !(Number(initial.visibleGoodsCount) > 0 || initial.rows?.length); attempt += 1) {
    if (classify(initial) === 'HUMAN_VERIFICATION') {
      initialCaptchaSeen = true;
      await appendEvent('WAITING_CAPTCHA', { phase: 'initial_load', browser_preserved: true });
      await writeSnapshot('WAITING_CAPTCHA', { browser_preserved: true, manual_action_required: true });
    }
    await sleep(1000);
    await ensurePageGuards(false);
    initial = await scan();
    await guardInitialProducts(initial, 'initial_load');
    if (initialCaptchaSeen && /^https:\/\/(?:www\.)?temu\.com\/search_result\.html(?:[?#].*)?$/i.test(String(initial.url || ''))) {
      let hasQuery = false;
      try { hasQuery = Boolean(new URL(initial.url).searchParams.get('search_key')); } catch {}
      if (!hasQuery) {
        initialCaptchaSeen = false;
        await appendEvent('SEARCH_STATE_RESET_AFTER_VERIFICATION', { phase: 'initial_load', destination: TEMU_EMPTY_SEARCH });
        await bootstrapThroughEmptySearch({ navigate: false, reason: 'POST_VERIFICATION_EMPTY_SEARCH' });
        await ensurePageGuards(false);
        initial = await scan();
        await guardInitialProducts(initial, 'initial_load');
      }
    }
  }
  while (!(/temu\.com\/search/i.test(initial.url) && (Number(initial.visibleGoodsCount) > 0 || initial.rows?.length))) {
    const decision = await guardInitialProducts(initial, 'initial_load');
    const waitingStatus = decision.status;
    await appendEvent(waitingStatus, { phase: 'initial_load', browser_preserved: true });
    await writeSnapshot(waitingStatus, { browser_preserved: true, manual_action_required: decision.manual });
    await sleep(2000);
    await ensurePageGuards(false);
    initial = await scan();
    await guardInitialProducts(initial, 'initial_load');
  }
}
async function prepareSalesSort() {

  await writeSnapshot('SELECTING_SALES_SORT');
  const guard=async () => {
    let state=await bootstrapPageState();
    await guardInitialProducts(state, 'sales_sort');
    while (classify(state)==='HUMAN_VERIFICATION') {
      await writeSnapshot('WAITING_CAPTCHA', {phase:'sales_sort', browser_preserved:true, manual_action_required:true});
      await sleep(1000);
      state=await bootstrapPageState();
      await guardInitialProducts(state, 'sales_sort');
    }
  };
  try {
    for(let recoveryAttempt=1;recoveryAttempt<=3;recoveryAttempt++) {
    salesSort=await selectSalesSort({inspect:readSalesSort, query:QUERY, sort:SORT_REQUEST, sleep, guard,
      onAction:async action=>appendEvent('SORT_CONTROL_ACTION',action),
      onRetry:async retry=>{
        await appendEvent('RETRYING_SALES_SORT',retry);
        await writeSnapshot('RETRYING_SALES_SORT',{sort_retry:retry,manual_action_required:false});
      },
      click:element => clickSalesSortControl({element,execute,sleep,
        click:current => wd('POST', `/element/${current[EKEY]}/click`, {})})});
    if(salesSort.state==='UI_CONFIRMED')break;
    if(recoveryAttempt<3) {
      await guard();
      await appendEvent('SORT_POSITION_RECOVERY',{attempt:recoveryAttempt,requested:SORT_REQUEST});
      await writeSnapshot('RETRYING_SALES_SORT',{manual_action_required:false,sort_recovery_attempt:recoveryAttempt});
      await execute(`window.scrollTo({top:0,left:0,behavior:'instant'});return true;`);
      await sleep(1000);
    }
    }
  } catch (error) {
    if (error instanceof InitialProductsStopped) throw error;
    salesSort={requested:SORT_REQUEST,state:'UNCONFIRMED',query:QUERY,reason:'目标排序控件无法操作；暂停当前关键词采集'};
  }
  salesSortEstablished=salesSort.state==='UI_CONFIRMED';
  await appendEvent('SALES_SORT_RESULT', salesSort);
  if (!salesSortEstablished) await stopSalesSort('before_collection');
  await writeSnapshot('SALES_SORT_CHECKED');
}
async function stopSalesSort(phase) {
  finalStatus='STOPPED_SALES_SORT_UNCONFIRMED';
  initialProductsPreparing=false;
  await appendEvent(finalStatus,{phase,query:QUERY,collector_paused:true,bidi_preserved:true});
  await writeSnapshot(finalStatus,{phase,browser_preserved:true,manual_action_required:true,
    automatic_retry:false,automatic_recovery_eligible:true,resume_requires_session_rotation:false,
    collector_paused:true,bidi_preserved:true,keyword_complete:false});
  while (true) {
    const state=await bootstrapPageState();
    if (classify(state) || httpBlock) {
      const decision=await guardInitialProducts(state,'sales_sort_pause');
      if (decision.manual) {
        await writeSnapshot('WAITING_CAPTCHA',{phase:'sales_sort_pause',browser_preserved:true,
          manual_action_required:true,collector_paused:true,bidi_preserved:true});
        await sleep(rand(5000,7000));
        continue;
      }
    }
    const observed=await readSalesSort().catch(()=>({}));
    if (confirmsSalesSort(observed,QUERY)) {
      salesSort={...salesSort,...observed,requested:SORT_REQUEST,state:'UI_CONFIRMED',
        query:QUERY,observed_at:now()};
      salesSortEstablished=true;
      finalStatus=null;
      await appendEvent('SALES_SORT_RECOVERED_AFTER_PAUSE',{phase,query:QUERY,
        selected_label:observed.selected_label,confirmed_by:observed.selected_by});
      await writeSnapshot('SALES_SORT_CHECKED',{phase,collector_paused:false,bidi_preserved:true,
        sales_sort:salesSort});
      return;
    }
    await sleep(rand(10000,15000));
  }
}
async function waitForHttpVerificationRecovery(status) {
  const originalBlock=httpBlock ? {...httpBlock} : null;
  let verificationObserved=false, stableChecks=0, previousSignature=null;
  await appendEvent('TERMINAL_HTTP_PASSIVE_MONITOR_STARTED',{status,
    collection_requests_disabled:true,bidi_preserved:true});
  await writeSnapshot(status,{browser_preserved:true,collection_requests_disabled:true,
    passive_page_checks:true,bidi_preserved:true});
  while (true) {
    await sleep(verificationObserved ? 5000 : 15000);
    const state=await bootstrapPageState().catch(()=>({}));
    const pageClass=classify(state);
    humanVerificationActive=pageClass==='HUMAN_VERIFICATION';
    if (humanVerificationActive) {
      if (!verificationObserved) await appendEvent('HTTP_TERMINAL_VERIFICATION_OBSERVED',{
        status,url:state.url || null,challenge_resources_allowed:true});
      verificationObserved=true;
      stableChecks=0;previousSignature=null;
      await writeSnapshot('WAITING_HUMAN_VERIFICATION',{phase:'terminal_http',
        prior_http_status:Number(originalBlock?.status) || Number(status.replace('STOPPED_HTTP_','')) || null,
        browser_preserved:true,manual_action_required:true,collector_paused:true,
        bidi_preserved:true,challenge_resources_allowed:true});
      continue;
    }
    if (!verificationObserved) {
      await appendEvent('TERMINAL_HTTP_PASSIVE_PAGE_CHECK',{status,
        page_state:pageClass,url:state.url || null,collection_requests_disabled:true});
      continue;
    }
    const blockChanged=Boolean(httpBlock && (!originalBlock
      || httpBlock.status!==originalBlock.status || httpBlock.observed_at!==originalBlock.observed_at
      || httpBlock.endpoint!==originalBlock.endpoint));
    if (blockChanged) {
      verificationObserved=false;stableChecks=0;previousSignature=null;
      await writeSnapshot(status,{browser_preserved:true,collection_requests_disabled:true,
        passive_page_checks:true,bidi_preserved:true,new_http_failure_after_verification:true});
      continue;
    }
    const current=await scan({commit:false}).catch(()=>({}));
    const sort=await readSalesSort().catch(()=>({}));
    let targetQuery=false;
    try {
      const url=new URL(String(current.url || state.url || ''));
      targetQuery=/^(?:www\.)?temu\.com$/i.test(url.hostname)
        && url.pathname==='/search_result.html'
        && url.searchParams.get('search_key')?.trim().toLowerCase()===QUERY.trim().toLowerCase();
    } catch {}
    const rowIds=(current.rows || []).map(row=>String(row.goods_id || '')).filter(Boolean);
    const listedIds=(sort.goods_ids || []).map(String);
    const hasRealProducts=rowIds.length>0 && rowIds.some(id=>listedIds.includes(id));
    const confirmed=targetQuery && hasRealProducts && confirmsSalesSort(sort,QUERY);
    const signature=confirmed ? JSON.stringify([sort.selected_label,sort.selected_by,
      sort.goods_ids,rowIds]) : null;
    stableChecks=signature && signature===previousSignature ? stableChecks+1 : signature ? 1 : 0;
    previousSignature=signature;
    await writeSnapshot('WAITING_HUMAN_VERIFICATION',{phase:'terminal_http_recovery_confirmation',
      prior_http_status:Number(originalBlock?.status) || Number(status.replace('STOPPED_HTTP_','')) || null,
      browser_preserved:true,manual_action_required:false,verification_observed:true,
      same_query:targetQuery,real_products:hasRealProducts,sort_confirmed:confirmed,
      stable_sort_checks:stableChecks,bidi_preserved:true});
    if (stableChecks>=3) {
      return {state:current,sort,previous_http_block:originalBlock};
    }
  }
}
try { await waitForInitialProducts(); }
catch (error) { if (!(error instanceof InitialProductsStopped)) throw error; }
try { if (!finalStatus) await prepareSalesSort(); }
catch (error) { if (!(error instanceof InitialProductsStopped)) throw error; }
initialProductsPreparing=false;
try {
  // Commit the initial list only after sorting has settled. This also lets an
  // already sufficient first page satisfy the quota without clicking See more.
  if (!finalStatus && ['sales','recent'].includes(SORT_REQUEST)) await guardInitialProducts(await scan(), 'after_sales_sort');
} catch (error) { if (!(error instanceof InitialProductsStopped)) throw error; }
let collectionStarted=false;
collectionLifecycle: while (true) {
if (!finalStatus && !collectionStarted) {
  await appendEvent('STARTED', { initial_unique_goods: products.size, webdriver_session: 'REUSED', bidi_status: bidiStatus });
  trafficSavingMode = true;
  await appendEvent('TRAFFIC_SAVER_ACTIVE', {
    blocked_host_classes: ['pftka-us.temu.com', 'thtka-us.temu.com', 'adtk-us.temu.com', 'static*.kwcdn.com'],
    activation: 'AFTER_INITIAL_PRODUCTS_READY',
  });
  await writeSnapshot('RUNNING');
  collectionStarted=true;
}

try {
while (!finalStatus && (consecutiveNoNewRounds < IDLE_REQUIRED || clicksSinceRecovery < IDLE_REQUIRED) && products.size < MAX_PRODUCTS) {
  await ensurePageGuards(false);
  // A round is exactly one successful See more expansion cycle. The baseline
  // is captured before positioning/scrolling, so late rows from the preceding
  // click are credited to this round and reset the no-new streak.
  const roundBaselineIds = new Set([...products.keys(),...unclassifiedProducts.keys()]);
  let before = await visibleScrollToLearnedSeeMoreZone();
  const block = classify(before);

  if (block || httpBlock) {
    const effectiveBlock = block || 'HTTP_BLOCK';
    const httpStatus = Number(httpBlock?.status) || null;
    const terminal = terminalCollectionStop(before, httpBlock);
    if (terminal?.status === 'STOPPED_LOGIN_REQUIRED') {
      finalStatus = terminal.status;
      const details = { round, http_status: httpStatus, browser_preserved: true,
        ...terminal, confirmed_login_redirect: confirmedLoginRedirect(before) };
      await appendEvent(finalStatus, details);
      await writeSnapshot(finalStatus, details);
      break;
    }
    if (effectiveBlock !== 'HUMAN_VERIFICATION' && [403, 429].includes(httpStatus)) {
      finalStatus = `STOPPED_HTTP_${httpStatus}`;
      await appendEvent(finalStatus, { round, http_status: httpStatus });
      await writeSnapshot(finalStatus, { http_status: httpStatus, automatic_recovery_eligible: [403, 429].includes(httpStatus), resume_requires_session_rotation: [403, 429].includes(httpStatus), confirmed_login_redirect: confirmedLoginRedirect(before) });
      break;
    }
    if (effectiveBlock === 'PROXY_OR_NETWORK_FAILURE') {
      finalStatus = 'STOPPED_PROXY_CONNECTION';
      await appendEvent(finalStatus, { round, clicks, unique_goods: products.size, automatic_recovery_eligible: true });
      await writeSnapshot(finalStatus, { automatic_recovery_eligible: true, resume_requires_session_rotation: true });
      break;
    }
    if (effectiveBlock === 'ACCESS_DENIED') {
      finalStatus = 'STOPPED_ACCESS_DENIED';
      await appendEvent(finalStatus, { round, clicks, unique_goods: products.size, automatic_retry: false });
      await writeSnapshot(finalStatus, { automatic_retry: false, resume_requires_user_action: true });
      break;
    }
    // Human verification and transient login pages often recover immediately
    // after the user acts. A confirmed 429 gets one short bounded cooldown;
    // two minutes made a healthy visible page look like a stopped collector.
    const waitMs = effectiveBlock === 'HUMAN_VERIFICATION'
      ? 5000
      : effectiveBlock === 'LOGIN_REQUIRED' ? 0
      : httpStatus === 429 ? rand(30000, 45000)
      : httpStatus === 403 ? 0
      : 30000;
    await appendEvent('WAITING_HUMAN_OR_COOLDOWN', { block: effectiveBlock, http_status: httpStatus, wait_ms: waitMs, round });
    await writeSnapshot(block === 'HUMAN_VERIFICATION' ? 'WAITING_HUMAN_VERIFICATION' : block === 'LOGIN_REQUIRED' ? 'WAITING_LOGIN' : 'WAITING_HTTP_COOLDOWN');
    if (effectiveBlock === 'LOGIN_REQUIRED' || (httpStatus === 403 && effectiveBlock !== 'HUMAN_VERIFICATION')) {
      finalStatus = effectiveBlock === 'LOGIN_REQUIRED' ? 'STOPPED_LOGIN_REQUIRED' : 'STOPPED_HTTP_403';
      await appendEvent(finalStatus, { round, clicks, unique_goods: products.size, automatic_retry: false });
      await writeSnapshot(finalStatus, { automatic_retry: false, automatic_recovery_eligible: httpStatus === 403, resume_requires_session_rotation: httpStatus === 403, resume_requires_new_run: httpStatus === 403, confirmed_login_redirect: confirmedLoginRedirect(before) });
      break;
    }
    await sleep(waitMs);
    const recovered = await scan();
    if (!classify(recovered) && recovered.rows?.length) {
      httpBlock = null; clicksSinceRecovery = 0; consecutiveNoNewRounds = 0;
      await appendEvent('RECOVERED', { round, clicks_since_recovery_reset: true, no_new_rounds_reset: true });
      await writeSnapshot('RUNNING', { recovered_at: now() });
    }
    continue;
  }
  await sleep(rand(1800, 3200));
  let element = await findSeeMoreFromCurrentDom();
  if (!element?.[EKEY]) {
    element = await revealSeeMoreAboveFooter();
  }
  if (!element?.[EKEY]) {
    const firstEnd = await lightPageState();
    if (firstEnd.noMoreResultsVisible) {
      await sleep(1500);
      const secondEnd = await lightPageState();
      const sortState = await readSalesSort().catch(() => ({}));
      if (confirmedNoMoreResults(firstEnd, secondEnd, {
          query: QUERY, sortConfirmed: salesSortEstablished && confirmsSalesSort(sortState, QUERY), httpBlock })) {
        noMoreResultsConfirmed = true;
        finalStatus = 'KEYWORD_COMPLETE';
        await appendEvent('NO_MORE_RESULTS_CONFIRMED', {query:QUERY, sort:SORT_REQUEST,
          visible_goods:secondEnd.visibleGoodsCount, stable_observations:2});
        break;
      }
    }
    missingSeeMore = missingControlState(missingSeeMore, products.size, Date.now(), SEE_MORE_MISSING_CHECKS, SEE_MORE_MISSING_WAIT_MS);
    if (missingSeeMore.exhausted) {
      finalStatus = 'STOPPED_SEE_MORE_UNAVAILABLE';
      await appendEvent(finalStatus, { round, unique_goods: products.size, missing_checks: missingSeeMore.checks });
      await writeSnapshot(finalStatus, { browser_preserved: true, keyword_complete: false,
        stop_reason: 'SEE_MORE_UNAVAILABLE_WITHOUT_COMPLETION_EVIDENCE', missing_checks: missingSeeMore.checks });
      break;
    }
    await appendEvent('SEE_MORE_NOT_VISIBLE', { round, unique_goods: products.size });
    await writeSnapshot('WAITING_SEE_MORE'); await sleep(8000); continue;
  }
  missingSeeMore = null;
  const kind = await controlKind(element);
  if (kind === 'TRY_AGAIN') {
    if (!await recoverTryAgain(element)) break;
    continue;
  }
  consecutiveTryAgainErrors = 0;
  consecutiveTryAgainNoProgress = 0;
  // Add a small human-like pause only before a real See more click. Network
  // retries keep their existing timing so recovery is not needlessly delayed.
  await sleep(rand(900, 1100));
  element = (await humanJitterAtSeeMore()) || element;
  await rememberSeeMoreBottomOffset(element);
  const countBefore = products.size;
  const clickBaselineIds = new Set([...products.keys(),...unclassifiedProducts.keys()]);
  const expansionBefore=await lightPageState();
  let clickEvidence;
  try { clickEvidence=await clickVisibleControl({element,execute,wd,sleep}); }
  catch (error) { await retrySeeMoreClick(String(error).slice(0,300));continue; }
  clicks += 1;
  await appendEvent('SEE_MORE_CLICKED', { round:round+1, click: clicks, unique_before: countBefore,
    expansion_confirmed:false,...clickEvidence,clicks_since_recovery: clicksSinceRecovery,
    see_more_bottom_offset_px: Math.round(seeMoreBottomOffset || 0) });
  await sleep(rand(2800, 4300));
  const after = await waitForExpansionToSettle(countBefore);
  const blockAfterClick = classify(after);

  if (blockAfterClick || httpBlock) {
    consecutiveNoNewRounds = 0;
    await appendEvent('ROUND_INTERRUPTED', { round, click: clicks, block: blockAfterClick || httpBlock, unique_goods: products.size });
    await writeSnapshot(blockAfterClick === 'HUMAN_VERIFICATION' ? 'WAITING_HUMAN_VERIFICATION' : blockAfterClick === 'LOGIN_REQUIRED' ? 'WAITING_LOGIN' : 'WAITING_HTTP_COOLDOWN');
    continue;
  }
  // This probe must never scroll. Calling the general See-more finder here
  // would bring the next off-screen See more into view immediately.
  const postClickControl = await findVisibleTryAgain();
  if (postClickControl?.[EKEY]) {
    consecutiveNoNewRounds = 0;
    await appendEvent('ROUND_INTERRUPTED', { round, click: clicks, block: 'TRY_AGAIN_AFTER_SEE_MORE', unique_goods: products.size });
    if (!await recoverTryAgain(postClickControl, { after_see_more: true, interrupted_round: round })) break;
    continue;
  }
  const expansionAfter=await lightPageState();
  if(classify(expansionAfter))continue;
  if(!expansionObserved(expansionBefore,expansionAfter)) {
    await retrySeeMoreClick('PRODUCT_LIST_DID_NOT_EXPAND');continue;
  }
  failedSeeMoreClicks=0;
  round += 1; clicksSinceRecovery += 1;
  await appendEvent('SEE_MORE_EXPANSION_CONFIRMED',{round,click:clicks,
    before_count:expansionBefore.visibleGoodsCount,after_count:expansionAfter.visibleGoodsCount});
  const postClickNewRows = [...products.entries(),...unclassifiedProducts.entries()].filter(([id]) => !clickBaselineIds.has(id)).map(([, row]) => row);
  const roundNewRows = [...products.entries(),...unclassifiedProducts.entries()].filter(([id]) => !roundBaselineIds.has(id)).map(([, row]) => row);
  const postClickNewCount = postClickNewRows.length;
  const roundNewCount = roundNewRows.length;
  const newRelevance = categoryRelevance.summarize(roundNewRows);
  const roundNewRelevantCount = newRelevance.relevant;
  const roundNewUnclassifiedCount = newRelevance.unknown;
  consecutiveNoNewRounds = newRelevance.progress === 0 ? consecutiveNoNewRounds + 1 : 0;
  await appendEvent('ROUND_COMPLETE', {
    round, click: clicks, new_goods: roundNewCount, post_click_new_goods: postClickNewCount,
    new_relevant_goods: roundNewRelevantCount,
    new_unclassified_goods: roundNewUnclassifiedCount,
    new_progress_goods: newRelevance.progress,
    unique_goods: products.size, consecutive_no_new_rounds: consecutiveNoNewRounds,
    consecutive_no_new_relevant_rounds: consecutiveNoNewRounds,
    consecutive_no_new_clicks: consecutiveNoNewRounds, image_requests_blocked: imageBlocked,
    images_backfilled: Number(after?.imageBackfilled) || 0,
    promoted_from_pending: Number(after?.promotedFromPending) || 0,
    pending_incomplete_goods: pendingProducts.size,
  });
  await writeSnapshot('RUNNING', {
    last_round_new_goods: roundNewCount, last_post_click_new_goods: postClickNewCount,
    last_round_new_relevant_goods: roundNewRelevantCount,
    last_round_new_unclassified_goods: roundNewUnclassifiedCount,
    last_round_new_progress_goods: newRelevance.progress,
    last_round_images_backfilled: Number(after?.imageBackfilled) || 0,
    last_round_promoted_from_pending: Number(after?.promotedFromPending) || 0,
  });
  await sleep(rand(1800, 3000));
}

} catch (error) { if (!(error instanceof InitialProductsStopped)) throw error; }
if (!/^STOPPED_HTTP_(?:403|429)$/.test(String(finalStatus || ''))) break;
const stoppedStatus=finalStatus;
const recovered=await waitForHttpVerificationRecovery(stoppedStatus);
const oldHttpBlock=recovered.previous_http_block;
httpBlock=null;
humanVerificationActive=false;
finalStatus=null;
salesSort={...salesSort,...recovered.sort,requested:SORT_REQUEST,state:'UI_CONFIRMED',
  query:QUERY,observed_at:now()};
salesSortEstablished=true;
initialProductsPreparing=false;
clicksSinceRecovery=0;
consecutiveNoNewRounds=0;
const refreshed=await scan();
await guardInitialProducts(refreshed,'after_terminal_http_verification');
await appendEvent('RECOVERED_AFTER_TERMINAL_HTTP_VERIFICATION',{previous_status:stoppedStatus,
  previous_http_status:Number(oldHttpBlock?.status) || Number(stoppedStatus.replace('STOPPED_HTTP_','')) || null,
  query:QUERY,selected_label:salesSort.selected_label,stable_sort_checks:3,
  unique_goods:products.size,collection_requests_resumed:true});
await writeSnapshot('RUNNING',{recovered_after_verification:true,query:QUERY,
  http_block_cleared_after_challenge:true,sort_confirmed:true,stable_sort_checks:3});
continue collectionLifecycle;
}
finalStatus ||= products.size >= MAX_PRODUCTS ? 'KEYWORD_LIMIT_REACHED' : 'KEYWORD_COMPLETE';
if (finalStatus === 'KEYWORD_COMPLETE' || finalStatus === 'KEYWORD_LIMIT_REACHED') {
  const stopReason = finalStatus === 'KEYWORD_LIMIT_REACHED' ? 'CONFIGURED_PRODUCT_LIMIT_REACHED' : noMoreResultsConfirmed ? 'NO_MORE_RESULTS_CONFIRMED' : 'CONFIGURED_COMPLETE_ROUNDS_WITHOUT_NEW_RELEVANT_OR_UNCLASSIFIED_GOODS';
  await appendEvent('KEYWORD_COLLECTION_COMPLETE', { round, clicks, unique_goods: products.size, stop_reason: stopReason, query: QUERY });
  await writeSnapshot(finalStatus, { finished_at: now(), stop_reason: stopReason });
}
if (bidi) await bidi.close();
console.log(JSON.stringify({ status: finalStatus, output: OUT, unique_goods: products.size, clicks, imageBlocked }));
if (!['KEYWORD_COMPLETE', 'KEYWORD_LIMIT_REACHED','KEYWORD_NO_RESULTS'].includes(finalStatus)) process.exitCode = 2;
