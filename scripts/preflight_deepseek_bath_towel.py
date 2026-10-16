"""No-network preflight for the bath-towel-set DeepSeek layer-3 run."""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from selection_workflow.bath_towel_deepseek import FACT_SCHEMA, FIXED_RELATION_PROMPT, model_name, provider_endpoint  # noqa: E402
from selection_workflow.bath_towel_layer3_v1 import MODULES, MINIMUMS, score_facts  # noqa: E402


def read_first(path: Path, limit: int = 3) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as source:
        for line in source:
            if line.strip():
                result.append(json.loads(line))
                if len(result) >= limit:
                    break
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description="浴巾套装DeepSeek第三层本地预检，不发API请求")
    parser.add_argument("--candidate-file", type=Path, default=ROOT / "runs/bath_towel_set/stage12_v2/layer3_candidates_bath_towel_set.jsonl")
    parser.add_argument("--run-dir", type=Path, default=ROOT / "runs/bath_towel_set/stage123/deepseek_bath_towel_200")
    parser.add_argument("--category-config", type=Path, default=ROOT / "config/categories/bath_towel_set.json")
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()

    checks: list[dict[str, Any]] = []
    def check(name: str, ok: bool, detail: str) -> None:
        checks.append({"name": name, "ok": bool(ok), "detail": detail})

    key_present = bool(os.getenv("DEEPSEEK_API_KEY"))
    check("DEEPSEEK_API_KEY", key_present, "已设置（不显示密钥）" if key_present else "缺少DEEPSEEK_API_KEY")
    selected_model = model_name()
    check("model", selected_model.lower().startswith("deepseek"), selected_model)
    endpoint = provider_endpoint()
    check("endpoint", endpoint.lower().startswith("https://api.deepseek.com"), endpoint or "未设置")
    check("workers", 1 <= args.workers <= 4, f"当前={args.workers}，新Key实测并发上限=4")
    check("run_dir_is_provider_specific", "deepseek" in str(args.run_dir).lower(), str(args.run_dir))
    check("candidate_file", args.candidate_file.is_file(), str(args.candidate_file))
    first = []
    if args.candidate_file.is_file():
        try:
            first = read_first(args.candidate_file)
            check("candidate_jsonl", bool(first), f"已读取首{len(first)}行")
            missing = []
            for row in first:
                for key in ("asin", "goods_id", "amazon_image_path", "temu_image_path", "pair_revision"):
                    if not row.get(key):
                        missing.append(key)
            check("candidate_required_fields", not missing, sorted(set(missing)).__repr__() if missing else "首样本字段完整")
            paths_ok = all(Path(str(row.get(key))).is_file() for row in first for key in ("amazon_image_path", "temu_image_path"))
            check("candidate_images", paths_ok, "首样本图片路径可读" if paths_ok else "首样本存在缺失图片")
        except Exception as exc:
            check("candidate_jsonl", False, str(exc)[:500])

    check("prompt_has_no_final_decision", "禁止输出最终结论" in FIXED_RELATION_PROMPT and "禁止计算分数" in FIXED_RELATION_PROMPT, "提示词明确由Python计分")
    check("schema_category_modules", len(FACT_SCHEMA["properties"]["m"]["required"]) == len(MODULES) == 4, "紧凑Schema包含浴巾四个纯视觉模块")
    check("module_weight_sum", sum(MODULES.values()) == 100, str(sum(MODULES.values())))
    check("thresholds", MINIMUMS == {"surface_design_weave_border": 34, "towel_construction_pile_edge": 21, "set_composition": 15}, "确认同款88/不同款79/三个关键模块最低分34、21、15")
    fixture = {"modules": {name: {"relation": "SAME", "amazon_evidence": "相同证据", "temu_evidence": "相同证据", "comparison_reason": "一致", "hard_conflict_code": "NONE"} for name in MODULES}, "overall_confidence": 0.9}
    try:
        score = score_facts(fixture)
        check("local_scorer", score.get("state") in {"确认同款", "同款（存在规格变体）"}, str(score.get("state")))
    except Exception as exc:
        check("local_scorer", False, str(exc)[:500])
    check("excel_exporter", (ROOT / "scripts/export_incremental_layer123_excel.py").is_file(), "品类模块列导出器存在")
    check("excel_validator", (ROOT / "scripts/validate_incremental_layer123_excel.py").is_file(), "固定格式校验器存在")
    if args.category_config.is_file():
        try:
            config = json.loads(args.category_config.read_text(encoding="utf-8"))
            stage3 = config.get("stage3") or {}
            check("category_provider", stage3.get("provider") == "deepseek", str(stage3.get("provider")))
            check("category_model", stage3.get("model") == selected_model, str(stage3.get("model")))
            check("category_concurrency", int(stage3.get("max_concurrency", 0)) == 4, str(stage3.get("max_concurrency")))
            check("category_batch_mode", stage3.get("request_mode") == "one_amazon_to_many_temu" and int(stage3.get("batch_size", 0)) == 12, f"{stage3.get('request_mode')} / {stage3.get('batch_size')}")
            check("category_reasoning", stage3.get("reasoning_effort") == "low" and float((stage3.get("high_rerun_policy") or {}).get("confidence_below", -1)) == 0.65, f"{stage3.get('reasoning_effort')} / {(stage3.get('high_rerun_policy') or {}).get('confidence_below')}")
            check("category_prompt_schema", stage3.get("prompt_version") == "bath-towel-set-image-only-four-module-v3.2-design-position" and stage3.get("schema_version") == "bath-towel-set-image-only-four-module-v3.0-compact", f"{stage3.get('prompt_version')} / {stage3.get('schema_version')}")
            check("stage2_candidate_threshold", float((config.get("stage2") or {}).get("candidate_threshold", -1)) == 60.0, str((config.get("stage2") or {}).get("candidate_threshold")))
        except Exception as exc:
            check("category_config", False, str(exc)[:500])
    else:
        check("category_config", False, str(args.category_config))

    report = {"schema_version": "deepseek-bath-towel-preflight-v1", "status": "READY" if all(item["ok"] for item in checks) else "BLOCKED", "api_requests": 0, "checks": checks, "model": selected_model, "endpoint": endpoint, "candidate_sample_count": len(first)}
    destination = args.report or (args.run_dir / "preflight.json")
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    raise SystemExit(0 if report["status"] == "READY" else 2)


if __name__ == "__main__":
    main()
