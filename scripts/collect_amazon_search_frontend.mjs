#!/usr/bin/env node
import fs from 'node:fs/promises';
import {existsSync} from 'node:fs';
import path from 'node:path';
import { chromium } from 'playwright';
import { amazonCollectionStatus, detailComplete, resumableAmazonRecords, serializedJsonWriter, selectAmazonMainImage } from './collection_checkpoint.mjs';
import {amazonDetailBlock,runAmazonDetailQueue} from './amazon_detail_queue.mjs';
import {enrichListingImage,inspectPublicImage,inspectAmazonImage} from './amazon_public_image.mjs';
import {qualifyAmazonDetail,usefulSpecifications} from './amazon_detail_quality.mjs';
import {openAmazonDetail,openAmazonRanking,recoverableAmazonBrowser} from './amazon_browser_recovery.mjs';
import {collectRankedListing} from './amazon_listing_checkpoint.mjs';
import {inspectAmazonPage} from './amazon_page_state.mjs';
import {amazonUsdPrice} from './amazon_price.mjs';

const arg = (name, fallback = null) => {
  const index = process.argv.indexOf(name);
  return index >= 0 ? process.argv[index + 1] : fallback;
};
const root = process.cwd();
const configPath = path.resolve(arg('--config', path.join(root, 'config', 'categories', 'bath_towel_set.json')));
const config = JSON.parse(await fs.readFile(configPath, 'utf8'));
const targetCount = Number(arg('--count', config.amazon.target_count || 10));
const searchUrl = String(arg('--url', config.amazon.bestsellers_url || config.amazon.search_url || '')).trim();
const outputPath = path.resolve(arg('--output', path.join(root, 'runs', config.category_key, 'amazon', 'amazon_frontend_latest.json')));
const checkpointPath = `${outputPath}.checkpoint.json`;
const listingOnly = process.argv.includes('--listing-only');
const checkpointScope = { source_url: searchUrl, category_key: config.category_key, target_count: targetCount, detail_standard: 'LATEST_DETAIL_V2' };
let checkpoint = null;
if (!process.argv.includes('--fresh')) {
  try { checkpoint = JSON.parse(await fs.readFile(checkpointPath, 'utf8')); }
  catch (error) { if (error.code !== 'ENOENT') throw error; }
}
const resumeRecords = resumableAmazonRecords(checkpoint?.status==='COMPLETE' ? {...checkpoint,status:'RUNNING'} : checkpoint, checkpointScope);
const detailConcurrency = Number(config.amazon.detail_concurrency ?? 4);
if (!Number.isInteger(detailConcurrency) || detailConcurrency<1 || detailConcurrency>10) throw new Error('detail_concurrency must be 1..10');
const saveCheckpoint = serializedJsonWriter(checkpointPath);
const installedEdge = process.platform==='win32' ? [process.env['ProgramFiles(x86)'],process.env.ProgramFiles]
  .filter(Boolean).map(directory=>path.join(directory,'Microsoft/Edge/Application/msedge.exe')).find(existsSync) : null;
const edge = arg('--browser', process.env.AMAZON_BROWSER_PATH || installedEdge || null);
if (!searchUrl.startsWith('https://www.amazon.com/')) throw new Error('Amazon US HTTPS search URL is required');
if (!(targetCount >= 1 && targetCount <= 100)) throw new Error('--count must be between 1 and 100');

const clean = value => String(value || '').replace(/\s+/g, ' ').trim();
const isHttps = value => { try { return new URL(value).protocol === 'https:'; } catch { return false; } };
const canonical = asin => `https://www.amazon.com/dp/${asin}`;

async function collectSearchCards(page) {
  const response = await page.goto(searchUrl, { waitUntil: 'domcontentloaded', timeout: 60000 });
  if (!response || response.status() >= 400) throw new Error(`Amazon search HTTP ${response?.status() ?? 'unknown'}`);
  await page.locator('[data-component-type="s-search-result"][data-asin]').first().waitFor({ timeout: 25000 });
  for (let pass = 0; pass < 6; pass += 1) {
    await page.evaluate(() => window.scrollTo(0, document.body.scrollHeight));
    await page.waitForTimeout(700);
  }
  return page.locator('[data-component-type="s-search-result"][data-asin]').evaluateAll((cards, limit) => cards.slice(0, limit).map((card, index) => {
    const text = selector => (card.querySelector(selector)?.textContent || '').replace(/\s+/g, ' ').trim();
    const asin = (card.getAttribute('data-asin') || '').toUpperCase();
    const titleLink = card.querySelector('h2 a[href*="/dp/"], a.a-link-normal.s-no-outline[href*="/dp/"]');
    const title = text('h2') || card.querySelector('img')?.alt || null;
    const image = card.querySelector('img.s-image')?.currentSrc || card.querySelector('img.s-image')?.src || null;
    const priceText = text('.a-price .a-offscreen');
    const ratingText = card.querySelector('.a-icon-alt')?.textContent || '';
    const reviewText = text('a[href*="customerReviews"] span, a[href*="product-reviews"] span');
    return {
      rank: index + 1,
      asin,
      title,
      product_url: asin ? `https://www.amazon.com/dp/${asin}` : titleLink?.href || null,
      main_image_url: image,
      listing_image_url: image,
      listing_price_text: priceText,
      price_usd: /^(?:US\s*\$|USD\s*|\$)/i.test(priceText.trim()) ? Number(priceText.replace(/[^0-9.]/g, '').match(/\d+(?:\.\d+)?/)?.[0]) || null : null,
      rating: Number(ratingText.match(/\d(?:\.\d)?/)?.[0]) || null,
      review_count: Number(reviewText.replace(/[^0-9]/g, '')) || null,
      raw_card_text: (card.innerText || '').trim().slice(0, 12000),
    };
  }), targetCount);
}

async function collectBestSellerCards(page, url = searchUrl) {
  let response;
  await openAmazonRanking(page,url,{onResponse:value=>{response=value;},
    inspect:async()=>({...await inspectAmazonPage(page),http_status:response?.status() ?? null})});
  for (let pass = 0; pass < 6; pass += 1) {
    await page.evaluate(() => window.scrollTo(0, document.body.scrollHeight));
    await page.waitForTimeout(700);
  }
  return page.locator('.zg-bdg-ctr').evaluateAll((badges, limit) => badges.map(badge => {
    if (!badge.getClientRects().length || getComputedStyle(badge).display === 'none' || getComputedStyle(badge).visibility === 'hidden') return null;
    const rank = Number((badge.textContent || '').replace(/[^0-9]/g, ''));
    let card = badge.closest('.zg-grid-general-faceout');
    if (!card) {
      let parent = badge.parentElement;
      while (parent && parent !== document.body) {
        if (parent.querySelector('a[href*="/dp/"]') && parent.querySelectorAll('.zg-bdg-ctr').length === 1) { card = parent; break; }
        parent = parent.parentElement;
      }
    }
    if (!card || !rank || rank > limit) return null;
    const href = [...card.querySelectorAll('a[href*="/dp/"]')].map(node => node.href).find(Boolean) || '';
    const asin = (href.match(/\/dp\/([A-Z0-9]{10})/i)?.[1] || card.querySelector('[id]')?.id || '').toUpperCase().match(/^[A-Z0-9]{10}$/)?.[0] || null;
    const links = [...card.querySelectorAll('a[href*="/dp/"]')].map(node => ({ text: (node.innerText || '').replace(/\s+/g, ' ').trim(), href: node.href }));
    const titleLink = links.filter(item => item.text).sort((a, b) => b.text.length - a.text.length)[0] || null;
    const imageNode = card.querySelector('img.p13n-product-image, img');
    const image = imageNode?.currentSrc || imageNode?.src || imageNode?.getAttribute('data-src') || null;
    const ratingText = card.querySelector('.a-icon-alt')?.textContent || '';
    const reviewText = [...card.querySelectorAll('a[href*="product-reviews"], a[href*="customerReviews"]')].map(node => node.textContent || '').join(' ');
    const priceText = [...card.querySelectorAll('.a-color-price, [class*="p13n-sc-price"], .a-price .a-offscreen')].map(node => node.textContent || '').find(Boolean) || '';
    return {
      rank,
      asin,
      title: titleLink?.text || imageNode?.alt || null,
      product_url: asin ? `https://www.amazon.com/dp/${asin}` : titleLink?.href || null,
      main_image_url: image,
      listing_image_url: image,
      listing_price_text: priceText,
      price_usd: Number(priceText.replace(/[^0-9.]/g, '').match(/\d+(?:\.\d+)?/)?.[0]) || null,
      rating: Number(ratingText.match(/\d(?:\.\d)?/)?.[0]) || null,
      review_count: Number(reviewText.replace(/[^0-9]/g, '')) || null,
      raw_card_text: (card.innerText || '').trim().slice(0, 12000),
    };
  }).filter(Boolean).sort((a, b) => a.rank - b.rank).slice(0, limit), targetCount);
}

async function detailFacts(context, record, attempt = 1) {
  // Once detail facts exist, retry only their observed image assets.
  if(record.detail_quality?.title_verified && usefulSpecifications(record.specifications).length && record.image_candidates?.length) {
    return qualifyAmazonDetail(record,{},record.image_candidates,(url,proof)=>inspectAmazonImage(context,url,proof));
  }
  let page,response,evidence={};
  const inspectFailure=async()=>{
    if(!page) return evidence;
    const state=await inspectAmazonPage(page).catch(()=>({}));
    return evidence={http_status:response?.status() ?? null,...state};
  };
  try {
    page = await context.newPage();
    await openAmazonDetail(page,record.product_url,{inspect:inspectFailure,onResponse:value=>{response=value;}});
    await page.evaluate(() => window.scrollTo(0, document.body.scrollHeight));
    await page.waitForTimeout(700);
    const value = await page.evaluate(() => {
      const cleanText = value => (value || '').replace(/\s+/g, ' ').trim();
      const first = (...selectors) => selectors.map(selector => cleanText(document.querySelector(selector)?.textContent)).find(Boolean) || null;
      const priceText = first('#corePriceDisplay_desktop_feature_div .a-offscreen', '#corePrice_feature_div .a-offscreen', '#priceblock_ourprice');
      const rangePrices=[...document.querySelectorAll('#corePriceDisplay_desktop_feature_div .a-price-range .a-offscreen, #corePrice_feature_div .a-price-range .a-offscreen')].map(node=>cleanText(node.textContent));
      const monthlySales=first('#social-proofing-faceout-title-tk_bought, #social-proofing-faceout-title_feature_div');
      const negativeReviews=[];
      for(const review of document.querySelectorAll('[data-hook="review"]')) {
        const stars=cleanText(review.querySelector('[data-hook="review-star-rating"], [data-hook="cmps-review-star-rating"]')?.textContent);
        const rating=Number(stars.match(/^[12](?:\.\d+)?(?=\s|$)/)?.[0]);
        const text=cleanText(review.querySelector('[data-hook="review-body"]')?.textContent);
        const href=review.querySelector('a[href*="/gp/customer-reviews/"]')?.href;
        if(rating>=1&&rating<=2&&text&&href)negativeReviews.push({rating,text:text.slice(0,1000),source_url:href});
      }
      const ratingText = document.querySelector('#acrPopover')?.getAttribute('title') || first('#acrPopover .a-icon-alt');
      const reviewText = first('#acrCustomerReviewText');
      const specs = [];
      const add = (label, value) => {
        const key = cleanText(label).replace(/:$/, ''); const content = cleanText(value);
        if (key && content && !specs.some(row => row.label === key && row.value === content)) specs.push({ label: key, value: content });
      };
      document.querySelectorAll('#productDetails_detailBullets_sections1 tr, #productDetails_techSpec_section_1 tr, #productDetails_db_sections tr, table.prodDetTable tr').forEach(row => add(row.querySelector('th')?.textContent, row.querySelector('td')?.textContent));
      document.querySelectorAll('#detailBullets_feature_div li, #detailBulletsWrapper_feature_div li').forEach(item => {
        const parts = [...item.querySelectorAll('span')].map(node => cleanText(node.textContent)).filter(Boolean);
        if (parts.length >= 2) add(parts[0], parts.slice(1).join(' '));
      });
      document.querySelectorAll('#feature-bullets li span').forEach((item, index) => { if (index < 20) add(`卖点${index + 1}`, item.textContent); });
      const image = document.querySelector('#landingImage');
      const dynamic = image?.getAttribute('data-a-dynamic-image');
      let candidates = [];
      try { candidates = Object.entries(JSON.parse(dynamic || '{}')).map(([url, size]) => ({ url, size, source: '详情页动态主图' })); } catch {}
      const oldHires = image?.getAttribute('data-old-hires');
      if (oldHires) candidates.push({ url: oldHires, size: [0, 0], source: '详情页原始主图' });
      if (image?.currentSrc || image?.src) candidates.push({ url: image.currentSrc || image.src, size: [0, 0], source: '详情页显示主图' });
      candidates.sort((a, b) => Math.max(...b.size) - Math.max(...a.size));
      const preferred = candidates.find(item => Math.max(...item.size) >= 600) || candidates[0] || null;
      const variants = [];
      document.querySelectorAll('#twister [id^="variation_"] li, #twister option, #twister_feature_div li, select[id^="native_dropdown_selected_"] option').forEach(option => {
        const label = cleanText(option.getAttribute('title') || option.textContent || option.querySelector('img')?.alt);
        const asin = option.getAttribute('data-defaultasin') || option.getAttribute('data-asin') || null;
        if (label) variants.push({ label, asin });
      });
      const detailText = [...document.querySelectorAll('#productDetails_detailBullets_sections1, #productDetails_techSpec_section_1, #productDetails_db_sections, #detailBullets_feature_div, #detailBulletsWrapper_feature_div, #productFactsDesktopExpander')]
        .map(element => cleanText(element.textContent)).join(' ');
      const bsr = specs.find(entry => /best sellers rank/i.test(entry.label))?.value
        || detailText.match(/Best Sellers Rank\s*(#.+?)(?=(?:\s{2,}|Product Dimensions|Date First Available|ASIN|Customer Reviews|$))/i)?.[1]
        || null;
      return {
        title: first('#productTitle'), brand: first('#bylineInfo', '#productOverview_feature_div .po-brand .po-break-word') || specs.find(row=>/^brand(?: name)?$/i.test(row.label))?.value || null, seller: first('#sellerProfileTriggerId', '#merchantInfoFeature_feature_div'),
        availability: first('#availability'), specifications: specs, variants: variants.slice(0, 60), best_sellers_rank: bsr,
        price_usd: Number((priceText || '').replace(/[^0-9.]/g, '').match(/\d+(?:\.\d+)?/)?.[0]) || null,
        detail_price_text: priceText,
        detail_price_range_text:rangePrices.length===2?rangePrices.join(' - '):null,
        monthly_sales_text:/bought in past month/i.test(monthlySales||'')?monthlySales:null,
        negative_reviews:negativeReviews.slice(0,10),
        rating: Number((ratingText || '').match(/\d(?:\.\d)?/)?.[0]) || null,
        review_count: Number((reviewText || '').replace(/[^0-9]/g, '')) || null,
        main_image_url: preferred?.url || null,
        image_source: preferred?.source || null,
        image_width_hint: Number(preferred?.size?.[0]) || null,
        image_height_hint: Number(preferred?.size?.[1]) || null,
        image_candidates: candidates,
      };
    });
    const preferredImage = selectAmazonMainImage(value.image_candidates || []);
    if (preferredImage) {
      value.main_image_url = preferredImage.url;
      value.image_source = preferredImage.source;
      value.image_width_hint = Number(preferredImage.size?.[0]) || null;
      value.image_height_hint = Number(preferredImage.size?.[1]) || null;
    }
    const qualified = await qualifyAmazonDetail(record,value,
      [preferredImage,...(value.image_candidates || [])].filter(Boolean),(url,proof)=>inspectAmazonImage(context,url,proof));
    return {
      ...qualified,
      listing_image_url: record.listing_image_url || record.main_image_url,
      price_usd: amazonUsdPrice(value.detail_price_text) ?? record.price_usd,
      detail_price_range_text:value.detail_price_range_text,
      monthly_sales_text:value.monthly_sales_text,
      negative_reviews:value.negative_reviews,
      rating: value.rating ?? record.rating,
      review_count: value.review_count ?? record.review_count,
      detail_image_attempts: attempt,
    };
  } catch (error) {
    const observed=await inspectFailure();
    const failure=amazonDetailBlock(observed) || {code:/timeout/i.test(error.message)?'DETAIL_TIMEOUT':'DETAIL_REQUEST_FAILED',retryable:true,message:clean(error.message).slice(0,300)};
    return {
      ...record,
      specifications: record.specifications || [], variants: record.variants || [],
      image_source: record.image_source || '榜单卡片主图', image_status: record.image_status || '详情页回退',
      detail_image_attempts: attempt,
      detail_status: `详情页失败：${failure.message.slice(0,160)}`,
      detail_error: {...observed,...failure},
    };
  } finally {
    if(page) await page.close().catch(()=>{});
  }
}

const browser = recoverableAmazonBrowser(
  ()=>chromium.launch({headless:true,executablePath:edge || chromium.executablePath(),
    ...(process.env.AMAZON_BROWSER_PROXY?{proxy:{server:process.env.AMAZON_BROWSER_PROXY}}:{})}),
  {viewport:{width:1440,height:1000},locale:'en-US'}, Infinity);
try {
  const bestSellersMode = /\/zgbs\//i.test(new URL(searchUrl).pathname);
  if (config.amazon.mode === 'BEST_SELLERS_FRONTEND' && !bestSellersMode) throw new Error('Best Sellers requires a verified /zgbs/ URL; search order cannot substitute ranks');
  let cards=resumeRecords;
  if(!cards && bestSellersMode) {
    const rankingPath=`${outputPath}.listing-checkpoint.json`;
    let ranking=null;
    if(!process.argv.includes('--fresh')) {
      try {ranking=JSON.parse(await fs.readFile(rankingPath,'utf8'));}
      catch(error) {if(error.code!=='ENOENT') throw error;}
    }
    const publishListing=serializedJsonWriter(outputPath);
    cards=await collectRankedListing({scope:{source_url:searchUrl,category_key:config.category_key,target_count:targetCount},
      checkpoint:ranking,save:serializedJsonWriter(rankingPath),
      loadPage:async url=>{
        const page=await (await browser.getContext()).newPage();
        try {return await collectBestSellerCards(page,url);} finally {await page.close().catch(()=>{});}
      },
      onProgress:state=>publishListing({schema_version:'amazon-bestsellers-frontend-v1',status:state.status,
        category_key:config.category_key,target_count:targetCount,source:'VISIBLE_AMAZON_US_FRONTEND',source_url:searchUrl,
        observed_at:state.updated_at,records:state.records,record_count:state.records.length,
        complete:false,listing_complete:state.status==='LISTING_COMPLETE',details_complete:false,listing_errors:state.errors})});
  } else if(!cards) {
    const page=await (await browser.getContext()).newPage();
    try {cards=await collectSearchCards(page);} finally {await page.close().catch(()=>{});}
  }
  const unique = [...new Map(cards.filter(row => row.asin).map(row => [row.asin, row])).values()];
  if (unique.length < targetCount) throw new Error(`Amazon frontend returned ${unique.length} unique ASINs, expected ${targetCount}`);
  if (bestSellersMode) {
    const ranks = unique.slice(0, targetCount).map(row => row.rank);
    const expected = Array.from({ length: targetCount }, (_, index) => index + 1);
    if (JSON.stringify(ranks) !== JSON.stringify(expected)) throw new Error(`Best Sellers ranks are not contiguous 1-${targetCount}: ${ranks.join(',')}`);
  }
  for (const row of unique) {
    row.product_url = canonical(row.asin);
    if(Object.hasOwn(row,'listing_price_text')) row.price_usd=amazonUsdPrice(row.detail_price_text) ?? amazonUsdPrice(row.listing_price_text);
    if (!row.title || !isHttps(row.product_url) || !isHttps(row.main_image_url)) throw new Error(`invalid frontend card for ASIN ${row.asin}`);
  }
  const active = new Map(unique.slice(0,targetCount).map(row => [row.asin,row]));
  const publishJson = serializedJsonWriter(outputPath);
  const publish = (complete = false) => {
    const records = [...active.values()].sort((a,b) => a.rank-b.rank);
    const payload = {schema_version:'amazon-bestsellers-frontend-v1',
      status:listingOnly?'LISTING_COMPLETE':'RUNNING',category_key:config.category_key,target_count:targetCount,
      source:'VISIBLE_AMAZON_US_FRONTEND', source_url:searchUrl, observed_at:new Date().toISOString(),
      listing_complete:true,details_complete:records.every(detailComplete),
      record_count:records.length, complete, records, detail_queue:detailQueue};
    return publishJson(payload);
  };
  let detailQueue=null;
  const persist = status => saveCheckpoint({
    schema_version: 'amazon-detail-checkpoint-v1', scope: checkpointScope, status,
    updated_at: new Date().toISOString(), records: [...active.values()], detail_queue:detailQueue,
  });
  await publish();
  await persist(listingOnly?'LISTING_COMPLETE':'RUNNING');
  if (listingOnly) {
    console.log(JSON.stringify({status:'LISTING_COMPLETE',output:outputPath,unique_asins:active.size,details_deferred:true}));
  } else {
  const outcome=await runAmazonDetailQueue({records:[...active.values()],concurrency:detailConcurrency,
    shouldStop:async()=>{try{await fs.access(path.resolve(path.dirname(outputPath),'../STOP'));return true;}catch(error){if(error.code==='ENOENT')return false;throw error;}},
    fetchDetail:async(row,attempt)=>detailFacts(await browser.getContext(),row,attempt),
    repairImage:async row=>detailFacts(await browser.getContext(),row,row.detail_image_attempts),
    onCheckpoint:async state=>{
      for(const row of state.records) active.set(row.asin,row);
      detailQueue=state.queue;
      await persist('RUNNING');await publish();
    }});
  const records=[];
  for(const row of outcome.records){
    const enriched=await enrichListingImage(row,async url=>inspectPublicImage(await browser.getContext(),url));
    records.push(enriched);active.set(row.asin,enriched);
    await persist('RUNNING');await publish();
  }
  const missingDetails = records.filter(row => !detailComplete(row))
    .map(row => ({asin: row.asin, reason: row.detail_status, image_status: row.image_status,
      attempts:row.detail_image_attempts,queue_status:detailQueue.items.find(item=>item.asin===row.asin)?.status,error:row.detail_error || null}));
  const status = amazonCollectionStatus(records);
  const payload = {
    status,
    complete: status === 'COMPLETE',
    details_complete: missingDetails.length === 0,
    missing_details: missingDetails,
    detail_queue: detailQueue,
    schema_version: bestSellersMode ? 'amazon-bestsellers-frontend-v1' : 'amazon-search-frontend-v1',
    source: 'VISIBLE_AMAZON_US_FRONTEND', source_url: searchUrl,
    category_key: config.category_key, observed_at: new Date().toISOString(), record_count: records.length, records,
    scope: bestSellersMode
      ? `Amazon US Best Sellers visible frontend ranks 1-${targetCount} at observation time`
      : 'Amazon US search result order at observation time; not a Best Sellers rank',
    detail_enrichment: {
      standard: 'LATEST_DETAIL_V2',
      processed_count: records.length,
      detail_success_count: records.filter(row => row.detail_status === '详情页已采集').length,
      detail_main_image_count: records.filter(row => row.image_status === '详情页主图').length,
      public_original_image_count:records.filter(row=>row.public_image_evidence?.verified===true).length,
      retry_request_count: outcome.retries,
      first_pass_retry_count: outcome.retried_asins.length,
      request_count_this_run: outcome.requests,
      max_attempts_per_item: null,
      retry_policy:'UNTIL_COMPLETE_OR_STOPPED',
      max_page_loads_per_attempt:2,
      remaining_failures: detailQueue.remaining_asins,
      queue: detailQueue,
      final_fallback_count: records.filter(row => row.image_status !== '详情页主图').length,
      fields: ['#landingImage最大动态候选', '品牌', '卖家', '库存提示', '规格与卖点', '变体', '详情页BSR', '价格', '评分', '评论数'],
    },
  };
  await fs.mkdir(path.dirname(outputPath), { recursive: true });
  await publishJson(payload);
  await persist(status);
  console.log(JSON.stringify({ status, output: outputPath, checkpoint: checkpointPath, unique_asins: records.length, missing_details: missingDetails.length }));
  if (status !== 'COMPLETE') process.exitCode = 2;
  }
} finally {
  await browser.close();
}
