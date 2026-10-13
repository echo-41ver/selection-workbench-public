// Evidence contract for collectors and checkpoint consumers. A listing title or
// an HTTPS string is not proof that detail content / an image was retrieved.
export const DETAIL_QUALITY_VERSION = 'amazon-detail-quality-v1';
const text = value => typeof value === 'string' ? value.trim() : '';
export const usefulSpecifications = rows => Array.isArray(rows) ? rows.filter(row =>
  text(row?.label) && text(row?.value)
  && !/^(ASIN|Best Sellers Rank|Customer Reviews|Date First Available)$/i.test(text(row.label))) : [];

// Preserve the normal 600px short-edge standard while admitting narrow originals.
export function highResolutionDimensions(width, height) {
  if (!Number.isInteger(width) || !Number.isInteger(height) || width <= 0 || height <= 0) return false;
  const short = Math.min(width, height), long = Math.max(width, height);
  return short >= 600 || (long >= 1000 && short >= 120 && width * height >= 180000);
}

export function nativeOriginalUrl(value) {
  try {
    const u=new URL(value);
    if(u.protocol!=='https:' || u.username || u.password || u.port || !['m.media-amazon.com','images-na.ssl-images-amazon.com','images-eu.ssl-images-amazon.com'].includes(u.hostname)) return null;
    const m=u.pathname.match(/^\/images\/I\/([A-Za-z0-9+%_-]+)(?:\._[^/]+_)?\.(jpg|jpeg|png|webp)$/i);
    return m ? u.origin+'/images/I/'+m[1]+'.'+m[2] : null;
  } catch {return null;}
}
export function nativeOriginalDimensions(image) {
  const proof=image.native_original;
  return proof?.observed_detail_url && nativeOriginalUrl(proof.observed_detail_url)===image.url
    && nativeOriginalUrl(image.url)===image.url
    && Number.isInteger(image.width) && Number.isInteger(image.height)
    && Math.max(image.width,image.height)>=750 && Math.min(image.width,image.height)>=120
    && image.width*image.height>=180000;
}
export function verifiedDetailImage(row) {
  const image = row?.detail_quality?.image;
  return image?.verified === true && image.decoded === true
    && image.url === row.main_image_url && image.http_status >= 200 && image.http_status < 300
    && ['image/jpeg','image/png','image/webp'].includes(image.mime)
    && (highResolutionDimensions(image.width, image.height) || !!nativeOriginalDimensions(image));
}

export function detailQualityComplete(row) {
  const quality = row?.detail_quality;
  return quality?.schema_version === DETAIL_QUALITY_VERSION
    && quality.title_verified === true && !!text(row?.title)
    && usefulSpecifications(row?.specifications).length > 0
    && Array.isArray(quality.missing_fields) && quality.missing_fields.length === 0
    && verifiedDetailImage(row);
}

export async function qualifyAmazonDetail(record, value, candidates, inspectImage) {
  const previous = record.detail_quality;
  const merged = {...record};
  // A transient empty DOM must not erase previously observed valid facts.
  for (const [key, item] of Object.entries(value)) {
    if (item !== null && item !== undefined && item !== '' && (!Array.isArray(item) || item.length)) merged[key] = item;
  }
  const titleVerified = !!text(value.title) || (previous?.title_verified === true && !!text(record.title));
  const imageFailures = [];
  let image = verifiedDetailImage(record) ? previous.image : null;
  if (image) {
    merged.main_image_url = record.main_image_url;
    merged.image_source = record.image_source;
  } else {
    const seen = new Set();
    const originals=candidates.filter(c=>/详情页/.test(c?.source || '')).map(c=>({
      url:nativeOriginalUrl(c.url),source:'详情页同一图片公开原图',observed_detail_url:c.url})).filter(c=>c.url);
    for (const candidate of [...originals,...candidates]) {
      if (!candidate?.url || seen.has(candidate.url)) continue;
      seen.add(candidate.url);
      if (seen.size > 3) break;
      try {
        const result = await inspectImage(candidate.url,candidate.observed_detail_url ? {observed_detail_url:candidate.observed_detail_url} : undefined);
        const proof = {...result, url:candidate.url, observed_at:new Date().toISOString()};
        if (!verifiedDetailImage({main_image_url:candidate.url,detail_quality:{image:proof}})) throw new Error('IMAGE_QUALITY_UNVERIFIED');
        image = proof;
        merged.main_image_url = candidate.url;
        merged.image_source = candidate.source;
        merged.image_width_hint = proof.width;
        merged.image_height_hint = proof.height;
        break;
      } catch (error) { imageFailures.push({url:candidate.url,error:String(error.message).slice(0,200)}); }
    }
    if (!image) {
      merged.main_image_url = record.main_image_url || null;
      merged.image_source = record.image_source || '榜单卡片主图';
    }
  }
  const missing = [];
  if (!titleVerified) missing.push('detail_title');
  if (!usefulSpecifications(merged.specifications).length) missing.push('specifications_or_features');
  if (!image) missing.push('verified_detail_image');
  merged.detail_quality = {schema_version:DETAIL_QUALITY_VERSION,title_verified:titleVerified,
    specification_count:usefulSpecifications(merged.specifications).length,image,
    image_failures:imageFailures,missing_fields:missing,observed_at:new Date().toISOString()};
  merged.image_status = image ? '详情页主图' : '榜单图回退';
  merged.detail_status = missing.length ? '详情页待补全' : '详情页已采集';
  merged.detail_error = missing.length ? {code:'DETAIL_QUALITY_INCOMPLETE',retryable:true,
    message:`详情证据不足：${missing.join(', ')}`,missing_fields:missing} : null;
  return merged;
}
