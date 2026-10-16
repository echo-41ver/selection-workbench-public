/** Parse only explicit, single-card metrics. Self-contained for browser serialization. */
export function parseProductCardMetrics(text, labels = []) {
  const inputs = [{text:String(text ?? ''),source:'card_text'},
    ...(Array.isArray(labels) ? labels.filter(value => typeof value === 'string')
      .map(value => ({text:value,source:'visible_label'})) : [])];
  const quantity = '([0-9]+(?:[,.][0-9]+)*)\\s*([kKmM万萬亿億千]?)\\s*(\\+?)';
  const score = '(?<![0-9.])([0-5](?:\\.[0-9]{1,3})?)(?![0-9.%])';
  const sales = [], ratings = [], reviews = [];
  function amount(match) {
    if (!/^(?:\d+|\d{1,3}(?:,\d{3})+)(?:\.\d+)?$/.test(match[1])) return null;
    const multiplier = {'':1,k:1000,m:1000000,'千':1000,'万':10000,'萬':10000,'亿':100000000,'億':100000000};
    const value = Number(match[1].replaceAll(',','')) * multiplier[match[2].toLowerCase()];
    if (!Number.isSafeInteger(value) || value < 0) return null;
    return {value,precision:match[3] ? 'displayed_lower_bound' : match[2] ? 'displayed_approximate' : 'displayed'};
  }
  const salesPatterns = [
    new RegExp('(?:已售|已售出|已销售|已銷售|售出|销量|銷量)\\s*[:：]?\\s*'+quantity+'\\s*(?:件|个|個|单|單)?','gi'),
    new RegExp(quantity+'\\s*(?:件|个|個|单|單)?\\s*(?:已售|已售出|售出)','gi'),
    new RegExp(quantity+'\\s*(?:sold|sales?)(?![a-z])','gi'),
    new RegExp('\\b(?:sold|sales)\\s*[:：]?\\s*'+quantity,'gi'),
    new RegExp(quantity+'\\s*bought\\s+in\\s+(?:the\\s+)?past\\s+month','gi'),
  ];
  const reviewPatterns = [
    new RegExp(quantity+'\\s*(?:条|條|则|則|个|個|人)?\\s*(?:评价|評價|评论|評論|评分|評分)(?![0-9])','gi'),
    new RegExp(quantity+'\\s*(?:customer\\s+)?(?:reviews?|ratings?)(?![a-z])','gi'),
    new RegExp('(?:评价|評價|评论|評論)\\s*[:：]?\\s*[（(]?\\s*'+quantity+'\\s*[）)]?','gi'),
    new RegExp('\\b(?:reviews|ratings)\\s*[:：]\\s*'+quantity,'gi'),
  ];
  const ratingPatterns = [
    new RegExp('(?:\\b(?:rating|rated)\\b|评分|評分|评星|評星)\\s*[:：为為]?\\s*'+score,'gi'),
    new RegExp(score+'\\s*out\\s+of\\s+5(?:\\.0)?(?:\\s*stars?)?(?![0-9])','gi'),
    new RegExp(score+'\\s*(?:颗星|顆星|stars\\b|星(?=$|[\\s，。,:：;；(（]))','gi'),
  ];
  for (const input of inputs) {
    const normalized = input.text.normalize('NFKC');
    const record = (match,data) => ({...data,text:match[0].trim(),source:input.source});
    for (const pattern of salesPatterns) {
      pattern.lastIndex=0;
      for (const match of normalized.matchAll(pattern)) {
        const before=normalized.slice(Math.max(0,match.index-12),match.index);
        if (/^\d/.test(match[0]) && /(?:US\s*\$|USD|CNY|[$￥¥€£])\s*$/i.test(before)) continue;
        const parsed=amount(match);
        if (parsed) sales.push(record(match,{...parsed,
          basis:/past\s+month/i.test(match[0]) ? '最近一月页面显示' : '页面显示销量，周期未知'}));
      }
    }
    for (const pattern of reviewPatterns) {
      pattern.lastIndex=0;
      for (const match of normalized.matchAll(pattern)) {
        const parsed=amount(match);
        if (parsed) reviews.push(record(match,parsed));
      }
    }
    for (const pattern of ratingPatterns) {
      pattern.lastIndex=0;
      for (const match of normalized.matchAll(pattern)) {
        const value=Number(match[1]);
        const before=normalized.slice(Math.max(0,match.index-20),match.index);
        const after=normalized.slice(match.index+match[0].length);
        if (value>5 || /(?:out\s+of|满分|滿分|最高)\s*$/i.test(before) || /^\s*(?:满分|滿分)/.test(after)) continue;
        ratings.push(record(match,{value,precision:'displayed',scale:5}));
      }
    }
  }
  function choose(observations) {
    const unique=new Map();
    for (const item of observations) {
      const key=JSON.stringify([item.value,item.precision,item.basis]);
      if (!unique.has(key)) unique.set(key,item);
    }
    const distinct=[...unique.values()];
    if (!distinct.length) return {status:'MISSING',source:null,text:null,observations:[]};
    // Two different explicit values on one card need inspection, not an invented winner.
    if (distinct.length>1) return {status:'CONFLICT',source:null,text:null,observations:distinct};
    return {status:'AVAILABLE',...distinct[0],observations:distinct};
  }
  const sale=choose(sales),rating=choose(ratings),review=choose(reviews);
  return {
    sales_text:sale.text,sales_count:sale.status==='AVAILABLE' ? sale.value : null,
    sales_basis:sale.status==='AVAILABLE' ? sale.basis : null,
    sales_precision:sale.status==='AVAILABLE' ? sale.precision : 'unknown',
    rating:rating.status==='AVAILABLE' ? rating.value : null,
    review_count:review.status==='AVAILABLE' ? review.value : null,
    review_count_text:review.text,
    review_count_precision:review.status==='AVAILABLE' ? review.precision : 'unknown',
    metric_evidence:{sales:sale,rating, reviews:review},
  };
}
