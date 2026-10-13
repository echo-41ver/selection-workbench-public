import fs from "node:fs/promises";
import path from "node:path";
import { chromium } from "playwright";

const rootUrl = "https://www.amazon.com/Best-Sellers-Shower-Curtains/zgbs/home-garden/13749881";
const pages = [
  rootUrl,
  "https://www.amazon.com/Best-Sellers-Home-Kitchen-Shower-Curtains/zgbs/home-garden/13749881/ref=zg_bs_pg_2_home-garden?_encoding=UTF8&pg=2",
];
const outputPath = path.resolve("output", "amazon_shower_bestsellers_raw_20260919.json");
const observedAt = new Date().toISOString();
const detailConcurrency = 4;

function canonicalProductUrl(asin) {
  return `https://www.amazon.com/dp/${asin}`;
}

function isHttpsUrl(value) {
  try {
    return new URL(value).protocol === "https:";
  } catch {
    return false;
  }
}

function imageCandidate(url, dimensions, source) {
  if (!isHttpsUrl(url)) return null;
  const [width, height] = Array.isArray(dimensions) ? dimensions.map(Number) : [0, 0];
  return { url, width: Number.isFinite(width) ? width : 0, height: Number.isFinite(height) ? height : 0, source };
}

async function extractDetailFacts(page) {
  return page.evaluate(() => {
    const clean = (value) => (value || "").replace(/\s+/g, " ").trim();
    const firstText = (...selectors) => {
      for (const selector of selectors) {
        const value = clean(document.querySelector(selector)?.textContent);
        if (value) return value;
      }
      return null;
    };
    const priceText = firstText("#corePriceDisplay_desktop_feature_div .a-offscreen", "#corePrice_feature_div .a-offscreen", "#priceblock_ourprice");
    const price = Number((priceText || "").replace(/[^0-9.]/g, "").match(/\d+(?:\.\d+)?/)?.[0]) || null;
    const ratingText = document.querySelector("#acrPopover")?.getAttribute("title") || firstText("#acrPopover .a-icon-alt");
    const rating = Number((ratingText || "").match(/\d(?:\.\d)?/)?.[0]) || null;
    const reviewCount = Number((firstText("#acrCustomerReviewText") || "").replace(/[^0-9]/g, "")) || null;

    const specifications = [];
    const addSpecification = (label, value) => {
      const key = clean(label).replace(/:$/, "");
      const content = clean(value);
      if (key && content && !specifications.some((entry) => entry.label === key && entry.value === content)) {
        specifications.push({ label: key, value: content });
      }
    };
    document.querySelectorAll("#productDetails_detailBullets_sections1 tr, #productDetails_techSpec_section_1 tr, #productDetails_db_sections tr").forEach((row) => {
      addSpecification(row.querySelector("th")?.textContent, row.querySelector("td")?.textContent);
    });
    document.querySelectorAll("#detailBullets_feature_div li").forEach((item) => {
      const parts = [...item.querySelectorAll("span")].map((span) => clean(span.textContent)).filter(Boolean);
      if (parts.length >= 2) addSpecification(parts[0], parts.slice(1).join(" "));
    });
    document.querySelectorAll("#feature-bullets li span").forEach((item, index) => {
      const value = clean(item.textContent);
      if (value && index < 20) addSpecification(`卖点${index + 1}`, value);
    });

    const variants = [];
    document.querySelectorAll('#twister [id^="variation_"]').forEach((group) => {
      const dimension = clean(group.querySelector(".a-form-label")?.textContent || group.id.replace(/^variation_/, ""));
      group.querySelectorAll("li, option").forEach((option) => {
        const asin = option.getAttribute("data-defaultasin") || option.getAttribute("data-asin") || null;
        const label = clean(option.getAttribute("title") || option.textContent || option.querySelector("img")?.getAttribute("alt"));
        if (label || asin) variants.push({ dimension, label: label || null, asin });
      });
    });
    document.querySelectorAll('#twister select, select[id^="native_dropdown_selected_"]').forEach((select) => {
      const dimension = clean(select.closest('[id^="variation_"]')?.querySelector(".a-form-label")?.textContent || select.name || select.id);
      [...select.options].forEach((option) => {
        const label = clean(option.textContent);
        if (label && !/select/i.test(label)) variants.push({ dimension, label, asin: option.getAttribute("data-asin") || null });
      });
    });
    const uniqueVariants = variants.filter((entry, index, all) => entry.label && all.findIndex((other) => `${other.dimension}|${other.label}|${other.asin || ""}` === `${entry.dimension}|${entry.label}|${entry.asin || ""}`) === index).slice(0, 60);
    const detailText = [...document.querySelectorAll("#productDetails_detailBullets_sections1, #productDetails_techSpec_section_1, #productDetails_db_sections, #detailBullets_feature_div, #productFactsDesktopExpander")]
      .map((element) => clean(element.textContent)).join(" ");
    const bsr = specifications.find((entry) => /best sellers rank/i.test(entry.label))?.value
      || detailText.match(/Best Sellers Rank\s*(#.+?)(?=(?:\s{2,}|Product Dimensions|Date First Available|ASIN|Customer Reviews|$))/i)?.[1]
      || null;
    return {
      brand: firstText("#bylineInfo"),
      seller: firstText("#sellerProfileTriggerId", "#merchantInfoFeature_feature_div a", "#merchantInfoFeature_feature_div"),
      availability: firstText("#availability span", "#availability"),
      best_sellers_rank: bsr,
      price_usd: price,
      rating,
      review_count: reviewCount,
      specifications,
      variants: uniqueVariants,
      detail_fields_status: "详情页字段已采集",
    };
  });
}

async function resolveDetailMainImage(context, record, attempt = 1) {
  const page = await context.newPage();
  try {
    const response = await page.goto(record.product_url, { waitUntil: "domcontentloaded", timeout: 60000 });
    if (!response || response.status() >= 400) throw new Error(`详情页HTTP ${response?.status() ?? "未知"}`);
    await page.evaluate(() => window.scrollTo(0, document.body.scrollHeight));
    await page.waitForTimeout(500);
    const facts = await extractDetailFacts(page);
    await page.locator("#landingImage").waitFor({ timeout: 15000 });
    const detail = await page.locator("#landingImage").evaluate((image) => ({
      src: image.currentSrc || image.src || null,
      oldHires: image.getAttribute("data-old-hires"),
      dynamic: image.getAttribute("data-a-dynamic-image"),
    }));
    const candidates = [];
    if (detail.dynamic) {
      try {
        const dynamic = JSON.parse(detail.dynamic);
        for (const [url, dimensions] of Object.entries(dynamic)) {
          const candidate = imageCandidate(url, dimensions, "详情页动态主图");
          if (candidate) candidates.push(candidate);
        }
      } catch {
        // The displayed main image remains a valid fallback below.
      }
    }
    const oldHires = imageCandidate(detail.oldHires, [0, 0], "详情页原始主图");
    if (oldHires) candidates.push(oldHires);
    const displayed = imageCandidate(detail.src, [0, 0], "详情页显示主图");
    if (displayed) candidates.push(displayed);
    const preferred = candidates
      .filter((candidate) => Math.max(candidate.width, candidate.height) >= 600)
      .sort((left, right) => Math.max(right.width, right.height) - Math.max(left.width, left.height))[0]
      || candidates[0];
    if (!preferred) throw new Error("详情页未提供可用主图");
    return {
      ...record,
      ...facts,
      listing_image_url: record.main_image_url,
      main_image_url: preferred.url,
      image_source: preferred.source,
      image_width_hint: preferred.width || null,
      image_height_hint: preferred.height || null,
      image_status: preferred.url === record.main_image_url ? "榜单图回退" : "详情页主图",
      detail_image_attempts: attempt,
      price_usd: facts.price_usd ?? record.price_usd,
      rating: facts.rating ?? record.rating,
      review_count: facts.review_count ?? record.review_count,
    };
  } catch (error) {
    return {
      ...record,
      listing_image_url: record.main_image_url,
      image_source: "榜单卡片主图",
      image_width_hint: 600,
      image_height_hint: 600,
      image_status: `详情页${attempt >= 2 ? "两次" : ""}回退：${String(error.message || error).slice(0, 160)}`,
      detail_image_attempts: attempt,
      detail_fields_status: `详情页字段未采集：${String(error.message || error).slice(0, 160)}`,
      brand: null,
      seller: null,
      availability: null,
      best_sellers_rank: null,
      specifications: [],
      variants: [],
    };
  } finally {
    await page.close();
  }
}

async function mapWithConcurrency(items, concurrency, worker) {
  const results = new Array(items.length);
  let nextIndex = 0;
  await Promise.all(Array.from({ length: Math.min(concurrency, items.length) }, async () => {
    while (true) {
      const index = nextIndex;
      nextIndex += 1;
      if (index >= items.length) return;
      results[index] = await worker(items[index]);
      if ((index + 1) % 10 === 0 || index + 1 === items.length) console.log(`详情主图已处理 ${index + 1}/${items.length}`);
    }
  }));
  return results;
}

const browser = await chromium.launch({
  headless: true,
  executablePath: "C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe",
});

try {
  const context = await browser.newContext({
    viewport: { width: 1440, height: 1000 },
    locale: "en-US",
    userAgent: "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/153.0.0.0 Safari/537.36 Edg/153.0.0.0",
  });
  const records = [];
  for (let pageNo = 1; pageNo <= pages.length; pageNo += 1) {
    const page = await context.newPage();
    const response = await page.goto(pages[pageNo - 1], { waitUntil: "domcontentloaded", timeout: 60000 });
    await page.locator(".zg-grid-general-faceout").first().waitFor({ timeout: 20000 });
    for (let pass = 0; pass < 4; pass += 1) {
      await page.evaluate(() => window.scrollTo(0, document.body.scrollHeight));
      await page.waitForTimeout(1000);
    }
    const status = response?.status() ?? null;
    if (status !== 200) throw new Error(`榜单第${pageNo}页HTTP状态异常: ${status}`);
    const pageRecords = await page.locator(".zg-bdg-ctr").evaluateAll((rankNodes, sourceUrl) => rankNodes.map((rankNode) => {
      const rank = Number((rankNode.textContent || "").replace(/[^\d]/g, ""));
      let card = rankNode.closest(".zg-grid-general-faceout");
      if (!card) {
        let parent = rankNode.parentElement;
        while (parent && parent !== document.body) {
          if (parent.querySelector('a[href*="/dp/"]') && parent.querySelectorAll(".zg-bdg-ctr").length === 1) {
            card = parent;
            break;
          }
          parent = parent.parentElement;
        }
      }
      if (!card) return { rank, extraction_error: "rank_badge_without_product_card" };
      const productRoot = card.querySelector("[id]");
      const asin = (productRoot?.id || "").match(/^[A-Z0-9]{10}$/i)?.[0]?.toUpperCase() || null;
      const links = [...card.querySelectorAll('a[href*="/dp/"]')];
      const titleLink = links.map((link) => ({ text: (link.innerText || "").trim(), href: link.href }))
        .filter((entry) => entry.text)
        .sort((a, b) => b.text.length - a.text.length)[0] || null;
      const ratingText = (card.querySelector(".a-icon-alt")?.textContent || "").trim();
      const rating = Number((ratingText.match(/\d(?:\.\d)?/) || [""])[0]) || null;
      const reviewText = (card.querySelector('a[href*="/product-reviews/"] .a-size-small')?.textContent || "").replace(/[^\d]/g, "");
      const priceText = ([...card.querySelectorAll(".a-color-price, [class*='p13n-sc-price']")].map((node) => (node.textContent || "").trim()).find(Boolean) || "");
      const price = Number((priceText.replace(/[^0-9.]/g, "").match(/\d+(?:\.\d+)?/) || [""])[0]) || null;
      const imageNode = card.querySelector("img.p13n-product-image, img");
      const image = imageNode?.currentSrc
        || imageNode?.src
        || imageNode?.getAttribute("data-src")
        || null;
      return {
        page_rank: rank,
        asin,
        title: titleLink?.text || card.querySelector("img")?.alt || null,
        product_url: asin ? `https://www.amazon.com/dp/${asin}` : titleLink?.href || null,
        main_image_url: image,
        price_usd: price,
        rating,
        review_count: reviewText ? Number(reviewText) : null,
        raw_card_text: (card.innerText || "").trim(),
        source_url: sourceUrl,
      };
    }), page.url());
    if (pageRecords.length !== 50) throw new Error(`榜单第${pageNo}页带#排名的目标卡片数为${pageRecords.length}，预期50`);
    for (const item of pageRecords) {
      item.product_url = item.asin ? canonicalProductUrl(item.asin) : item.product_url;
      if (!item.asin || !item.title || !isHttpsUrl(item.product_url) || !isHttpsUrl(item.main_image_url)) {
        throw new Error(`榜单第${pageNo}页存在缺少ASIN、标题、HTTPS商品链接或HTTPS主图链接的卡片`);
      }
      records.push({ ...item, rank: item.page_rank, page_no: pageNo });
    }
    await page.close();
  }
  const unique = new Map(records.map((record) => [record.asin, record]));
  if (records.length !== 100 || unique.size !== 100) {
    throw new Error(`两页榜单未形成100个唯一ASIN：卡片${records.length}，唯一ASIN ${unique.size}`);
  }
  const firstPassRecords = await mapWithConcurrency(records, detailConcurrency, (record) => resolveDetailMainImage(context, record));
  const firstPassFallbacks = firstPassRecords.filter((record) => record.image_status !== "详情页主图");
  console.log(`详情主图首轮完成：成功${firstPassRecords.length - firstPassFallbacks.length}，待补${firstPassFallbacks.length}`);

  // 首轮全部结束后才集中补采失败项，避免一条短暂网络波动阻塞其余99条。
  const retriedFallbacks = await mapWithConcurrency(
    firstPassFallbacks,
    Math.min(2, detailConcurrency),
    (record) => resolveDetailMainImage(context, record, 2),
  );
  const retryByAsin = new Map(retriedFallbacks.map((record) => [record.asin, record]));
  const enrichedRecords = firstPassRecords.map((record) => retryByAsin.get(record.asin) || record);
  const finalFallbacks = enrichedRecords.filter((record) => record.image_status !== "详情页主图");
  console.log(`详情主图补采完成：成功${enrichedRecords.length - finalFallbacks.length}，最终回退${finalFallbacks.length}`);
  const payload = {
    schema_version: "amazon-shower-bestsellers-listing-v1",
    source_name: "Amazon US Best Sellers 前端榜单卡片",
    source_root_url: rootUrl,
    category: "Home & Kitchen > Shower Curtains",
    observed_at: observedAt,
    pages: pages.map((url, index) => ({ page_no: index + 1, url })),
    record_count: records.length,
    page_card_count: 50,
    records: enrichedRecords,
    detail_image_enrichment: {
      method: "Amazon商品详情页#landingImage动态主图，优先最大候选，最低接受600px；首轮失败项在全部100条完成后统一补采1次，无详情图时保留榜单图",
      processed_count: enrichedRecords.length,
      detail_main_image_count: enrichedRecords.filter((record) => record.image_status === "详情页主图").length,
      listing_fallback_count: enrichedRecords.filter((record) => record.image_status !== "详情页主图").length,
      first_pass_fallback_count: firstPassFallbacks.length,
      retry_pass_count: retriedFallbacks.length,
      detail_field_count: enrichedRecords.filter((record) => record.detail_fields_status === "详情页字段已采集").length,
    },
  };
  await fs.mkdir(path.dirname(outputPath), { recursive: true });
  await fs.writeFile(outputPath, JSON.stringify(payload, null, 2), "utf8");
  console.log(JSON.stringify({ outputPath, record_count: records.length, unique_asin_count: unique.size, observed_at: observedAt }, null, 2));
} finally {
  await browser.close();
}
