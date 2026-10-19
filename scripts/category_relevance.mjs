import {createHash} from 'node:crypto';

// Collection recall only. These labels are not a same-product decision.
export const CATEGORY_RELEVANCE_VERSION = 'multilingual-subject-v4';
export const COLLECTION_PROGRESS_VERSION = 'relevant-round-v4';
export function restoreNoNewRounds(checkpoint, policy) {
  const count = Number(checkpoint?.consecutive_no_new_rounds);
  return checkpoint?.stop_logic_version === COLLECTION_PROGRESS_VERSION &&
    checkpoint?.relevance_policy_fingerprint === policy.fingerprint &&
    Number.isSafeInteger(count) && count >= 0 ? count : 0;
}
const normalize = value => String(value ?? '').normalize('NFKC').toLowerCase()
  .replace(/[\u00ad\u200b\ufeff]/g, '')
  // A spaced title separator ends a suitability clause; a word hyphen does not.
  .replace(/\s+[-‐‑–—]\s+/g, ' ; ')
  .replace(/[‐‑–—-]/g, ' ').replace(/\s+/g, ' ').trim();
const list = value => Array.isArray(value) ? value.filter(v => typeof v === 'string') : [];
const aliases = value => Array.isArray(value) ? list(value) : value && typeof value === 'object'
  ? Object.entries(value).filter(([locale]) => /^[a-z]{2,3}(?:[-_][a-z0-9]{2,8})*$/i.test(locale))
    .flatMap(([,terms]) => list(terms)) : [];
const uniqueTerms = values => [...new Set(values.map(normalize).filter(v => [...v].length >= 2))];
const wordChar = value => /[\p{Script=Latin}\p{N}_]/u.test(value || '');

function occurrences(text, term) {
  const result = [];
  let offset = 0;
  while (offset < text.length) {
    const start = text.indexOf(term, offset);
    if (start < 0) break;
    const end = start + term.length;
    if ((!wordChar(term[0]) || !wordChar(text[start - 1])) &&
        (!wordChar(term.at(-1)) || !wordChar(text[end]))) result.push({term, start, end});
    offset = end;
  }
  return result;
}

// Generic accessory/suitability context is ambiguous, never a deletion rule.
// Explicit category aliases, e.g. "phone case", cover the full noun instead.
function accessoryContext(text, hit) {
  const after = text.slice(hit.end);
  const accessory = /^\s*(?:pads?|mats?|cases?|covers?|skins?|sleeves?|holders?|stands?|cables?|grips?|feet|skates?|receivers?|accessories|replacement parts?|protectors?)\b/u;
  const chineseAccessory = /^\s*(?:保[护護][套壳殼]|收[纳納][包盒袋]|替[换換]件|配件|支架|接收器|[贴貼]膜|[脚腳][贴貼]|[垫墊]|[壳殼]|包(?!含|括|装|裝)|罩|[线線]|[盖蓋]|套(?!装|裝))/u;
  return accessory.test(after) || chineseAccessory.test(after);
}
function compatibilityContext(text, hit) {
  const before = text.slice(Math.max(0, hit.start - 90), hit.start).split(/[,，;；。.!！？]/).at(-1);
  // In Chinese "适用于笔记本的无线鼠标", 的 closes the host clause.
  if (/的(?:无线|無線|有线|有線|蓝牙|藍牙|可充电|可充電|静音|靜音|游戏|遊戲|光学|光學|人体工学|人體工學|\s)*$/u.test(before)) return false;
  const compatibility = /(?:\b(?:for|fits?|compatible with|replacement for|suitable for|without|excluding|not included)\b|[适適]用[于於]|兼容|相容|替换|替換|不含|不包含)[^,，;；。.!！？]*$/u;
  return compatibility.test(before);
}
function ambiguousContext(text, hit) {
  return accessoryContext(text, hit) || compatibilityContext(text, hit);
}

/**
 * Approved config only; never translate a search query or borrow another category.
 * Optional category_aliases (root/temu) contain full multilingual category nouns,
 * either a string array or locale-to-array map. They are explicit reviewed rules,
 * not automatic translation or a claim that every language has been covered.
 * Identity synonyms are expanded only from a uniquely anchored product_form group,
 * or explicitly listed temu.relevance_identity_attributes. Feature adjectives and
 * brand/model/search words are deliberately not inferred as category identities.
 */
export function createCategoryRelevance(config = {}) {
  const temu = config.temu || {};
  const anchors = uniqueTerms([
    ...list(temu.include_terms), config.category_name_zh, config.category_name_en,
    ...aliases(config.category_aliases), ...aliases(temu.category_aliases),
    // Compact authoring explicitly defines these as category-body nouns, before
    // any per-product keyword expansion. Legacy/free-form queries stay untrusted.
    ...(config.rule_authoring_format === 'compact-category-v1' ? list(temu.base_queries) : []),
  ]);
  const identityTerms = [];
  const approvedAttributes = new Set(['product_form', ...list(temu.relevance_identity_attributes)]);
  for (const attribute of config.stage1?.attributes || []) {
    if (!attribute.hard_conflict || !approvedAttributes.has(attribute.name)) continue;
    const groups = Object.entries(attribute.values || {}).map(([value, terms]) => uniqueTerms([value, ...list(terms)]));
    const selected = groups.filter(terms => terms.some(term => anchors.includes(term)));
    if (selected.length === 1) identityTerms.push(...selected[0]);
  }
  const includeTerms = uniqueTerms([...anchors, ...identityTerms]);
  const severeExcludeTerms = uniqueTerms(list(temu.severe_exclude_terms));
  const subjectFilter = temu.subject_filter || {};
  const strict = subjectFilter.mode === 'TARGET_BODY_ONLY';
  const subjectExcludeTerms = uniqueTerms(list(subjectFilter.exclude_subject_terms));
  const fingerprint = createHash('sha256').update(JSON.stringify({
    version:CATEGORY_RELEVANCE_VERSION,include:[...includeTerms].sort(),exclude:[...severeExcludeTerms].sort(),
    subject_filter:{strict,exclude:[...subjectExcludeTerms].sort(),reject_mixed_subjects:subjectFilter.reject_mixed_subjects===true},
  })).digest('hex');

  function classify(row) {
    // The title is the product statement; a full card may also name recommendations.
    const text = normalize(row?.title || row?.card_text || row?.goods_name || '').replace(/^商品图片\s*/u,'');
    const targets = includeTerms.flatMap(term => occurrences(text, term));
    const exclusions = severeExcludeTerms.flatMap(term => occurrences(text, term));
    const usable = targets.filter(hit => !ambiguousContext(text, hit) &&
      !exclusions.some(other => other.start <= hit.start && other.end >= hit.end));
    const foreignSubjects = strict ? subjectExcludeTerms.flatMap(term => occurrences(text,term)).filter(hit => {
      // A complete target noun wins over a shorter constituent: phone case > phone.
      if(usable.some(target=>target.start<=hit.start&&target.end>=hit.end))return false;
      if (compatibilityContext(text,hit)) return false;
      // Keyboard shortcuts/key mapping are features, not a sold keyboard.
      return !/^\s*(?:shortcuts?\b|快捷键|快捷鍵|映射)/u.test(text.slice(hit.end));
    }) : [];
    let state = 'UNKNOWN';
    let reason = text ? 'NO_CATEGORY_EVIDENCE' : 'MISSING_PRODUCT_TEXT';
    if (foreignSubjects.length && (subjectFilter.reject_mixed_subjects===true || !usable.length)) {
      state='EXCLUDED';reason='OTHER_SOLD_SUBJECT';
    } else if (strict && targets.length && !usable.length && targets.some(hit=>accessoryContext(text,hit))) {
      state='EXCLUDED';reason='TARGET_ACCESSORY_ONLY';
    } else if (usable.length && !exclusions.length) {
      state = 'RELEVANT'; reason = 'CATEGORY_TERM';
    } else if (usable.length && exclusions.length) {
      reason = 'MIXED_CATEGORY_EVIDENCE';
    } else if (exclusions.length) {
      state = 'EXCLUDED'; reason = 'EXPLICIT_EXCLUSION';
    } else if (targets.length) {
      reason = 'ACCESSORY_OR_COMPATIBILITY_CONTEXT';
    } else if (!includeTerms.length) {
      reason = 'MISSING_CATEGORY_TERMS';
    }
    return {state, reason, matched_terms: [...new Set(targets.map(hit => hit.term))],
      exclusion_terms: [...new Set([...exclusions,...foreignSubjects].map(hit => hit.term))]};
  }

  function summarize(rows) {
    const result = {relevant: 0, unknown: 0, excluded: 0, progress: 0};
    for (const row of rows) result[classify(row).state.toLowerCase()] += 1;
    // Missing language coverage is not evidence that collection stopped advancing.
    // Unknown rows stay available for later review but are not reported as relevant.
    result.progress = result.relevant + result.unknown;
    return result;
  }
  return {version: CATEGORY_RELEVANCE_VERSION, fingerprint, strict, includeTerms, severeExcludeTerms, classify, summarize,
    isRelated: row => classify(row).state === 'RELEVANT',
    isSevereOffTopic: row => classify(row).state === 'EXCLUDED',
    countProgressRows: rows => summarize(rows).progress};
}
