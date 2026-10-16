"""Category-fixed, image-only visual scoring for bath towel sets.

The model extracts bilateral facts and relationships.  Python owns all scores,
hard-conflict acceptance and final states.  Thresholds are category-specific
and intentionally marked provisional until a human-labelled towel-set sample
has been evaluated.
"""
from __future__ import annotations

from typing import Any, Mapping
import math

SCORING_VERSION = "bath-towel-set-image-only-four-module-v3.1"
SAME_THRESHOLD = 88
DIFFERENT_SCORE_CEILING = 69

MODULES = {
    "surface_design_weave_border": 40,
    "towel_construction_pile_edge": 27,
    "set_composition": 21,
    "color_pattern_variant": 12,
}
KEY_MODULES = {
    "surface_design_weave_border",
    "towel_construction_pile_edge",
    "set_composition",
}
MINIMUMS = {
    "surface_design_weave_border": 34,
    "towel_construction_pile_edge": 21,
    "set_composition": 15,
}
RELATION_SCORES = {
    "SAME": 1.0,
    "VARIANT": 0.82,
    "SIMILAR": 0.55,
    "DIFFERENT": 0.0,
    "UNKNOWN": None,
}
HARD_CODES = {
    "DIFFERENT_PRODUCT_SUBTYPE",
    "DIFFERENT_WEAVE_OR_SURFACE_DESIGN",
    "DIFFERENT_EDGE_OR_BORDER_LAYOUT",
}

PROMPT = """
你在核验 Amazon 和 TEMU 的浴巾套装是否为同一基础产品。只抽取双边事实和关系，不计算分数，不输出最终同款结论。

先分离浴巾本体与浴室背景、模特、包装、尺寸线、促销文字、品牌水印。不得把折叠方式、拍摄光线、滤镜、颜色偏差或摆放顺序当作产品身份差异。

严格比较四个可由两张主图判断的模块：
1. surface_design_weave_border：印花、提花、织出条纹和装饰带的样式、数量、间距，以及相对毛巾边缘的位置。拍摄视角、旋转或折叠不同不改变对应位置；遮挡时标未知。
2. towel_construction_pile_edge：可见毛圈、华夫格、剪绒等材质外观及缝边、包边和挂环；不得据此外推棉/涤纶等纤维成分。
3. set_composition：主图中可见的毛巾件数、类型组合及套装构成。
4. color_pattern_variant：颜色是次要因素；可见颜色、色块和颜色变体，主图案布局归模块1。

材质、精确尺寸、GSM、吸水性、快干、低扭/零扭等无法由两张主图可靠验证的属性不进入评分。

硬冲突只能在 relation=DIFFERENT 且两侧均有短证据时输出：可见产品子类不同、织法/表面主设计不同、标志性边框/装饰带布局不同。
“都是浴巾套装”、“都是棉”或颜色接近不足以证明同款。
""".strip()


def _score(maximum: int, relation: str) -> int | None:
    factor = RELATION_SCORES[relation]
    return None if factor is None else round(maximum * factor)


def _evidence(value: Any) -> str:
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, list):
        return "；".join(item.strip() for item in value if isinstance(item, str) and item.strip())
    return ""


def score_facts(value: Mapping[str, Any]) -> dict[str, Any]:
    raw_modules = value.get("modules")
    if not isinstance(raw_modules, Mapping) or not set(MODULES).issubset(raw_modules):
        raise ValueError("modules must contain all configured bath-towel visual modules")
    clean: dict[str, Any] = {}
    hard_conflicts: list[str] = []
    known = 0
    identity_mismatches = set()
    for name, maximum in MODULES.items():
        row = raw_modules[name]
        if not isinstance(row, Mapping):
            raise ValueError(f"invalid module object for {name}")
        relation = str(row.get("relation") or "").upper()
        if relation not in RELATION_SCORES:
            raise ValueError(f"invalid relation for {name}")
        left = _evidence(row.get("amazon_evidence"))
        right = _evidence(row.get("temu_evidence"))
        if relation != "UNKNOWN" and (not left or not right):
            relation = "UNKNOWN"
        score = _score(maximum, relation)
        code = row.get("hard_conflict_code")
        # The provider JSON schema uses the explicit sentinel ``NONE``;
        # deterministic scoring stores absence as Python None.
        if code == "NONE":
            code = None
        if code is not None and code not in HARD_CODES:
            raise ValueError(f"invalid hard conflict code for {name}")
        if code and relation == "DIFFERENT" and left and right:
            hard_conflicts.append(code)
        if relation != "UNKNOWN":
            known += 1
        # Similar appearance is insufficient to confirm identity, but is not
        # affirmative evidence of a different product either.
        if relation == "DIFFERENT":
            identity_mismatches.add(name)
        clean[name] = {**row, "relation": relation, "score": score, "maximum": maximum,
                       "amazon_evidence": left, "temu_evidence": right}

    scored = [row["score"] for row in clean.values() if row["score"] is not None]
    total = sum(scored) if len(scored) == len(MODULES) else None
    confidence = value.get("overall_confidence")
    confidence = float(confidence) if isinstance(confidence, (int, float)) and not isinstance(confidence, bool) else 0.0
    if not math.isfinite(confidence) or not 0 <= confidence <= 1:
        confidence = 0.0
    quality = "HIGH" if confidence >= 0.80 and known >= 4 else "MEDIUM" if confidence >= 0.55 and known >= 3 else "LOW"
    minimums_pass = all((clean[name]["score"] or 0) >= minimum for name, minimum in MINIMUMS.items())
    key_known = all(clean[name]["relation"] != "UNKNOWN" for name in KEY_MODULES)
    # Reconstruct from the retained visual modules. Legacy seven-module
    # material/dimension labels must never affect a four-module conclusion.
    variants = [name for name, row in clean.items() if row["relation"] == "VARIANT"]
    if hard_conflicts:
        state, reason = "不同款", "存在双边明确的浴巾套装身份级硬冲突"
    elif total is None:
        state, reason = "待补充证据", "四个视觉模块中存在未知项，单独进入补证队列，不进入人工待复核队列"
    elif total is not None and total >= SAME_THRESHOLD and minimums_pass and known == len(MODULES) and key_known and quality in {"HIGH", "MEDIUM"}:
        state, reason = ("同款（存在规格变体）" if variants else "确认同款"), "总分和关键模块均达标，且无硬冲突"
    elif total is not None and total <= DIFFERENT_SCORE_CEILING and quality in {"HIGH", "MEDIUM"} and identity_mismatches & KEY_MODULES:
        state, reason = "不同款", "纯视觉总分不高于69，且关键身份模块存在双边明确不同证据"
    else:
        state, reason = "待复核", "四个视觉模块评分完整，但处于中间分带或未满足关键模块门槛"
    return {
        "rule_version": SCORING_VERSION,
        "threshold_status": "首版待人工真值集校准",
        "modules": clean,
        "total_score": total,
        "state": state,
        "reason": reason,
        "hard_conflicts": hard_conflicts,
        "known_module_count": known,
        "evidence_quality": quality,
        "variant_differences": variants,
        "uncertainties": [name for name, row in clean.items() if row["relation"] == "UNKNOWN"],
    }
