"""Conservative, bilateral text gate for shower-curtain matching.

Stage 1 is a gate, not a similarity score. It excludes only explicit and
identity-critical contradictions supported by text on both sides. Missing
facts, ordinary SKU variants, and installation-accessory differences advance
to image-vector processing.
"""
from __future__ import annotations

import re
from typing import Any, Mapping

THEMES = {
    "pokemon": (r"pokemon|pikachu|宝可梦|皮卡丘", "宝可梦主题"),
    "halloween": (r"halloween|ghost|pumpkin|skull|bat\b|万圣|幽灵|南瓜|骷髅", "万圣节主题"),
    "christmas": (r"christmas|santa|snowman|reindeer|圣诞|雪人|驯鹿", "圣诞主题"),
    "marble": (r"marble|大理石", "大理石纹"),
    "floral": (r"floral|flower|rose|botanical|花卉|鲜花|玫瑰|植物", "花卉植物"),
    "geometric": (r"geometric|quadrilateral|grid|stripe|几何|网格|条纹", "几何条纹"),
    "forest": (r"forest|tree|woodland|森林|树木", "森林树木"),
    "animal": (r"dinosaur|cat\b|dog\b|butterfly|bird|动物|恐龙|猫|狗|蝴蝶|鸟", "动物主题"),
}


def _has(pattern: str, text: str) -> bool:
    from selection_automation.rules import uncertain_alias_context
    return any(not uncertain_alias_context(text,m.start(),m.end()) for m in re.finditer(pattern,text,re.I))


def extract_text_facts(text: str) -> dict[str, Any]:
    raw = str(text or "").strip()
    t = raw.lower()
    materials = set()
    if _has(r"\bpeva\b|\beva\b|乙烯|塑料内衬", t): materials.add("PEVA_EVA")
    if _has(r"\bpvc\b|polyvinyl|聚氯乙烯", t): materials.add("PVC")
    if _has(r"polyester|聚酯", t): materials.add("POLYESTER")
    if _has(r"\bfabric\b|\bcloth\b|织物|布帘", t): materials.add("FABRIC_UNSPECIFIED")

    opacity = set()
    translucent = _has(r"semi[- ]?transparent|translucent|半透明|frosted|磨砂", t)
    clear_scope = re.sub(r"semi[- ]?transparent|translucent|半透明|frosted|磨砂", " ", t)
    if _has(r"\bclear\b|\btransparent\b|see[- ]?through|全透明|完全透明|(?<!半)透明", clear_scope): opacity.add("CLEAR")
    if translucent: opacity.add("TRANSLUCENT")
    if _has(r"opaque|blackout|不透明|遮光", t): opacity.add("OPAQUE")

    subtype = set()
    double_layer = _has(r"double[- ]?layer|two[- ]?layer|snap[- ]?in liner|detachable liner|双层|可拆.*衬", t)
    bath_set = _has(r"(?:3|4|5)\s*(?:pcs?|piece)|toilet lid|bath mat|contour mat|浴室套装|马桶.*垫|地垫", t)
    liner = _has(r"shower (?:curtain )?liner|liner set|内衬", t)
    if double_layer: subtype.add("DOUBLE_LAYER_CURTAIN")
    if bath_set: subtype.add("BATHROOM_SET")
    if liner and not double_layer: subtype.add("LINER")
    if _has(r"shower curtain|bath curtain|浴帘", t) and not liner and not bath_set and not double_layer:
        subtype.add("CURTAIN_UNSPECIFIED")

    themes = {key for key, (pattern, _) in THEMES.items() if _has(pattern, t)}
    explicit_patterned = _has(r"print(?:ed)?|pattern(?:ed)?|floral|flower|marble|geometric|watercolor|图案|印花|花卉|大理石", t) or bool(themes)
    explicit_plain = _has(r"plain|solid color|solid white|no pattern|无图案|纯色", t)
    # A transparent/clear liner is treated as unprinted only when text also
    # identifies it as a liner/plastic product, not merely because 'clear' is
    # used as marketing language.
    clear_unprinted = "CLEAR" in opacity and ("LINER" in subtype or "PEVA_EVA" in materials or "PVC" in materials) and not explicit_patterned
    if "CURTAIN_UNSPECIFIED" in subtype and (explicit_patterned or bool(set(materials) & {"POLYESTER", "FABRIC_UNSPECIFIED"}) or "OPAQUE" in opacity):
        subtype.remove("CURTAIN_UNSPECIFIED")
        subtype.add("DECORATIVE_CURTAIN")

    thickness = sorted(set(re.findall(r"\b(\d{1,2})\s*g\b", t)))
    sizes = sorted(set(re.findall(r"\b(\d{2,3})\s*(?:[x×]\s*(\d{2,3}))?\s*(?:inch|inches|\")", t)))
    variants = []
    if _has(r"hooks?|grommets?|rings?|挂钩|孔眼|大环", t): variants.append("吊挂配件")
    if thickness: variants.append("厚度")
    if sizes: variants.append("尺寸")
    if _has(r"white|black|blue|green|pink|gray|red|白|黑|蓝|绿|粉|灰|红", t): variants.append("颜色")

    return {
        "raw": raw,
        "materials": sorted(materials), "opacity": sorted(opacity),
        "subtype": sorted(subtype), "themes": sorted(themes),
        "explicit_patterned": explicit_patterned,
        "explicit_plain_or_clear_unprinted": explicit_plain or clear_unprinted,
        "thickness": thickness, "sizes": sizes, "variant_dimensions": sorted(set(variants)),
    }


def evaluate_text_gate(amazon: str | Mapping[str, Any], temu: str | Mapping[str, Any]) -> dict[str, Any]:
    """Return TEXT_HARD_EXCLUDE, TEXT_CLEAR, or TEXT_INSUFFICIENT."""
    af = extract_text_facts(amazon if isinstance(amazon, str) else amazon.get("text", ""))
    tf = extract_text_facts(temu if isinstance(temu, str) else temu.get("text", ""))
    conflicts: list[dict[str, Any]] = []

    def conflict(code: str, dimension: str, reason: str) -> None:
        conflicts.append({
            "code": code, "dimension": dimension, "reason": reason,
            "amazon_evidence": af["raw"], "temu_evidence": tf["raw"],
        })

    # Pattern conflicts require explicit bilateral evidence. Absence of a
    # pattern word is never treated as plain/unprinted.
    if af["explicit_plain_or_clear_unprinted"] and tf["explicit_patterned"]:
        conflict("PLAIN_VS_PATTERNED", "主体图案", "Amazon明确为纯色/透明无印花，TEMU明确为印花")
    elif tf["explicit_plain_or_clear_unprinted"] and af["explicit_patterned"]:
        conflict("PLAIN_VS_PATTERNED", "主体图案", "TEMU明确为纯色/透明无印花，Amazon明确为印花")
    elif af["themes"] and tf["themes"] and set(af["themes"]).isdisjoint(tf["themes"]):
        conflict("DIFFERENT_BASE_PATTERN_LAYOUT", "主体图案", "双边均明确描述不同主题图案")

    # PEVA/EVA/PVC plastic and polyester/fabric are different core product
    # constructions only when both sides state them explicitly.
    a_plastic = bool(set(af["materials"]) & {"PEVA_EVA", "PVC"})
    t_plastic = bool(set(tf["materials"]) & {"PEVA_EVA", "PVC"})
    a_fabric = bool(set(af["materials"]) & {"POLYESTER", "FABRIC_UNSPECIFIED"})
    t_fabric = bool(set(tf["materials"]) & {"POLYESTER", "FABRIC_UNSPECIFIED"})
    if (a_plastic and t_fabric) or (t_plastic and a_fabric):
        conflict("DIFFERENT_CORE_MATERIAL", "核心材质", "双边明确为塑料类与织物类核心材质")

    if af["opacity"] and tf["opacity"] and set(af["opacity"]).isdisjoint(tf["opacity"]):
        conflict("DIFFERENT_OPACITY_CLASS", "透明度", "双边明确属于不同透明度类别（全透明、半透明、不透明不可互换）")

    a_types, t_types = set(af["subtype"]), set(tf["subtype"])
    if ("LINER" in a_types and bool(t_types & {"DECORATIVE_CURTAIN", "BATHROOM_SET"})) or ("LINER" in t_types and bool(a_types & {"DECORATIVE_CURTAIN", "BATHROOM_SET"})):
        conflict("DIFFERENT_PRODUCT_SUBTYPE", "产品子类型", "双边明确为浴帘内衬与装饰帘/浴室套装")
    if ("BATHROOM_SET" in a_types and bool(t_types & {"LINER", "DECORATIVE_CURTAIN"})) or ("BATHROOM_SET" in t_types and bool(a_types & {"LINER", "DECORATIVE_CURTAIN"})):
        conflict("DIFFERENT_MAIN_BUNDLE", "主要套装", "一侧明确为多件浴室套装，另一侧明确为单帘类商品")

    bilateral_dimensions = sum(bool(af[key]) and bool(tf[key]) for key in ("materials", "opacity", "subtype"))
    bilateral_dimensions += int(bool(af["themes"] or af["explicit_plain_or_clear_unprinted"]) and bool(tf["themes"] or tf["explicit_plain_or_clear_unprinted"]))
    if conflicts:
        status = "TEXT_HARD_EXCLUDE"
    elif bilateral_dimensions >= 2:
        status = "TEXT_CLEAR"
    else:
        status = "TEXT_INSUFFICIENT"

    variant_differences = []
    if af["thickness"] and tf["thickness"] and af["thickness"] != tf["thickness"]:
        variant_differences.append("厚度变体")
    if af["sizes"] and tf["sizes"] and af["sizes"] != tf["sizes"]:
        variant_differences.append("尺寸变体")
    # Hook/ring wording is deliberately not compared as a hard conflict.
    if "吊挂配件" in af["variant_dimensions"] or "吊挂配件" in tf["variant_dimensions"]:
        variant_differences.append("吊挂配件待视觉确认")

    return {
        "stage1_status": status,
        "advance_to_image_vector": status != "TEXT_HARD_EXCLUDE",
        "text_score": None,
        "hard_conflicts": conflicts,
        "variant_differences": variant_differences,
        "amazon_facts": af,
        "temu_facts": tf,
        "bilateral_fact_dimensions": bilateral_dimensions,
        "reason": "存在双边明确的身份级文字硬冲突" if conflicts else ("未发现双边文字硬冲突" if status == "TEXT_CLEAR" else "文字事实不足，不排除并进入图片向量层"),
    }
