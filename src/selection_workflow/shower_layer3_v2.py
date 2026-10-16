"""Deterministic third-layer scoring contract for shower-curtain matching.

The VLM extracts facts and proposes bounded module scores. Python validates the
schema, recomputes the total, applies hard gates, and produces the final state.
The model's self-reported total or recommendation is never trusted directly.
"""
from __future__ import annotations

import json
from typing import Any, Mapping

RELATIONS = {"SAME", "VARIANT", "DIFFERENT", "UNKNOWN"}
QUALITY = {"HIGH", "MEDIUM", "LOW"}
MODULES = {
    "design_identity": 32,
    "core_structure": 18,
    "material_opacity": 15,
    "function_subtype": 12,
    "surface_details": 10,
    "bundle_accessories": 7,
    "spec_variants": 6,
}
KEY_MODULES = {"design_identity", "core_structure", "material_opacity", "function_subtype"}
HARD_CODES = {
    "DIFFERENT_BASE_PATTERN_LAYOUT",
    "PLAIN_VS_PATTERNED",
    "DIFFERENT_CORE_MATERIAL",
    "DIFFERENT_OPACITY_CLASS",
    "DIFFERENT_PRODUCT_SUBTYPE",
    "DIFFERENT_LAYER_STRUCTURE",
    "DIFFERENT_MAIN_BUNDLE",
}
EVIDENCED_IDENTITY_HARD_CODES = HARD_CODES - {"DIFFERENT_MAIN_BUNDLE"}

RUBRIC = r"""
你是“浴帘同款核验器”，不是通用品类识别器。目标是判断两侧是否属于同一基础款/同一设计家族；尺寸、颜色、厚度、挂钩、包装文字和营销图差异可以是变体。只依据输入图片与随附来源事实，不猜品牌、工厂或隐藏参数。

【先隔离无关视觉信息】
1. 先定位浴帘主体区域。忽略浴室背景、浴缸、花盆、毛巾、地垫、人物、手机框、阴影、拍摄角度、裁切和压缩。
2. 忽略加在图片上的水印、商标、品牌名、规格数字、促销文案、箭头、尺寸线、贴纸、色块和包装标签。它们只写入 ignored_overlays，不得作为图案/版式差异。
3. 区分“印在浴帘本体上的设计文字”和“后期叠加的水印/商品说明”。只有前者属于主体图案。
4. 同一张基础商品图被换背景、镜像、裁切、调色、模糊商标或覆盖规格文字，仍可判同款。

【业务定义】
- 同款：基础产品设计、主体图案/无图案状态、核心材质类型、透明度类别、层数/衬里结构及主要功能一致。
- 同款变体：基础款一致，但颜色、尺寸、厚度（如8G/10G）、挂钩/环、赠品、包装、水印、展示场景或轻微安装配件不同。它仍属于同款。
- 不同款：主体花型/人物/文字/重复单元或版式不同；全透明与半透明属于不同透明度类别，不能作为颜色或普通规格变体；透明内衬与装饰印花布帘不同；PEVA/EVA塑料与涤纶织物等核心材质冲突；单层/双层/可拆衬里等核心结构冲突；单帘与含地垫马桶套的主套装冲突。
- “都是浴帘”“都是白色”“都有12个孔”只能证明同类，不能证明同款。

【七个评分模块，总分100】
A. 主体款式、图案与版式 0—32：
32=主体设计实质相同，仅水印/背景/裁切/调色不同；27—31=同一基础设计，存在颜色或展示差异；18—26=疑似同一设计但关键区域被遮挡；8—17=仅通用品类外观相似；0—7=主体图案、重复单元或版式实质不同。主体平纹与印花、不同主题花型构成硬冲突。
B. 核心结构与安装 0—18：
18=层数、顶部、衬里、开合方式一致；14—17=主体结构一致，仅普通孔眼/大环/随附挂钩等变体；8—13=结构部分可见且大体接近；1—7=核心结构明显不一致；0=单层与双层/可拆衬里等核心结构冲突。
C. 材质、透明度与厚度 0—15：
15=核心材质和透明度类别一致；12—14=同材质且同透明度类别，仅8G/10G等厚度不同；8—11=材质表述接近但透明度证据不完整；1—7=外观或文字存在较大冲突；0=塑料内衬与涤纶布帘、全透明与半透明、全透明与不透明、半透明与不透明等透明度类别冲突。
D. 功能与产品子类型 0—12：
12=同为内衬/装饰帘/双层套装等同一子类型；9—11=同一基础功能，销售命名不同；4—8=只能确认同属浴帘；0—3=内衬、装饰帘、浴帘套装等子类型冲突，明确冲突为硬冲突。
E. 表面纹理与工艺细节 0—10：
10=华夫格、纵向纹理、褶皱、边缝、孔眼、磁石/配重等一致；7—9=主体工艺一致，仅小配件不同；4—6=部分可见；0—3=关键纹理或工艺不同。
F. 套装与配件 0—7：
7=主体与主要部件一致；5—6=仅挂钩/赠品/包装差异；2—4=信息不完整；0—1=单帘与四件套等主要商品构成冲突，明确冲突为硬冲突。
G. 规格与可接受变体 0—6：
6=规格一致；5=尺寸、颜色、厚度或挂钩属于正常SKU变体；3—4=规格部分缺失；0—2=规格明显不兼容。单独的颜色、尺寸或厚度差异不得作为硬冲突。

【硬冲突代码】
只允许：DIFFERENT_BASE_PATTERN_LAYOUT、PLAIN_VS_PATTERNED、DIFFERENT_CORE_MATERIAL、DIFFERENT_OPACITY_CLASS、DIFFERENT_PRODUCT_SUBTYPE、DIFFERENT_LAYER_STRUCTURE、DIFFERENT_MAIN_BUNDLE。水印、品牌、规格文字、背景、颜色、尺寸、厚度、挂钩或环的差异不得生成硬冲突。

【输出要求】
API会提供固定JSON结构。逐模块填写关系、整数分、置信度、Amazon具体证据、TEMU具体证据、评分原因和允许的硬冲突代码。关系只能是 SAME、VARIANT、DIFFERENT、UNKNOWN。证据必须描述当前图片或来源文字中的具体内容。严禁复制“事实”“主体区域与遮挡”“评分原因”“忽略的水印/标签/背景”等模板字样。没有具体证据就写UNKNOWN，不要编造。不要仅输出结论，不要把类别相同写成商品身份相同，不要自行决定最终总分。
""".strip()


def build_prompt(amazon: Mapping[str, Any], temu: Mapping[str, Any]) -> str:
    """Build the complete evidence-grounded prompt for one pair."""
    return RUBRIC + "\n\n【Amazon来源事实】\n" + json.dumps(amazon, ensure_ascii=False) + "\n【TEMU来源事实】\n" + json.dumps(temu, ensure_ascii=False)


def validate_and_score(value: Mapping[str, Any]) -> dict[str, Any]:
    """Validate VLM modules, recompute total, and apply deterministic gates."""
    modules = value.get("modules")
    if not isinstance(modules, Mapping) or set(modules) != set(MODULES):
        raise ValueError("第三层模块不完整")
    clean: dict[str, dict[str, Any]] = {}
    accepted_conflicts: list[str] = []
    known = 0
    for name, maximum in MODULES.items():
        row = modules[name]
        if not isinstance(row, Mapping):
            raise ValueError(f"模块{name}不是对象")
        relation = str(row.get("relation", "")).upper()
        score = row.get("score")
        confidence = row.get("confidence")
        if relation not in RELATIONS:
            raise ValueError(f"模块{name}关系无效")
        if isinstance(score, bool) or not isinstance(score, int) or not 0 <= score <= maximum:
            raise ValueError(f"模块{name}分数超出0—{maximum}")
        if isinstance(confidence, bool) or not isinstance(confidence, (int, float)) or not 0 <= confidence <= 100:
            raise ValueError(f"模块{name}置信度无效")
        for key in ("amazon_evidence", "temu_evidence"):
            if not isinstance(row.get(key), list) or not all(isinstance(x, str) for x in row[key]):
                raise ValueError(f"模块{name}缺少双边证据")
        code = row.get("hard_conflict_code")
        if code is not None and code not in HARD_CODES:
            raise ValueError(f"模块{name}硬冲突代码无效")
        # A concrete bilateral identity conflict is stronger than the model's
        # uncalibrated global confidence field. Bundle-only conflicts retain
        # the stricter confidence gate because accessories are often unclear.
        if (
            code
            and relation == "DIFFERENT"
            and row["amazon_evidence"] and row["temu_evidence"]
            and (confidence >= 85 or code in EVIDENCED_IDENTITY_HARD_CODES)
        ):
            accepted_conflicts.append(code)
        if relation != "UNKNOWN":
            known += 1
        clean[name] = {
            "relation": relation,
            "score": score,
            "confidence": float(confidence),
            "amazon_evidence": row["amazon_evidence"][:6],
            "temu_evidence": row["temu_evidence"][:6],
            "reason": str(row.get("reason") or ""),
            "hard_conflict_code": code,
        }

    total = sum(row["score"] for row in clean.values())
    quality = str(value.get("evidence_quality", "LOW")).upper()
    if quality not in QUALITY:
        quality = "LOW"
    key_unknown = any(clean[name]["relation"] == "UNKNOWN" for name in KEY_MODULES)
    minimums_pass = (
        clean["design_identity"]["score"] >= 27
        and clean["core_structure"]["score"] >= 14
        and clean["material_opacity"]["score"] >= 10
        and clean["function_subtype"]["score"] >= 9
    )
    variants = [str(x) for x in value.get("variant_differences", []) if isinstance(x, str)]
    evidenced_differences = {
        name for name, row in clean.items()
        if row["relation"] == "DIFFERENT"
        and row["amazon_evidence"] and row["temu_evidence"]
    }
    decisive_low_score_exclusion = (
        total <= 71
        and known >= 5
        and len(evidenced_differences) >= 2
        and bool(evidenced_differences & KEY_MODULES)
    )

    if accepted_conflicts:
        state = "不同款"
        reason = "存在双边明确的身份级硬冲突：" + "、".join(accepted_conflicts)
    elif total >= 86 and minimums_pass and known >= 6 and quality in {"HIGH", "MEDIUM"} and not key_unknown:
        state = "同款（存在规格变体）" if variants else "确认同款"
        reason = "总分不低于86，关键模块达到下限，无硬冲突且核心证据完整。"
    elif total <= 71 and quality in {"HIGH", "MEDIUM"} and known >= 5:
        state = "不同款"
        reason = "总分不高于71，且已有至少五个有效模块支持排除。"
    elif decisive_low_score_exclusion:
        state = "不同款"
        reason = "总分不高于71，且至少五个模块有效、一个关键身份模块及另一模块均有双边不同证据。"
    else:
        state = "待复核"
        reason = "处于72—85分区间，或关键模块/证据质量未满足自动判定条件。"

    return {
        "modules": clean,
        "module_scores": {name: clean[name]["score"] for name in MODULES},
        "total_score": total,
        "threshold": 86,
        "state": state,
        "reason": reason,
        "hard_conflicts": accepted_conflicts,
        "variant_differences": variants,
        "uncertainties": [str(x) for x in value.get("uncertainties", []) if isinstance(x, str)],
        "evidence_quality": quality,
        "known_module_count": known,
    }


def _response_schema() -> dict[str, Any]:
    module_schema = {
        "type": "object",
        "required": ["relation", "score", "confidence", "amazon_evidence", "temu_evidence", "reason", "hard_conflict_code"],
        "properties": {
            "relation": {"type": "string", "enum": sorted(RELATIONS)},
            "score": {"type": "integer", "minimum": 0},
            "confidence": {"type": "number", "minimum": 0, "maximum": 100},
            "amazon_evidence": {"type": "array", "items": {"type": "string"}},
            "temu_evidence": {"type": "array", "items": {"type": "string"}},
            "reason": {"type": "string"},
            "hard_conflict_code": {"type": ["string", "null"], "enum": sorted(HARD_CODES) + [None]},
        },
    }
    return {
        "type": "object",
        "required": ["product_mask_summary", "ignored_overlays", "modules", "variant_differences", "uncertainties", "evidence_quality"],
        "properties": {
            "product_mask_summary": {"type": "object", "required": ["amazon", "temu"], "properties": {"amazon": {"type": "string"}, "temu": {"type": "string"}}},
            "ignored_overlays": {"type": "array", "items": {"type": "string"}},
            "modules": {"type": "object", "required": list(MODULES), "properties": {name: module_schema for name in MODULES}},
            "variant_differences": {"type": "array", "items": {"type": "string"}},
            "uncertainties": {"type": "array", "items": {"type": "string"}},
            "evidence_quality": {"type": "string", "enum": sorted(QUALITY)},
        },
    }


def compare_images_v2(*, profile: Mapping[str, Any], amazon_image_paths=(), temu_image_paths=(),
                      amazon_facts: Mapping[str, Any] | None = None,
                      temu_facts: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Historical Ollama transport retained only for reproducing legacy runs.

    Current production runs use shower_openai_responses.py; the shared scoring
    validator in this module remains current.
    """
    from selection_workflow.shower_vision import (
        _extract_json, _image_payload, _post_json, vision_configuration,
    )
    import urllib.error

    config = vision_configuration(profile)
    if not config["configured"]:
        return {"status": "NEEDS_EVIDENCE", "reason": "VISION_CONFIGURATION_MISSING", "facts": [], "missing_configuration": config["missing"]}
    amazon_images, amazon_evidence = _image_payload(amazon_image_paths)
    temu_images, temu_evidence = _image_payload(temu_image_paths)
    input_evidence = {"amazon": amazon_evidence, "temu": temu_evidence}
    if not amazon_images or not temu_images:
        return {"status": "NEEDS_EVIDENCE", "reason": "IMAGE_BYTES_NOT_COLLECTED", "facts": [], "input_evidence": input_evidence}
    prompt = build_prompt(amazon_facts or {}, temu_facts or {})
    payload = {
        "model": config["model"], "stream": False,
        "messages": [
            {"role": "user", "content": "Amazon图片组。请先记住图片，再阅读下一条TEMU图片和完整规则。", "images": amazon_images},
            {"role": "user", "content": prompt + "\n\nTEMU图片组如下。现在比较两侧并严格输出JSON。", "images": temu_images},
        ],
        "options": {"temperature": 0},
        "format": _response_schema(),
    }
    try:
        response = _post_json(f"{config['endpoint'].rstrip('/')}/api/chat", payload, int(profile["vision"]["timeout_seconds"]))
    except (OSError, ValueError, urllib.error.URLError, urllib.error.HTTPError) as exc:
        return {"status": "NEEDS_EVIDENCE", "reason": "VISION_PROVIDER_ERROR", "facts": [], "provider_error": str(exc), "input_evidence": input_evidence}
    content = response.get("message", {}).get("content") if isinstance(response.get("message"), Mapping) else None
    parsed = _extract_json(content) if isinstance(content, str) else None
    if not parsed:
        return {"status": "NEEDS_EVIDENCE", "reason": "VISION_INVALID_JSON", "facts": [], "raw_response": content, "input_evidence": input_evidence}
    try:
        scored = validate_and_score(parsed)
    except (ValueError, TypeError) as exc:
        return {"status": "NEEDS_EVIDENCE", "reason": "VISION_INVALID_V2_SCHEMA", "facts": [], "schema_error": str(exc), "raw_response": content, "input_evidence": input_evidence}
    return {
        "status": "EVIDENCED", "reason": "OLLAMA_VLM_V2_SCORED_FACTS",
        "facts": [scored], "provider": "OLLAMA", "model": config["model"],
        "prompt_version": "shower-layer3-v2.0", "input_evidence": input_evidence,
    }
