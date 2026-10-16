"""OpenAI Responses API七模块视觉事实抽取与确定性计分。"""
from __future__ import annotations

import base64
import io
import json
import mimetypes
import os
import re
import time
import hashlib
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlsplit

from selection_workflow.shower_layer3_v2 import MODULES, validate_and_score


DEFAULT_MODEL = "gpt-5.6-luna"
MODEL_ENV = "OPENAI_VISION_MODEL"
REASONING_EFFORT_ENV = "OPENAI_VISION_REASONING_EFFORT"
API_KEY_ENV = "OPENAI_API_KEY"
API_BASE_ENV = "OPENAI_BASE_URL"
PROMPT_VERSION = "shower-openai-module-facts-v2.3-provider-neutral"
FACT_SCHEMA_VERSION = "shower-module-facts-v2.1-compact"
IMAGE_INPUT_VERSION = "api-image-max1600-jpeg90-444-v1"
LEGACY_COMPACT_COMPATIBILITY = (
    ("shower-openai-module-facts-v2.0", "shower-module-facts-v2.0", None),
    ("shower-openai-module-facts-v2.1-compact", "shower-module-facts-v2.1-compact", IMAGE_INPUT_VERSION),
    ("shower-openai-module-facts-v2.2-confidence-calibrated", "shower-module-facts-v2.1-compact", IMAGE_INPUT_VERSION),
)
FACT_RELATIONS = {"same", "variant", "different", "uncertain"}
HARD_CONFLICT_CODES = {
    "NONE", "DIFFERENT_BASE_PATTERN_LAYOUT", "PLAIN_VS_PATTERNED",
    "DIFFERENT_CORE_MATERIAL", "DIFFERENT_OPACITY_CLASS",
    "DIFFERENT_PRODUCT_SUBTYPE", "DIFFERENT_LAYER_STRUCTURE", "DIFFERENT_MAIN_BUNDLE",
}


_MODULE_FACT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["relation", "amazon_evidence", "temu_evidence", "comparison_reason", "hard_conflict_code"],
    "properties": {
        "relation": {"type": "string", "enum": sorted(FACT_RELATIONS)},
        "amazon_evidence": {"type": "array", "maxItems": 1, "items": {"type": "string"}},
        "temu_evidence": {"type": "array", "maxItems": 1, "items": {"type": "string"}},
        "comparison_reason": {"type": "string"},
        "hard_conflict_code": {"type": "string", "enum": sorted(HARD_CONFLICT_CODES)},
    },
}

FACT_SCHEMA: dict[str, Any] = {
    "type": "object", "additionalProperties": False,
    "required": ["modules", "ignored_elements", "overall_confidence"],
    "properties": {
        "modules": {
            "type": "object", "additionalProperties": False,
            "required": list(MODULES),
            "properties": {name: _MODULE_FACT_SCHEMA for name in MODULES},
        },
        "ignored_elements": {"type": "array", "maxItems": 3, "items": {"type": "string"}},
        "overall_confidence": {"type": "number", "minimum": 0, "maximum": 1},
    },
}


FIXED_RELATION_PROMPT = """
你是浴帘商品的视觉事实抽取器。第一张图片是Amazon主图，第二张是TEMU主图。只输出七个业务模块的可见事实关系，禁止输出分数、总分和最终同款结论；所有分数由Python规则计算。

七模块：design_identity主体款式/图案/版式；core_structure层数/衬里/顶部安装；material_opacity核心材质外观与全透明/半透明磨砂/不透明类别；function_subtype内衬/装饰帘/双层套装等子类型；surface_details纹理/褶皱/边缝/孔眼/磁石配重；bundle_accessories主体套装和主要配件；spec_variants尺寸/普通颜色/厚度/挂钩等可接受变体。

relation只能是same、variant、different、uncertain。每个模块分别写Amazon可见证据、TEMU可见证据和具体比较原因。看不见就写uncertain，不得用类别常识猜测材质、层数或隐藏部件。全透明CLEAR、半透明/磨砂TRANSLUCENT、不透明OPAQUE是三个身份级类别，双边明确不同时使用DIFFERENT_OPACITY_CLASS。主体基础图案或版式不同可使用DIFFERENT_BASE_PATTERN_LAYOUT；纯色/透明无印花与主体印花冲突可使用PLAIN_VS_PATTERNED。只有双边证据明确时才能输出其他硬冲突代码，否则hard_conflict_code写NONE。

来源标题和规格只能补充图片无法直接看清的文字事实，不能冒充可见证据。不得仅因两边都叫浴帘、都是纯色、颜色接近、都有顶部孔或标题关键词相似就输出same；确认主体设计相同必须指出两图共有的可区分图案、版式、结构或纹理。设计、纹理和透明度硬冲突必须来自两张图片中均可定位的具体差异；一侧主体严重遮挡、过小或不可辨认时写uncertain，不得仅依据标题生成视觉硬冲突。

先定位浴帘本体。忽略浴室背景、人物、浴缸、地垫、拍摄角度、裁切、镜像、调色、水印、商标遮罩、促销文字、尺寸线、箭头、贴纸和包装标签；把这些写入ignored_elements。印在浴帘本体上的设计文字仍属于主体图案。普通颜色、尺寸、8G/10G厚度、挂钩和包装差异不得单独形成硬冲突。

输出必须精简：每侧每模块最多一条最关键的短证据；comparison_reason只写一条不重复证据的短句；ignored_elements最多三项。不得复述标题、规则、模块名或输出分析过程。Python会根据这些结构化事实评分，模型不得评分。

overall_confidence必须填写0到1之间的小数，表示你对“图片是否看清、七模块事实是否有足够双边可见证据”的总体把握度，不是同款概率。两图主体清楚且至少六个模块有双边证据时可填0.80到0.99；四到五个模块可判断或存在局部遮挡时填0.55到0.79；证据更少时填0.01到0.54。只有两张图片均无法读取、主体完全不可辨认，并且七个模块全部为uncertain时才允许填写0。只要任一模块输出same、variant或different且有Amazon和TEMU双边证据，overall_confidence就必须大于0。
""".strip()


def model_name() -> str:
    return os.getenv(MODEL_ENV, DEFAULT_MODEL).strip() or DEFAULT_MODEL


def provider_endpoint_identity() -> str | None:
    """Bind non-OpenAI model caches to their real API endpoint without secrets."""
    selected = model_name().lower()
    if not selected.startswith("deepseek"):
        return None
    raw = os.getenv(API_BASE_ENV, "").strip()
    if not raw:
        return "missing-deepseek-endpoint"
    parsed = urlsplit(raw if "://" in raw else f"https://{raw}")
    host = (parsed.hostname or "").lower()
    path = parsed.path.rstrip("/")
    return f"{host}{path}"


def reasoning_effort() -> str:
    value = os.getenv(REASONING_EFFORT_ENV, "low").strip().lower() or "low"
    allowed = {"none", "minimal", "low", "medium", "high", "xhigh"}
    if value not in allowed:
        raise ValueError(f"{REASONING_EFFORT_ENV}取值无效")
    return value


def _api_image_bytes(path: str | Path) -> tuple[bytes, str]:
    from .api_image_input import ImageInputError, validate_api_image
    file_path = Path(path)
    original = file_path.read_bytes()
    mime = mimetypes.guess_type(file_path.name)[0] or "image/jpeg"
    from PIL import Image, ImageOps

    try:
        with Image.open(io.BytesIO(original)) as image:
            validate_api_image(image)
            # Cached .img files need their decoded format, not their suffix.
            mime = Image.MIME.get(image.format, mime)
            image = ImageOps.exif_transpose(image)
            if max(image.size) <= 1600:
                return original, mime
            image.thumbnail((1600, 1600), Image.Resampling.LANCZOS)
            if image.mode not in {"RGB", "L"}:
                has_alpha = "A" in image.getbands() or (image.mode == "P" and "transparency" in image.info)
                if has_alpha:
                    image = image.convert("RGBA")
                    background = Image.new("RGB", image.size, "white")
                    background.paste(image, mask=image.getchannel("A"))
                    image = background
                else:
                    image = image.convert("RGB")
            output = io.BytesIO()
            image.save(output, format="JPEG", quality=90, subsampling=0, optimize=True)
            return output.getvalue(), "image/jpeg"
    except ImageInputError:
        raise
    except Exception as exc:
        raise ImageInputError('IMAGE_DECODE_FAILED') from exc


def _data_url(path: str | Path) -> str:
    content, mime = _api_image_bytes(path)
    encoded = base64.b64encode(content).decode("ascii")
    return f"data:{mime};base64,{encoded}"


def _sanitize_error(error: BaseException | str) -> str:
    text = str(error)
    secret = os.getenv(API_KEY_ENV, "")
    if secret:
        text = text.replace(secret, "[已隐藏]")
    # API errors may echo a partly masked key; redact the whole token-shaped
    # fragment rather than relying on the exact secret value.
    text = re.sub(r"sk-[^\s'\",}\]]+", "[已隐藏]", text)
    return text.replace("\r", " ").replace("\n", " ")[:800]


def uncertain_facts() -> dict[str, Any]:
    return {
        "modules": {
            name: {"relation": "uncertain", "amazon_evidence": [], "temu_evidence": [],
                   "comparison_reason": "视觉请求失败或证据不足", "hard_conflict_code": "NONE"}
            for name in MODULES
        },
        "ignored_elements": [],
        "overall_confidence": 0.0,
    }


def validate_facts(value: Mapping[str, Any]) -> dict[str, Any]:
    # 历史检查点继续可读，但新API请求只使用七模块Schema。
    if "same_person" in value:
        allowed = {"same_person", "visual_evidence", "confidence", "same_scene", "spatial_relation"}
        if not {"same_person", "visual_evidence", "confidence"} <= set(value) or set(value) - allowed:
            raise ValueError("历史视觉事实字段不完整或包含额外字段")
        identity = str(value["same_person"]).lower()
        evidence, confidence = value["visual_evidence"], value["confidence"]
        if identity not in {"yes", "no", "uncertain"}:
            raise ValueError("历史基础产品关系无效")
        if not isinstance(evidence, list) or len(evidence) > 6 or not all(isinstance(x, str) and x.strip() for x in evidence):
            raise ValueError("历史visual_evidence必须为最多6项非空字符串")
        if isinstance(confidence, bool) or not isinstance(confidence, (int, float)) or not 0 <= confidence <= 1:
            raise ValueError("历史confidence必须在0到1之间")
        return {"same_person": identity, "visual_evidence": [x.strip() for x in evidence], "confidence": float(confidence)}

    if set(value) != {"modules", "ignored_elements", "overall_confidence"}:
        raise ValueError("七模块视觉事实顶层字段不完整或包含额外字段")
    modules = value["modules"]
    if not isinstance(modules, Mapping) or set(modules) != set(MODULES):
        raise ValueError("七模块视觉事实不完整")
    clean_modules = {}
    required = {"relation", "amazon_evidence", "temu_evidence", "comparison_reason", "hard_conflict_code"}
    for name in MODULES:
        row = modules[name]
        if not isinstance(row, Mapping) or set(row) != required:
            raise ValueError(f"模块{name}字段不完整")
        relation, code = str(row["relation"]).lower(), str(row["hard_conflict_code"])
        if relation not in FACT_RELATIONS or code not in HARD_CONFLICT_CODES:
            raise ValueError(f"模块{name}关系或硬冲突代码无效")
        evidence = {}
        for key in ("amazon_evidence", "temu_evidence"):
            items = row[key]
            if not isinstance(items, list) or len(items) > 3 or not all(isinstance(x, str) and x.strip() for x in items):
                raise ValueError(f"模块{name}的{key}无效")
            evidence[key] = [x.strip() for x in items]
        clean_modules[name] = {
            "relation": relation, **evidence,
            "comparison_reason": str(row["comparison_reason"]).strip(),
            "hard_conflict_code": code,
        }
    ignored, confidence = value["ignored_elements"], value["overall_confidence"]
    if not isinstance(ignored, list) or len(ignored) > 6 or not all(isinstance(x, str) and x.strip() for x in ignored):
        raise ValueError("ignored_elements无效")
    if isinstance(confidence, bool) or not isinstance(confidence, (int, float)) or not 0 <= confidence <= 1:
        raise ValueError("overall_confidence必须在0到1之间")
    return {"modules": clean_modules, "ignored_elements": [x.strip() for x in ignored], "overall_confidence": float(confidence)}


def _usage_dict(response: Any) -> dict[str, int]:
    usage = getattr(response, "usage", None)
    if usage is None:
        return {"input_tokens": 0, "cached_input_tokens": 0, "output_tokens": 0, "total_tokens": 0}
    input_tokens = int(getattr(usage, "input_tokens", 0) or 0)
    output_tokens = int(getattr(usage, "output_tokens", 0) or 0)
    total_tokens = int(getattr(usage, "total_tokens", input_tokens + output_tokens) or 0)
    details = getattr(usage, "input_tokens_details", None)
    cached = int(getattr(details, "cached_tokens", 0) or 0) if details is not None else 0
    return {"input_tokens": input_tokens, "cached_input_tokens": cached, "output_tokens": output_tokens, "total_tokens": total_tokens}


def extract_visual_facts(
    *,
    amazon_image: str | Path,
    temu_image: str | Path,
    amazon_title: str,
    temu_title: str,
    amazon_context: Mapping[str, Any] | None = None,
    temu_context: Mapping[str, Any] | None = None,
    timeout_seconds: float = 180.0,
    client: Any | None = None,
    max_attempts: int = 2,
) -> dict[str, Any]:
    """Call Responses API and return facts plus usage metadata.

    ``max_attempts`` remains two for small standalone callers.  The production
    full runner passes one and performs its single retry only after the primary
    queue has drained, so a transient upstream outage is not retried in the
    same failure window.
    """
    if not os.getenv(API_KEY_ENV):
        raise RuntimeError(f"缺少Windows进程环境变量{API_KEY_ENV}")
    from openai import OpenAI

    requested_model = model_name()
    client = client or OpenAI(max_retries=0, timeout=timeout_seconds)
    input_content = [
        {"type": "input_text", "text": "Amazon来源事实（只补充图片不可见信息，不覆盖可见反证）：" + json.dumps(amazon_context or {"title": amazon_title}, ensure_ascii=False)},
        {"type": "input_image", "image_url": _data_url(amazon_image), "detail": "high"},
        {"type": "input_text", "text": "TEMU来源事实（只补充图片不可见信息，不覆盖可见反证）：" + json.dumps(temu_context or {"title": temu_title}, ensure_ascii=False)},
        {"type": "input_image", "image_url": _data_url(temu_image), "detail": "high"},
    ]
    attempts: list[dict[str, Any]] = []
    accumulated = {"input_tokens": 0, "cached_input_tokens": 0, "output_tokens": 0, "total_tokens": 0}
    max_attempts = max(1, int(max_attempts))
    for attempt in range(1, max_attempts + 1):
        started = time.monotonic()
        try:
            response = client.responses.create(
                model=requested_model,
                instructions=FIXED_RELATION_PROMPT,
                input=[{"role": "user", "content": input_content}],
                text={
                    "format": {
                        "type": "json_schema",
                        "name": "shower_visual_facts",
                        "strict": True,
                        "schema": FACT_SCHEMA,
                    },
                    "verbosity": "low",
                },
                reasoning={"effort": reasoning_effort()},
                max_output_tokens=max(512, min(20000, int(os.getenv("OPENAI_VISION_MAX_OUTPUT_TOKENS", "1400")))),
                store=False,
            )
            usage = _usage_dict(response)
            for key in accumulated:
                accumulated[key] += usage[key]
            facts = validate_facts(json.loads(response.output_text))
            attempts.append({"attempt": attempt, "elapsed_seconds": round(time.monotonic() - started, 3), "status": "success", "usage": usage})
            return {
                "status": "success",
                "facts": facts,
                "model": str(getattr(response, "model", requested_model) or requested_model),
                "requested_model": requested_model,
                "response_id": str(getattr(response, "id", "") or ""),
                "usage": accumulated,
                "attempts": attempts,
                "failure_reason": None,
            }
        except Exception as exc:
            attempts.append({"attempt": attempt, "elapsed_seconds": round(time.monotonic() - started, 3), "status": "failed", "failure_reason": _sanitize_error(exc)})
            if attempt < max_attempts:
                time.sleep(3.0)
    return {
        "status": "failed",
        "facts": uncertain_facts(),
        "model": requested_model,
        "requested_model": requested_model,
        "response_id": "",
        "usage": accumulated,
        "attempts": attempts,
        "failure_reason": attempts[-1]["failure_reason"],
    }


def _legacy_facts_to_seven_modules(facts: Mapping[str, Any], pair: Mapping[str, Any], *, failure_reason: str | None = None) -> dict[str, Any]:
    """只用于读取v1历史检查点；新请求不得生成该结构。"""
    value = validate_facts(facts)
    identity = value["same_person"]
    confidence = round(value["confidence"] * 100, 2)
    evidence = value["visual_evidence"][:6]
    if not evidence:
        evidence = ["视觉事实不足"]
    variants = [str(x) for x in pair.get("variant_differences", []) if str(x).strip()]
    evidence_text = "；".join(evidence)
    amazon_text_facts = pair.get("amazon_facts") if isinstance(pair.get("amazon_facts"), Mapping) else {}
    temu_text_facts = pair.get("temu_facts") if isinstance(pair.get("temu_facts"), Mapping) else {}

    def fact_set(source: Mapping[str, Any], name: str) -> set[str]:
        raw = source.get(name, [])
        return {str(x) for x in raw} if isinstance(raw, list) else set()

    def bilateral_state(name: str) -> str:
        left, right = fact_set(amazon_text_facts, name), fact_set(temu_text_facts, name)
        if left and right:
            return "same" if left & right else "different"
        return "unknown"

    def mentions(*terms: str) -> bool:
        return any(term in evidence_text for term in terms)

    def module(relation: str, score: int, reason: str, code: str | None = None) -> dict[str, Any]:
        bilateral = [evidence_text]
        return {
            "relation": relation,
            "score": score,
            "confidence": confidence,
            "amazon_evidence": bilateral,
            "temu_evidence": bilateral,
            "reason": reason,
            "hard_conflict_code": code,
        }

    if identity == "yes":
        has_variants = bool(variants)
        design_score = max(27, round(32 * (0.80 + 0.20 * value["confidence"])))
        structure_uncertain = mentions("结构不同", "未能清晰确认", "未见相同", "分层结构不同", "安装结构不同")
        material_state = bilateral_state("materials")
        opacity_state = bilateral_state("opacity")
        subtype_state = bilateral_state("subtype")
        visual_material_match = mentions("材质外观一致", "相似的立体", "相同纹理", "相似的纹理")
        material_score = 0 if opacity_state == "different" else 15 if material_state == opacity_state == "same" else 14 if "same" in {material_state, opacity_state} or visual_material_match else 10
        function_score = 12 if subtype_state == "same" else 8 if subtype_state == "different" else 10
        surface_score = 10 if mentions("纹理", "褶皱", "表面") and not mentions("纹理明显不同", "未见相同格纹") else 7
        bundle_score = 6 if has_variants else 7
        modules = {
            "design_identity": module("SAME", design_score, "结构化视觉事实支持同一基础产品设计；分数按视觉置信度确定"),
            "core_structure": module("VARIANT" if structure_uncertain or has_variants else "SAME", 14 if structure_uncertain else 16 if has_variants else 18, "根据可见层数、衬里和安装结构事实确定"),
            "material_opacity": module("DIFFERENT" if opacity_state == "different" else "SAME" if material_score == 15 else "VARIANT" if material_score >= 12 else "UNKNOWN", material_score, "全透明与半透明为不同类别；其余根据双边文字材质、透明度事实及视觉身份共同确定", "DIFFERENT_OPACITY_CLASS" if opacity_state == "different" else None),
            "function_subtype": module("SAME" if function_score == 12 else "UNKNOWN" if function_score == 8 else "VARIANT", function_score, "根据双边产品子类型事实确定"),
            "surface_details": module("SAME" if surface_score == 10 else "VARIANT", surface_score, "根据模型返回的纹理、褶皱和表面事实确定"),
            "bundle_accessories": module("VARIANT" if has_variants else "SAME", bundle_score, "配件、包装或赠品差异按变体处理"),
            "spec_variants": module("VARIANT" if has_variants else "SAME", 5 if has_variants else 6, "尺寸、颜色、厚度与挂钩属于可接受规格变体"),
        }
    elif identity == "no":
        material_state = bilateral_state("materials")
        opacity_state = bilateral_state("opacity")
        subtype_state = bilateral_state("subtype")
        structure_score = 3 if mentions("分层结构", "层结构", "结构不同", "未见相同") else 9
        material_score = 0 if opacity_state == "different" else 12 if material_state == "same" else 8
        function_score = 10 if subtype_state == "same" else 6
        surface_score = 2 if mentions("纹理明显不同", "未见相同格纹", "表面视觉效果明显不同") else 5
        modules = {
            "design_identity": module("DIFFERENT", 0, "结构化视觉事实不支持同一基础产品设计", "DIFFERENT_BASE_PATTERN_LAYOUT"),
            "core_structure": module("DIFFERENT" if structure_score <= 3 else "UNKNOWN", structure_score, "根据可见层数、衬里和安装结构差异确定"),
            "material_opacity": module("DIFFERENT" if opacity_state == "different" else "VARIANT" if material_score >= 12 else "UNKNOWN", material_score, "全透明与半透明为不同类别；其余根据双边材质与透明度文字事实确定", "DIFFERENT_OPACITY_CLASS" if opacity_state == "different" else None),
            "function_subtype": module("VARIANT" if function_score >= 10 else "UNKNOWN", function_score, "根据双边产品子类型文字事实确定"),
            "surface_details": module("DIFFERENT" if surface_score <= 2 else "UNKNOWN", surface_score, "根据可见纹理与工艺差异确定"),
            "bundle_accessories": module("UNKNOWN", 4, "套装事实不足"),
            "spec_variants": module("UNKNOWN", 3, "规格事实不足"),
        }
    else:
        modules = {name: module("UNKNOWN", round(maximum * 0.45), "模型事实为uncertain，保留人工复核") for name, maximum in MODULES.items()}

    conflict_module = {
        "PLAIN_VS_PATTERNED": "design_identity",
        "DIFFERENT_BASE_PATTERN_LAYOUT": "design_identity",
        "DIFFERENT_CORE_MATERIAL": "material_opacity",
        "DIFFERENT_OPACITY_CLASS": "material_opacity",
        "DIFFERENT_PRODUCT_SUBTYPE": "function_subtype",
        "DIFFERENT_LAYER_STRUCTURE": "core_structure",
        "DIFFERENT_MAIN_BUNDLE": "bundle_accessories",
    }
    for conflict in pair.get("hard_conflicts", []):
        code = str(conflict.get("code") or "")
        name = conflict_module.get(code)
        if not name:
            continue
        reason = str(conflict.get("reason") or "第一层存在双边明确的文字硬冲突")
        modules[name] = module("DIFFERENT", 0, reason, code)

    quality = "HIGH" if value["confidence"] >= 0.80 else "MEDIUM" if value["confidence"] >= 0.55 else "LOW"
    payload = {
        "modules": modules,
        "variant_differences": variants,
        "uncertainties": ([failure_reason] if failure_reason else []) + ([] if identity != "uncertain" else ["视觉事实为uncertain"]),
        "evidence_quality": quality,
    }
    scored = validate_and_score(payload)
    scored["visual_facts"] = value
    scored["scoring_source"] = "PYTHON_RULES_FROM_STRUCTURED_FACTS"
    return scored


_VARIANT_SCORES = {
    "design_identity": 28, "core_structure": 15, "material_opacity": 12,
    "function_subtype": 10, "surface_details": 7, "bundle_accessories": 5, "spec_variants": 5,
}
_DIFFERENT_SCORES = {
    "design_identity": 0, "core_structure": 2, "material_opacity": 0,
    "function_subtype": 2, "surface_details": 2, "bundle_accessories": 1, "spec_variants": 2,
}


def facts_to_seven_modules(facts: Mapping[str, Any], pair: Mapping[str, Any], *, failure_reason: str | None = None) -> dict[str, Any]:
    """把七模块事实关系确定性映射为分数；兼容读取v1历史事实。"""
    if "same_person" in facts:
        return _legacy_facts_to_seven_modules(facts, pair, failure_reason=failure_reason)
    value = validate_facts(facts)
    confidence = round(value["overall_confidence"] * 100, 2)
    variants = [str(x) for x in pair.get("variant_differences", []) if str(x).strip()]
    modules = {}
    bilateral = 0
    for name, maximum in MODULES.items():
        fact = value["modules"][name]
        relation = fact["relation"]
        amazon_evidence, temu_evidence = fact["amazon_evidence"], fact["temu_evidence"]
        if amazon_evidence and temu_evidence:
            bilateral += 1
        # 没有双边证据时，不允许same/variant/different制造确定性分数。
        if relation != "uncertain" and not (amazon_evidence and temu_evidence):
            relation = "uncertain"
        if relation == "same":
            score, internal_relation = maximum, "SAME"
        elif relation == "variant":
            score, internal_relation = _VARIANT_SCORES[name], "VARIANT"
            variants.append(f"{name}存在允许变体")
        elif relation == "different":
            score, internal_relation = _DIFFERENT_SCORES[name], "DIFFERENT"
        else:
            score, internal_relation = round(maximum * 0.45), "UNKNOWN"
        code = fact["hard_conflict_code"]
        if code == "NONE" or internal_relation != "DIFFERENT":
            code = None
        modules[name] = {
            "relation": internal_relation,
            "score": int(score),
            "confidence": confidence,
            "amazon_evidence": amazon_evidence,
            "temu_evidence": temu_evidence,
            "reason": fact["comparison_reason"],
            "hard_conflict_code": code,
        }

    conflict_module = {
        "PLAIN_VS_PATTERNED": "design_identity", "DIFFERENT_BASE_PATTERN_LAYOUT": "design_identity",
        "DIFFERENT_CORE_MATERIAL": "material_opacity", "DIFFERENT_OPACITY_CLASS": "material_opacity",
        "DIFFERENT_PRODUCT_SUBTYPE": "function_subtype", "DIFFERENT_LAYER_STRUCTURE": "core_structure",
        "DIFFERENT_MAIN_BUNDLE": "bundle_accessories",
    }
    for conflict in pair.get("hard_conflicts", []):
        code = str(conflict.get("code") or "")
        name = conflict_module.get(code)
        if not name:
            continue
        reason = str(conflict.get("reason") or "第一层存在双边明确的文字硬冲突")
        modules[name] = {
            "relation": "DIFFERENT", "score": 0, "confidence": 100.0,
            "amazon_evidence": [str(conflict.get("amazon_evidence") or reason)],
            "temu_evidence": [str(conflict.get("temu_evidence") or reason)],
            "reason": reason, "hard_conflict_code": code,
        }

    if value["overall_confidence"] >= 0.80 and bilateral >= 6:
        quality = "HIGH"
    elif value["overall_confidence"] >= 0.55 and bilateral >= 4:
        quality = "MEDIUM"
    else:
        quality = "LOW"
    payload = {
        "modules": modules,
        "variant_differences": sorted(set(variants)),
        "uncertainties": ([failure_reason] if failure_reason else []) + [name for name, row in modules.items() if row["relation"] == "UNKNOWN"],
        "evidence_quality": quality,
    }
    scored = validate_and_score(payload)
    scored["visual_facts"] = value
    scored["scoring_source"] = "PYTHON_RULES_FROM_SEVEN_MODULE_FACTS_V2"
    return scored


def vision_revision_key(
    *, amazon_image: str | Path, temu_image: str | Path, pair: Mapping[str, Any],
    reasoning_effort_override: str | None = None,
) -> str:
    """输入图片、事实、规则、提示词或模型变化时自动使第三层缓存失效。"""
    effort = reasoning_effort_override or reasoning_effort()
    return vision_revision_keys(
        amazon_image=amazon_image, temu_image=temu_image, pair=pair,
        reasoning_efforts=(effort,),
    )[effort]


def vision_revision_keys(
    *, amazon_image: str | Path, temu_image: str | Path, pair: Mapping[str, Any],
    reasoning_efforts: tuple[str, ...],
    prompt_version: str = PROMPT_VERSION,
    schema_version: str = FACT_SCHEMA_VERSION,
    image_input_version: str | None = IMAGE_INPUT_VERSION,
) -> dict[str, str]:
    """一次读取图片，为多个推理强度生成兼容修订键。"""
    def digest(path: str | Path) -> str:
        return hashlib.sha256(Path(path).read_bytes()).hexdigest()

    base_payload = {
        "asin": str(pair["asin"]), "goods_id": str(pair["goods_id"]),
        "amazon_image_sha256": digest(amazon_image), "temu_image_sha256": digest(temu_image),
        "amazon_facts": pair.get("amazon_product_facts") or pair.get("amazon_facts"),
        "temu_facts": pair.get("temu_product_facts") or pair.get("temu_facts"),
        "rule_version": "shower-curtain-three-layer-v3.1-module-facts",
        "prompt_version": prompt_version, "schema_version": schema_version,
        "model": model_name(),
    }
    endpoint_identity = provider_endpoint_identity()
    if endpoint_identity is not None:
        base_payload["provider_endpoint"] = endpoint_identity
    if image_input_version is not None:
        base_payload["image_input_version"] = image_input_version
    result = {}
    for effort in reasoning_efforts:
        payload = {**base_payload, "reasoning_effort": effort}
        raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        result[effort] = hashlib.sha256(raw.encode("utf-8")).hexdigest()
    return result


def facts_for_display(facts: Mapping[str, Any]) -> dict[str, Any]:
    """把新旧视觉事实统一为Excel需要的基础关系、证据和置信度。"""
    value = validate_facts(facts)
    if "same_person" in value:
        return {"base_relation": value["same_person"], "evidence": value["visual_evidence"], "confidence": value["confidence"]}
    design = value["modules"]["design_identity"]["relation"]
    base = "yes" if design in {"same", "variant"} else "no" if design == "different" else "uncertain"
    evidence = []
    for name in MODULES:
        row = value["modules"][name]
        if row["comparison_reason"]:
            evidence.append(f"{name}：{row['comparison_reason']}")
        if len(evidence) >= 6:
            break
    assertive_bilateral = sum(
        row["relation"] != "uncertain"
        and bool(row["amazon_evidence"])
        and bool(row["temu_evidence"])
        for row in value["modules"].values()
    )
    evidenced_identity_conflict = any(
        row["relation"] == "different"
        and row["hard_conflict_code"] not in {"NONE", "DIFFERENT_MAIN_BUNDLE"}
        and bool(row["amazon_evidence"])
        and bool(row["temu_evidence"])
        for row in value["modules"].values()
    )
    confidence: float | str = value["overall_confidence"]
    if confidence == 0.0 and (assertive_bilateral >= 5 or evidenced_identity_conflict):
        confidence = "字段异常：模型返回0，不参与判定"
    return {"base_relation": base, "evidence": evidence, "confidence": confidence}


def estimate_cost_usd(usage: Mapping[str, int], model: str | None = None) -> float | None:
    """Return an OpenAI-list-price equivalent; a compatible gateway may bill differently."""
    prices = {
        "gpt-5.6-luna": (0.20, 0.02, 1.20),
        "gpt-5.6-terra": (2.00, 0.20, 12.00),
    }
    selected = model or model_name()
    if selected not in prices:
        return None
    input_rate, cached_rate, output_rate = prices[selected]
    input_tokens = int(usage.get("input_tokens", 0))
    cached = min(input_tokens, int(usage.get("cached_input_tokens", 0)))
    output = int(usage.get("output_tokens", 0))
    return ((input_tokens - cached) * input_rate + cached * cached_rate + output * output_rate) / 1_000_000
