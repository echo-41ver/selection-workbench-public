"""DeepSeek-compatible image-only fact extraction for bath-towel-set pairs.

The provider only returns the category's four image-visible bilateral facts. It never calculates a
score or a final same/different decision; :mod:`bath_towel_layer3_v1` owns that
policy.  The module deliberately keeps the provider adapter category-specific
so a shower-curtain prompt or schema cannot silently leak into this run.
"""
from __future__ import annotations

import base64
import hashlib
import io
import json
import mimetypes
import os
import re
import threading
import time
from pathlib import Path
from typing import Any, Callable, Mapping
from urllib.parse import urlsplit

from selection_workflow.bath_towel_layer3_v1 import HARD_CODES, MODULES

DEFAULT_MODEL = "deepseek-flash"
MODEL_ENV = "OPENAI_VISION_MODEL"
API_KEY_ENV = "DEEPSEEK_API_KEY"
FALLBACK_API_KEY_ENV = "OPENAI_API_KEY"
API_BASE_ENV = "OPENAI_BASE_URL"
PROMPT_VERSION = "bath-towel-set-image-only-four-module-v3.2-design-position"
PREVIOUS_PROMPT_VERSION = "bath-towel-set-image-only-four-module-v3.0-compact"
FACT_SCHEMA_VERSION = "bath-towel-set-image-only-four-module-v3.0-compact"
IMAGE_INPUT_VERSION = "api-image-max1600-jpeg90-v1"
FACT_RELATIONS = {"SAME", "VARIANT", "SIMILAR", "DIFFERENT", "UNKNOWN"}

MODULE_CODES = {
    "d": "surface_design_weave_border",
    "k": "towel_construction_pile_edge",
    "s": "set_composition",
    "v": "color_pattern_variant",
}
RELATION_CODES = {"S": "SAME", "V": "VARIANT", "M": "SIMILAR", "D": "DIFFERENT", "U": "UNKNOWN"}
HARD_CODE_CODES = {
    "N": "NONE",
    "P": "DIFFERENT_PRODUCT_SUBTYPE",
    "W": "DIFFERENT_WEAVE_OR_SURFACE_DESIGN",
    "E": "DIFFERENT_EDGE_OR_BORDER_LAYOUT",
}

_COMPACT_MODULE_FACT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["r", "a", "t", "h"],
    "properties": {
        "r": {"type": "string", "enum": list(RELATION_CODES)},
        "a": {"type": "string", "maxLength": 160},
        "t": {"type": "string", "maxLength": 160},
        "h": {"type": "string", "enum": list(HARD_CODE_CODES)},
    },
}

FACT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["m", "c"],
    "properties": {
        "m": {
            "type": "object",
            "additionalProperties": False,
            "required": list(MODULE_CODES),
            "properties": {code: _COMPACT_MODULE_FACT_SCHEMA for code in MODULE_CODES},
        },
        "c": {"type": "number", "minimum": 0, "maximum": 1},
    },
}

_CLIENT_LOCAL = threading.local()


FIXED_RELATION_PROMPT = """
你是 Amazon 与 TEMU 浴巾套装图片对比的事实抽取器。第一张图是 Amazon 主图，第二张图是 TEMU 主图。只能使用这两张图片中的可见事实。你只输出 JSON 中四个视觉模块的双边事实关系，禁止计算分数，禁止输出最终结论，所有分数和最终状态由本地 Python 规则决定。

四个模块及权重（权重仅供你理解优先级，不要输出或计算）：
1. surface_design_weave_border（40）：最高优先级比较毛巾本体的印花、提花、织出条纹、装饰带、边框。逐项看图案形状、条带数量和间距，以及它们相对毛巾短边/长边的位置和走向；同样式同位置即使颜色不同也倾向 SAME。纯色底布上靠近一端的多条织纹仍是主体设计，不得误写成“无图案、无边框”。
2. towel_construction_pile_edge（27）：比较图片确实可见的毛圈、华夫格、剪绒等表面织法/绒面外观和缝边、包边、挂环；这是材质外观，不是棉、涤纶等纤维成分鉴定。
3. set_composition（21）：主图中实际可见的毛巾件数、类型组合和套装构成。折叠遮挡时不要猜测。
4. color_pattern_variant（12）：颜色是次要因素。颜色不同而主体图案样式及相对位置相同，记为普通颜色变体；主图案或边框布局归模块1。

确切纤维成分、精确尺寸、GSM、吸水性、快干、低扭/零扭、认证和其他图片不可验证属性全部忽略，不得输出为模块，也不得根据商品标题或规格猜测。可见毛圈/织法/绒面外观仍归模块2。

JSON 使用紧凑代码，不要输出比较理由、忽略元素、模块名、分数或总结。m 中四个键固定对应：d=surface_design_weave_border，k=towel_construction_pile_edge，s=set_composition，v=color_pattern_variant。每个模块只返回 r/a/t/h：r 只能为 S/V/M/D/U；a/t 分别是两张图中的一条可定位短证据；h 只能为 N/P/W/E，分别表示无硬冲突/产品子类不同/织法或主表面设计不同/标志性边框布局不同。无法从图片确认时使用 r=U、a=""、t=""、h=N。

硬冲突代码只能在 relation=DIFFERENT、且两张图都有具体可见证据时使用：产品子类不同、织法或主表面设计不同、标志性边框/装饰带布局不同。普通颜色、可见件数、挂环、包装或品牌差异不得单独生成硬冲突。

先定位浴巾本体，以可见的毛巾边缘、角和折线建立相对位置，再比较图案；允许旋转、镜像、折叠和不同拍摄视角。只有两图对应区域都清晰可见，才可凭图案位置不同判 DIFFERENT；一侧被遮挡、裁掉或折在背面时使用 U，不得把“没看到”说成“没有”。忽略浴室背景、人物、家具、摆放顺序、光线、滤镜、颜色偏差、水印、品牌商标遮罩、促销文字、尺寸线、箭头和包装标签。印在或织在浴巾本体上的图案、文字和条带属于表面设计。确切纤维成分只能由第一层商品文字/标签证据判断，不得从图片外观猜测或引用标题、规格、详情。

四个模块均能判断且主图清晰时，c 填 0.80 到 0.99；三个模块能判断或有局部遮挡时填 0.55 到 0.79；证据更少时填 0.01 到 0.54；两图主体均不可辨认且四个模块全 U 才填 0。c 是证据可见性，不是同款概率。严格只输出与给定 JSON Schema 相符的 JSON，不要输出 Markdown、解释、分数或最终判断。
""".strip()


def model_name() -> str:
    value = os.getenv(MODEL_ENV, "").strip()
    return value or os.getenv("DEEPSEEK_MODEL", DEFAULT_MODEL).strip() or DEFAULT_MODEL


def provider_endpoint() -> str:
    value = os.getenv(API_BASE_ENV, "").strip()
    if value:
        return value.rstrip("/")
    if model_name().lower().startswith("deepseek"):
        return "https://api.deepseek.com"
    return ""


def provider_endpoint_identity() -> str | None:
    if not model_name().lower().startswith("deepseek"):
        return None
    raw = provider_endpoint()
    parsed = urlsplit(raw if "://" in raw else f"https://{raw}")
    return f"{(parsed.hostname or '').lower()}{parsed.path.rstrip('/')}"


def _api_key() -> str:
    return os.getenv(API_KEY_ENV, "").strip() or os.getenv(FALLBACK_API_KEY_ENV, "").strip()


def _api_image_bytes(path: str | Path) -> tuple[bytes, str]:
    from .api_image_input import ImageInputError, validate_api_image
    file_path = Path(path)
    original = file_path.read_bytes()
    mime = mimetypes.guess_type(file_path.name)[0] or "image/jpeg"
    try:
        from PIL import Image, ImageOps

        with Image.open(io.BytesIO(original)) as image:
            validate_api_image(image)
            # Cached .img files need their decoded format, not their suffix.
            mime = Image.MIME.get(image.format, mime)
            image = ImageOps.exif_transpose(image)
            if max(image.size) <= 1600:
                return original, mime
            image.thumbnail((1600, 1600), Image.Resampling.LANCZOS)
            if image.mode not in {"RGB", "L"}:
                if "A" in image.getbands() or (image.mode == "P" and "transparency" in image.info):
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
    return f"data:{mime};base64,{base64.b64encode(content).decode('ascii')}"


def _sanitize_error(error: BaseException | str) -> str:
    text = str(error)
    for secret in (_api_key(),):
        if secret:
            text = text.replace(secret, "[已隐藏]")
    text = re.sub(r"sk-[^\s'\",}\]]+", "[已隐藏]", text)
    return text.replace("\r", " ").replace("\n", " ")[:800]


def _usage_dict(response: Any) -> dict[str, int]:
    usage = getattr(response, "usage", None)
    if usage is None:
        return {"input_tokens": 0, "cached_input_tokens": 0, "output_tokens": 0, "reasoning_tokens": 0, "total_tokens": 0}
    input_tokens = int(getattr(usage, "input_tokens", 0) or 0)
    output_tokens = int(getattr(usage, "output_tokens", 0) or 0)
    total_tokens = int(getattr(usage, "total_tokens", input_tokens + output_tokens) or 0)
    details = getattr(usage, "input_tokens_details", None)
    cached = int(getattr(details, "cached_tokens", 0) or 0) if details is not None else 0
    output_details = getattr(usage, "output_tokens_details", None)
    reasoning = int(getattr(output_details, "reasoning_tokens", 0) or 0) if output_details is not None else 0
    return {
        "input_tokens": input_tokens,
        "cached_input_tokens": cached,
        "output_tokens": output_tokens,
        "reasoning_tokens": reasoning,
        "total_tokens": total_tokens,
    }


def _short_context(value: Mapping[str, Any] | None, title: str) -> dict[str, Any]:
    """Keep text evidence useful without sending the huge raw stage-12 row."""
    value = value or {}
    result: dict[str, Any] = {"title": str(title or "")[:1200]}
    for key in ("material", "size", "specifications", "sku", "card_text", "gate_text"):
        raw = value.get(key)
        if raw is None:
            continue
        text = json.dumps(raw, ensure_ascii=False) if not isinstance(raw, str) else raw
        result[key] = text[:2400]
    return result


def _clean_text(value: Any, limit: int) -> str:
    return str(value or "").strip()[:limit]


def validate_facts(value: Mapping[str, Any]) -> dict[str, Any]:
    if set(value) == {"m", "c"}:
        compact_modules = value.get("m")
        if not isinstance(compact_modules, Mapping) or set(compact_modules) != set(MODULE_CODES):
            raise ValueError("浴巾套装紧凑视觉事实必须包含且只包含四个视觉模块代码")
        expanded: dict[str, Any] = {}
        for module_code, module_name in MODULE_CODES.items():
            row = compact_modules[module_code]
            if not isinstance(row, Mapping) or set(row) != {"r", "a", "t", "h"}:
                raise ValueError(f"紧凑模块{module_code}字段不完整")
            relation = RELATION_CODES.get(str(row.get("r") or "").strip().upper())
            hard_code = HARD_CODE_CODES.get(str(row.get("h") or "").strip().upper())
            if relation is None or hard_code is None:
                raise ValueError(f"紧凑模块{module_code}枚举无效")
            expanded[module_name] = {
                "relation": relation,
                "amazon_evidence": row.get("a") or "",
                "temu_evidence": row.get("t") or "",
                "comparison_reason": "",
                "hard_conflict_code": hard_code,
            }
        value = {"modules": expanded, "ignored_elements": [], "overall_confidence": value.get("c")}
    if set(value) != {"modules", "ignored_elements", "overall_confidence"}:
        raise ValueError("浴巾套装四模块视觉事实顶层字段不完整或包含额外字段")
    raw_modules = value.get("modules")
    if not isinstance(raw_modules, Mapping) or set(raw_modules) != set(MODULES):
        raise ValueError("浴巾套装视觉事实必须包含且只包含四个视觉模块")
    clean_modules: dict[str, dict[str, Any]] = {}
    relation_aliases = {item.lower(): item for item in FACT_RELATIONS}
    allowed_codes = {"NONE", *HARD_CODES}
    for name in MODULES:
        row = raw_modules[name]
        if not isinstance(row, Mapping) or set(row) != {
            "relation", "amazon_evidence", "temu_evidence", "comparison_reason", "hard_conflict_code",
        }:
            raise ValueError(f"模块{name}字段不完整")
        relation = relation_aliases.get(str(row.get("relation") or "").strip().lower())
        if relation is None:
            raise ValueError(f"模块{name}关系无效")
        code = _clean_text(row.get("hard_conflict_code"), 80).upper() or "NONE"
        if code not in allowed_codes:
            raise ValueError(f"模块{name}硬冲突代码无效")
        clean_modules[name] = {
            "relation": relation,
            "amazon_evidence": _clean_text(row.get("amazon_evidence"), 260),
            "temu_evidence": _clean_text(row.get("temu_evidence"), 260),
            "comparison_reason": _clean_text(row.get("comparison_reason"), 360),
            "hard_conflict_code": code,
        }
    ignored = value.get("ignored_elements")
    if not isinstance(ignored, list) or len(ignored) > 6 or not all(isinstance(item, str) for item in ignored):
        raise ValueError("ignored_elements无效")
    confidence = value.get("overall_confidence")
    if isinstance(confidence, bool) or not isinstance(confidence, (int, float)) or not 0 <= confidence <= 1:
        raise ValueError("overall_confidence必须在0到1之间")
    confidence = float(confidence)
    bilateral = sum(
        row["relation"] != "UNKNOWN" and bool(row["amazon_evidence"]) and bool(row["temu_evidence"])
        for row in clean_modules.values()
    )
    quality = "HIGH" if confidence >= 0.80 and bilateral >= 4 else "MEDIUM" if confidence >= 0.55 and bilateral >= 3 else "LOW"
    variants = [name for name, row in clean_modules.items() if row["relation"] == "VARIANT"]
    uncertainties = [name for name, row in clean_modules.items() if row["relation"] == "UNKNOWN"]
    design_relation = clean_modules["surface_design_weave_border"]["relation"]
    same_base: bool | None = True if design_relation in {"SAME", "VARIANT"} else False if design_relation == "DIFFERENT" else None
    return {
        "modules": clean_modules,
        "ignored_elements": [_clean_text(item, 100) for item in ignored if _clean_text(item, 100)],
        "overall_confidence": confidence,
        "evidence_quality": quality,
        "variant_differences": variants,
        "uncertainties": uncertainties,
        "same_base_product": same_base,
    }


def uncertain_facts() -> dict[str, Any]:
    return validate_facts({
        "modules": {
            name: {
                "relation": "UNKNOWN", "amazon_evidence": "", "temu_evidence": "",
                "comparison_reason": "视觉请求失败或证据不足", "hard_conflict_code": "NONE",
            }
            for name in MODULES
        },
        "ignored_elements": [],
        "overall_confidence": 0.0,
    })


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
    max_attempts: int = 1,
    reasoning_effort: str | None = None,
    request_observer: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    """Call the OpenAI-compatible Responses API once per attempt.

    The production runner passes ``max_attempts=1``.  It performs the only
    deferred retry after the primary queue drains, so transient errors never
    create an uncontrolled retry storm inside a worker.
    """
    if not _api_key():
        raise RuntimeError("缺少 DEEPSEEK_API_KEY/OPENAI_API_KEY")
    from openai import OpenAI

    requested_model = model_name()
    base_url = provider_endpoint() or None
    if client is None:
        cached_client = getattr(_CLIENT_LOCAL, "client", None)
        cached_identity = getattr(_CLIENT_LOCAL, "identity", None)
        identity = (requested_model, base_url, float(timeout_seconds))
        if cached_client is None or cached_identity != identity:
            cached_client = OpenAI(api_key=_api_key(), base_url=base_url, max_retries=0, timeout=timeout_seconds)
            _CLIENT_LOCAL.client = cached_client
            _CLIENT_LOCAL.identity = identity
        client = cached_client
    input_content = [
        {"type": "input_text", "text": "Amazon基准主图："},
        {"type": "input_image", "image_url": _data_url(amazon_image), "detail": "high"},
        {"type": "input_text", "text": "TEMU候选主图："},
        {"type": "input_image", "image_url": _data_url(temu_image), "detail": "high"},
    ]
    attempts: list[dict[str, Any]] = []
    accumulated = {"input_tokens": 0, "cached_input_tokens": 0, "output_tokens": 0, "reasoning_tokens": 0, "total_tokens": 0}
    response_id = ""
    for attempt in range(1, max(1, int(max_attempts)) + 1):
        started = time.monotonic()
        try:
            effort = (reasoning_effort or os.getenv("DEEPSEEK_REASONING_EFFORT", "low")).strip().lower() or "low"
            if effort not in {"low", "high", "max"}:
                raise ValueError(f"不支持的DeepSeek推理强度: {effort}")
            if request_observer:
                request_observer({"status": "request_started", "provider_requests": 1})
            response = client.responses.create(
                model=requested_model,
                instructions=FIXED_RELATION_PROMPT,
                input=[{"role": "user", "content": input_content}],
                text={
                    "format": {
                        "type": "json_schema",
                        "name": "bath_towel_set_visual_facts",
                        "strict": True,
                        "schema": FACT_SCHEMA,
                    }
                },
                reasoning={"effort": effort},
                max_output_tokens=max(1000, min(12000, int(os.getenv("OPENAI_VISION_MAX_OUTPUT_TOKENS", "8192")))),
            )
            usage = _usage_dict(response)
            response_id = str(getattr(response, "id", "") or "")
            if request_observer:
                request_observer({"status": "response_received", "provider_requests": 0, "usage": usage, "response_id": response_id})
            for key in accumulated:
                accumulated[key] += usage[key]
            raw = json.loads(str(getattr(response, "output_text", "") or ""))
            facts = validate_facts(raw)
            attempts.append({"attempt": attempt, "elapsed_seconds": round(time.monotonic() - started, 3), "status": "success", "usage": usage})
            return {
                "status": "success", "facts": facts,
                "model": str(getattr(response, "model", requested_model) or requested_model),
                "requested_model": requested_model, "response_id": str(getattr(response, "id", "") or ""),
                "usage": accumulated, "attempts": attempts, "failure_reason": None,
            }
        except Exception as exc:
            attempts.append({"attempt": attempt, "elapsed_seconds": round(time.monotonic() - started, 3), "status": "failed", "failure_reason": _sanitize_error(exc)})
            if attempt < max(1, int(max_attempts)):
                time.sleep(1.5)
    return {
        "status": "failed", "facts": uncertain_facts(), "model": requested_model,
        "requested_model": requested_model, "response_id": response_id, "usage": accumulated,
        "attempts": attempts, "failure_reason": attempts[-1]["failure_reason"] if attempts else "未知失败",
    }


def image_digest(path: str | Path) -> str:
    # Windows creation time is not a content version. A same-size overwrite
    # can also retain mtime, so a paid cache key must hash the current bytes.
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def vision_revision_key(*, amazon_image: str | Path, temu_image: str | Path, pair: Mapping[str, Any],
                        model: str | None = None, endpoint: str | None = None,
                        prompt_version: str | None = None) -> str:

    if endpoint is not None:
        parsed = urlsplit(endpoint if "://" in endpoint else f"https://{endpoint}")
        endpoint = f"{(parsed.hostname or '').lower()}{parsed.path.rstrip('/')}"

    payload = {
        "asin": str(pair.get("asin") or ""), "goods_id": str(pair.get("goods_id") or ""),
        "pair_revision": pair.get("pair_revision"),
        "amazon_image_sha256": image_digest(amazon_image), "temu_image_sha256": image_digest(temu_image),
        "prompt_version": prompt_version or PROMPT_VERSION, "schema_version": FACT_SCHEMA_VERSION,
        "image_input_version": IMAGE_INPUT_VERSION, "model": model if model is not None else model_name(),
        "endpoint": endpoint if endpoint is not None else provider_endpoint_identity(),
    }
    return hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
