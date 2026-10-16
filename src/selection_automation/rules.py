"""Frozen, declarative category rules; never execute agent-generated Python."""
from __future__ import annotations

import hashlib
import json
import math
import re
from functools import lru_cache
from pathlib import Path, PureWindowsPath

RELATIONS = {"SAME": 1, "VARIANT": .82, "SIMILAR": .55, "DIFFERENT": 0, "UNKNOWN": None}


def revision(value: dict) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def validate_rules(category: dict) -> None:
    """Validate editable rules even when collection prerequisites are unknown."""
    if not isinstance(category, dict) or any(not isinstance(category.get(k), dict) for k in ('amazon','temu','stage1','stage2','stage3')):
        raise ValueError('category requires object sections for collection and rules')
    source=category['amazon'].get('bestsellers_url')
    if source is not None and not isinstance(source,str):
        raise ValueError('Amazon source must be text or unknown')
    for section,field in (('amazon','target_count'),('temu','max_products_per_keyword')):
        count=category[section].get(field)
        if count is not None and type(count) is not int:
            raise ValueError('collection count must be an integer or unknown')
    if not isinstance(category.get('category_key'), str) or not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", category["category_key"]):
        raise ValueError("category_key must be a safe lowercase identifier")
    name=category.get('category_name_zh')
    if not isinstance(name,str) or not name.strip() or re.search(r'[<>:"/\\|?*\x00-\x1f]', name) or name in ('.','..'):
        raise ValueError('category display name is not a safe filename')
    raw_output = category.get('stage12_output_dir', 'stage12')
    if not isinstance(raw_output,str) or not raw_output:
        raise ValueError('stage12 output must stay inside this task')
    output = Path(raw_output)
    windows_output = PureWindowsPath(raw_output)
    if not raw_output or output.is_absolute() or windows_output.drive or windows_output.root or '..' in windows_output.parts or '..' in output.parts:
        raise ValueError('stage12 output must stay inside this task')
    stage = category["stage3"]
    from .stage3_admission import concurrency
    concurrency(stage)
    modules = stage.get("modules")
    if (not isinstance(modules,dict) or not modules or len(modules)>12
        or any(not isinstance(k,str) or not k.strip() for k in modules)
        or any(type(v) not in (int,float) or not math.isfinite(v) or v<=0 for v in modules.values())
        or sum(modules.values())!=100):
        raise ValueError("visual module weights must be positive and total 100")
    if (any(type(stage.get(k)) not in (int,float) or not math.isfinite(stage[k]) for k in ('different_score_ceiling','same_threshold'))
        or not 0 <= stage["different_score_ceiling"] < stage["same_threshold"] <= 100):
        raise ValueError("invalid classification thresholds")
    bindings = stage.get('module_variant_fields', {})
    allowed_variants = set(stage.get('variant_fields', [])) - set(modules)
    if not isinstance(bindings, dict) or any(
        key not in modules or not isinstance(fields, list) or not fields or
        any(not isinstance(field, str) or field not in allowed_variants for field in fields)
        for key, fields in bindings.items()
    ):
        raise ValueError('module variants must bind known modules to explicit allowed variant fields')
    required_modules = stage.get('identity_required_modules')
    if required_modules is not None and (not isinstance(required_modules, list) or not required_modules or
        any(not isinstance(key, str) or key not in modules for key in required_modules)):
        raise ValueError('identity required modules must reference known visual modules')
    for key, score in stage.get("confirmation_minimums", {}).items():
        if key == "known_modules":
            if isinstance(score, bool) or not isinstance(score, int) or not 1 <= score <= len(modules):
                raise ValueError("invalid known_modules minimum")
            continue
        if key == "evidence_quality":
            if not isinstance(score, list) or not score or any(q not in {"HIGH", "MEDIUM"} for q in score):
                raise ValueError("invalid evidence_quality minimum")
            continue
        if key not in modules:
            raise ValueError("confirmation minimum references an unknown module")
        if type(score) not in (int,float) or not math.isfinite(score) or not 0 <= score <= modules[key]:
            raise ValueError("module minimum exceeds its weight")
    threshold = category["stage2"]["candidate_threshold"]
    if isinstance(threshold, bool) or not isinstance(threshold, (int, float)) or not math.isfinite(threshold) or not 0 <= threshold <= 100:
        raise ValueError("stage2 candidate threshold must be finite and 0..100")
    if category['stage2'].get('included_in_final_score',False) is not False:
        raise ValueError('stage2 candidate score cannot become final identity evidence')
    status=category['stage2'].get('threshold_status','')
    if not isinstance(status,str): raise ValueError('stage2 threshold status must be text')
    if status.startswith('PROVISIONAL') and category['stage2'].get('below_threshold_action') not in {'REVIEW_NOT_FINAL_DIFFERENT','NOT_SELECTED_NOT_MANUAL_REVIEW'}:
        raise ValueError('uncalibrated stage2 threshold cannot create a final different-product decision')
    gate=category['stage1'].get('text_gate')
    if not isinstance(gate,str) or not re.fullmatch('[a-z][a-z0-9_]*',gate):
        raise ValueError('text gate must be a named declarative or registered rule')
    if gate == "declarative":
        attributes=category['stage1'].get('attributes')
        if not isinstance(attributes,list) or not 1<=len(attributes)<=32:
            raise ValueError("new category requires explicit text attributes")
        if set(modules) != set(stage.get("module_descriptions", {})):
            raise ValueError("every visual module requires a description")
        if any(not isinstance(v,str) or not v.strip() for v in stage['module_descriptions'].values()):
            raise ValueError('every visual module requires a nonempty text description')
        for attr in attributes:
            if (not isinstance(attr,dict) or not isinstance(attr.get('name'),str) or not attr['name'].strip()
                or type(attr.get('hard_conflict')) is not bool or not isinstance(attr.get('values'),dict) or not attr['values']):
                raise ValueError('text attributes must have a name, boolean conflict flag and literal values')
            if not isinstance(attr.get('ambiguous_phrases',[]),list) or any(not isinstance(p,str) for p in attr.get('ambiguous_phrases',[])):
                raise ValueError('ambiguous phrases must be a list of literal text')
            for phrases in attr["values"].values():
                if not isinstance(phrases,list) or not phrases or any(not isinstance(p, str) or not p.strip() for p in phrases):
                    raise ValueError("attribute aliases must be nonempty literal phrases")
    from .identity_policy import validate_policy_consistency
    validate_policy_consistency(category)
    from .set_inventory import requirements
    requirements(category)
    from .stage3_admission import threshold
    threshold(category)


def validate_execution_inputs(category: dict) -> None:
    """Require collection inputs only for freezing/running, never rule editing."""
    from urllib.parse import urlparse
    raw_url=category['amazon'].get('bestsellers_url')
    if not isinstance(raw_url,str): raise ValueError('a real Amazon US Best Sellers URL is required')
    url=urlparse(raw_url)
    if url.scheme!='https' or url.netloc!='www.amazon.com' or '/zgbs/' not in url.path:
        raise ValueError('a real Amazon US Best Sellers URL is required')
    count=category['amazon'].get('target_count')
    if type(count) is not int or not 1<=count<=100:
        raise ValueError('Amazon Best Sellers target must be integer 1..100')
    count=category['temu'].get('max_products_per_keyword')
    if type(count) is not int or count<1:
        raise ValueError('TEMU target must be a positive integer')


def validate(category: dict) -> None:
    """Strict execution validation remains the default for frozen readers."""
    validate_rules(category)
    validate_execution_inputs(category)


@lru_cache(maxsize=32)
def _validated_frozen_content(content: str) -> dict:
    value = json.loads(content)
    validate(value)
    return value


def read_frozen(path: str) -> dict:
    """Cache validated content, never a filename that can be edited or reused."""
    return _validated_frozen_content(Path(path).read_text(encoding="utf-8"))


def contains(text: str, phrase: str) -> bool:
    return bool(re.search(r"(?<!\w)" + re.escape(phrase.casefold()) + r"(?!\w)", text.casefold())) if phrase.isascii() else phrase in text


def related_product_alias_context(text: str, start: int, end: int) -> bool:
    """A paired/layered other item is not a fact about the sold item's body."""
    prefix = text.casefold()[max(0, start - 96):start]
    prefix = re.split(r'[|,;.!?\n，。；：]', prefix)[-1]
    match = re.search(
        r'\b(?:pair|layer)(?:\s+(?:it|this|them|these))?\s+with(?:\s+[\w-]+){0,5}\s+$', prefix)
    if not match:
        return False
    # A named layer in the item's own construction is affirmative evidence.
    before = prefix[:match.start()]
    return not bool(re.search(r'\b(?:double|single|two|inner|outer|top|bottom|reinforced|a|an|the)(?:[ -]+[\w-]+){0,5}[ -]+$', before))


def uncertain_alias_context(text: str, start: int, end: int) -> bool:
    normalized=text.casefold()
    prefix=normalized[max(0,start-48):start]
    prefix=re.split(r'[|,;.!?，。；！]',prefix)[-1]
    suffix=normalized[end:end+24]
    negated=bool(re.search(r'\b(?:not|no|without|except|excluding|non)[\s-]+(?:[\w%]+[\s-]+){0,3}$',prefix)
                 or re.search(r'\b(?:un|non)$',prefix)
                 or re.search(r'\bfree\s+(?:of|from)\s+$',prefix)
                 or re.match(r'[ -]*free\b|\s+(?:is\s+|are\s+)?not\b',suffix)
                 or re.search(r'(?:不是|不含|不带|不适用|不可|不|非|无)[\u4e00-\u9fff]{0,3}$',prefix))
    appearance=bool(re.search(r'\b(?:looks? like|feels? like|imitates?)\s+$',prefix)
                    or prefix.endswith('仿') or re.match(r'[ -]*(?:like|look|feel|style|effect|texture)\b',suffix)
                    or re.match(r'(?:感|质感|外观|风格|纹理)',suffix))
    return negated or appearance or related_product_alias_context(text, start, end)


def attribute_values(text: str, aliases: dict) -> tuple[set, bool]:
    """Negated aliases are uncertain evidence, never positive identity conflicts."""
    values, uncertain = set(), False
    normalized = text.casefold()
    for name, phrases in aliases.items():
        for phrase in phrases:
            pattern = (r'(?<!\w)' + re.escape(phrase.casefold()) + r'(?!\w)') if phrase.isascii() else re.escape(phrase.casefold())
            for match in re.finditer(pattern,normalized):
                if related_product_alias_context(normalized,match.start(),match.end()):
                    continue  # Ignore the other item, retaining any affirmative own-item fact.
                if uncertain_alias_context(normalized,match.start(),match.end()):
                    uncertain = True
                else:
                    values.add(name)
    return values, uncertain


def text_gate(category: dict, left: str, right: str) -> dict:
    conflicts, facts, insufficient = [], [], False
    for attr in category["stage1"].get("attributes", []):
        a, a_uncertain = attribute_values(left,attr['values'])
        b, b_uncertain = attribute_values(right,attr['values'])
        # Mixed/negated context is not sufficiently certain for an exclusion.
        uncertain = a_uncertain or b_uncertain or any(contains(t, p) for t in (left, right) for p in attr.get("ambiguous_phrases", []))
        allowed_variants = set(category['stage3'].get('variant_fields',[])) - set(category['stage3']['modules'])
        ordinary_variant = attr['name'] in allowed_variants
        insufficient |= uncertain or len(a) != 1 or len(b) != 1
        if len(a) == len(b) == 1 and a != b and attr.get("hard_conflict") and not uncertain and not ordinary_variant:
            conflicts.append({"field": attr["name"], "amazon": next(iter(a)), "temu": next(iter(b))})
        facts.append({"field": attr["name"], "amazon": sorted(a), "temu": sorted(b), 'ambiguous_context':uncertain})
    return {"excluded": bool(conflicts), "stage1_status": "TEXT_HARD_EXCLUDE" if conflicts else ("TEXT_INSUFFICIENT" if insufficient else "TEXT_PASS"),
            "hard_conflicts": [{**c, "reason": f"{c['field']}：{c['amazon']} 与 {c['temu']} 冲突"} for c in conflicts], "facts": facts,
            "reason": "双边明确文字属性冲突" if conflicts else ("文字属性缺失或存在歧义，继续图片核验" if insufficient else "未发现双边明确文字硬冲突"),
            "rule_version": category["stage3"]["rule_version"]}


def _confidence(value: dict) -> float:
    raw = value.get("overall_confidence", 0)
    if isinstance(raw, bool) or not isinstance(raw, (int, float)) or not math.isfinite(raw) or not 0 <= raw <= 1:
        raise ValueError("confidence must be a finite number in 0..1")
    return float(raw)


def score(category: dict, value: dict) -> dict:
    if isinstance(value, dict) and 'decision_api' in value:
        from .stage3_provider import decision_score
        return decision_score(category, value)
    from .identity import apply_identity
    from .visual_decision import scoring_category, final_decision
    effective = scoring_category(category)
    return final_decision(category, apply_identity(effective, value, _score_modules(effective, value)))


def _score_modules(category: dict, value: dict) -> dict:
    """Auxiliary scores only. Final classification always passes identity guard."""
    _confidence(value)
    stage = category["stage3"]
    modules, hard, contradictions = {}, [], []
    for name, maximum in stage["modules"].items():
        row = dict(value.get("modules", {}).get(name, {}))
        rel = row.get("relation", "UNKNOWN")
        if rel not in RELATIONS:
            raise ValueError("unknown visual relation")
        evidence = (row.get("amazon_evidence"), row.get("temu_evidence"))
        if not all(isinstance(item, str) and item.strip() for item in evidence):
            rel = "UNKNOWN"
        code = row.get("hard_conflict_code")
        if rel != "UNKNOWN" and rel != "DIFFERENT" and code not in (None, "", "NONE"):
            contradictions.append(name)
        if rel == "DIFFERENT" and code in stage.get("identity_hard_conflicts", []):
            hard.append(code)
        modules[name] = {**row, "relation": rel, "maximum": maximum,
                         "score": None if rel == "UNKNOWN" else round(maximum * RELATIONS[rel])}
    known = sum(r["score"] is not None for r in modules.values())
    total = sum(r["score"] for r in modules.values()) if known == len(modules) else None
    confidence = _confidence(value)
    quality = "HIGH" if confidence >= .8 else "MEDIUM" if confidence >= .55 else "LOW"
    return {"modules": modules, "total_score": total,
            "hard_conflicts": hard, "known_module_count": known, "evidence_quality": quality,
            "variant_differences": [], "uncertainties": [k for k, m in modules.items() if m['relation'] == 'UNKNOWN'],
            "rule_version": stage["rule_version"], "threshold_status": stage.get("threshold_status", "待校准"),
            "contradictory_modules": contradictions}



def generic_keywords(rows: list[dict], category: dict) -> list[dict]:
    from .keyword_planner import _search_text
    base = category["temu"]["base_queries"][0]
    result = {base: {"query": base, "source_asins": [], "source_evidence": []}}
    for row in rows:
        text = _search_text(row)
        terms = [p for p in category["temu"].get("keyword_features", []) if contains(text, p)]
        for query in [base] + [f"{p} {base}" for p in terms[:max(1, int(category["temu"].get("keywords_per_product") or 6))]]:
            entry = result.setdefault(query, {"query": query, "source_asins": [], "source_evidence": []})
            if row["asin"] not in entry["source_asins"]:
                entry["source_asins"].append(row["asin"])
                entry["source_evidence"].append({"asin": row["asin"], "title": row.get("title"), "rank": row.get("rank")})
    from .keyword_planner import select_useful_queries
    result[base]["purpose"] = "CATEGORY_SAMPLE"
    return select_useful_queries([{**v, "max_products": category["temu"]["max_products_per_keyword"], "status": "PLANNED"}
            for v in list(result.values())])
