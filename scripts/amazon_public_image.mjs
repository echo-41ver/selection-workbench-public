import {highResolutionDimensions,nativeOriginalDimensions} from './amazon_detail_quality.mjs';
// Recover the original public CDN asset identified by the observed listing.
// Never change image identity or infer product specifications from another SKU.
export function originalListingImage(value) {
  try {
    const url=new URL(value);
    if(url.protocol!=='https:' || url.username || url.password || url.port
       || !['m.media-amazon.com','images-na.ssl-images-amazon.com'].includes(url.hostname)) return null;
    const match=url.pathname.match(/^\/images\/I\/([A-Za-z0-9+%_-]+)(?:\._[^/]+_)?\.(jpg|jpeg|png)$/i);
    if(!match) return null;
    return `${url.origin}/images/I/${match[1]}.${match[2]}`;
  } catch {return null;}
}

export async function enrichListingImage(record,inspectImage) {
  if(record.image_status==='详情页主图' || record.public_image_evidence?.verified===true) return record;
  const original=originalListingImage(record.listing_image_url || record.main_image_url);
  if(!original) return record;
  const attempts=(record.public_image_attempts || 0)+1;
  if(attempts>3) return record;
  try {
    const result=await inspectImage(original);
    if(!result || !highResolutionDimensions(result.width,result.height)) throw new Error('IMAGE_TOO_SMALL_OR_INVALID');
    return {...record,main_image_url:original,image_source:'Amazon榜单同一图片的公开原图',
      image_status:'榜单原图已核验',image_width_hint:result.width,image_height_hint:result.height,
      public_image_attempts:attempts,public_image_evidence:{verified:true,url:original,
        listing_url:record.listing_image_url || record.main_image_url,width:result.width,height:result.height,
        observed_at:new Date().toISOString()}};
  } catch {
    return {...record,public_image_attempts:attempts,
      public_image_evidence:{verified:false,url:original,error:'公开原图暂不可用，保留已采集图片'}};
  }
}

export function isAmazonImageUrl(value) {
  try {
    const url = new URL(value);
    return url.protocol === 'https:' && !url.username && !url.password && !url.port
      && ['m.media-amazon.com','images-na.ssl-images-amazon.com','images-eu.ssl-images-amazon.com'].includes(url.hostname)
      && /^\/images\/I\/[A-Za-z0-9+%_-]+(?:\._[^/]+_)?\.(?:jpg|jpeg|png|webp)$/i.test(url.pathname);
  } catch { return false; }
}

export async function inspectAmazonImage(context,url,native_original) {
  if(!isAmazonImageUrl(url)) throw new Error('INVALID_AMAZON_IMAGE_URL');
  const response=await context.request.get(url,{timeout:15000,maxRedirects:0});
  let page;
  try {
    const mime=(response.headers()['content-type'] || '').split(';')[0].trim().toLowerCase();
    if(!response.ok() || !['image/jpeg','image/png','image/webp'].includes(mime)) throw new Error('INVALID_IMAGE_RESPONSE');
    if(Number(response.headers()['content-length'])>20*1024*1024) throw new Error('IMAGE_TOO_LARGE');
    const bytes=await response.body();
    if(!bytes.length || bytes.length>20*1024*1024) throw new Error('IMAGE_TOO_LARGE_OR_EMPTY');
    page=await context.newPage();
    const dimensions=await page.evaluate(async data=>{
      const image=new Image();image.src=data;
      await Promise.race([image.decode(),new Promise((_,reject)=>setTimeout(()=>reject(new Error('IMAGE_DECODE_TIMEOUT')),5000))]);
      return {width:image.naturalWidth,height:image.naturalHeight};
    },`data:${mime};base64,${bytes.toString('base64')}`);
    if(!highResolutionDimensions(dimensions.width,dimensions.height) && !nativeOriginalDimensions({...dimensions,url,native_original})) throw new Error(`IMAGE_TOO_SMALL (${dimensions.width}x${dimensions.height})`);
    return {...dimensions,...(native_original ? {native_original} : {}),verified:true,decoded:true,http_status:response.status(),mime,byte_count:bytes.length};
  } finally {if(page) await page.close();await response.dispose();}
}

export async function inspectPublicImage(context,url) {
  if(originalListingImage(url)!==url) throw new Error('INVALID_PUBLIC_IMAGE_URL');
  return inspectAmazonImage(context,url);
}
