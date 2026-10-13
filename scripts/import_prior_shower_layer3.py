"""Reuse paid shower-curtain visual facts only when both image bytes are identical."""
from __future__ import annotations

import argparse
from collections import Counter
from functools import lru_cache
import hashlib
import json
from pathlib import Path
import re
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from selection_automation.category_config import load_category
from selection_automation.rules import score
from selection_automation.vision import vision_revision


RELATIONS = {
    "same": "SAME", "variant": "VARIANT", "different": "DIFFERENT",
    "unknown": "UNKNOWN", "uncertain": "UNKNOWN",
}


def rows(path: Path):
    if not path.exists():
        return
    with path.open(encoding="utf-8") as stream:
        for line in stream:
            if line.strip():
                try:
                    yield json.loads(line)
                except json.JSONDecodeError:
                    continue


@lru_cache(maxsize=200_000)
def digest(path_text: str) -> str | None:
    path = Path(path_text)
    if not path.is_file():
        return None
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def evidence(value) -> str:
    if isinstance(value, list):
        return "；".join(str(item).strip() for item in value if str(item).strip())
    return str(value or "").strip()


def canonical_amazon_image_url(value: str) -> str:
    """Treat Amazon CDN resize suffixes as the same source image identity."""
    return re.sub(r"\._[^/]+(?=\.[A-Za-z]+$)", "", str(value or "").strip())


def normalize_facts(category: dict, prior: dict) -> dict | None:
    source = prior.get("visual_facts") or {}
    if not isinstance(source.get("identity"), dict) or 'design_comparison' not in source['identity']:
        return None
    modules = source.get("modules") or {}
    expected = list(category["stage3"]["modules"])
    if set(modules) != set(expected):
        return None
    allowed_codes = set(category["stage3"].get("identity_hard_conflicts", []))
    normalized = {}
    for name in expected:
        old = modules[name]
        relation = RELATIONS.get(str(old.get("relation", "unknown")).casefold())
        if relation is None:
            return None
        code = str(old.get("hard_conflict_code") or "NONE")
        if code not in allowed_codes:
            code = "NONE"
        normalized[name] = {
            "relation": relation,
            "amazon_evidence": evidence(old.get("amazon_evidence")),
            "temu_evidence": evidence(old.get("temu_evidence")),
            "hard_conflict_code": code,
        }
    confidence = float(source.get("overall_confidence", 0))
    if confidence > 1:
        confidence /= 100
    result = {
        "modules": normalized,
        "identity": source["identity"],
        "overall_confidence": max(0.0, min(1.0, confidence)),
        "variant_differences": [name for name, item in normalized.items() if item["relation"] == "VARIANT"],
    }
    score(category, result)
    return result


def rank(row: dict) -> tuple[int, str]:
    prompt = str(row.get("prompt_version") or "")
    priority = 0
    for marker, value in (("v2.3", 5), ("v2.2", 4), ("v2.1", 3), ("v2.0", 2), ("v1.0", 1)):
        if marker in prompt:
            priority = value
            break
    return priority, str(row.get("completed_at") or "")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--category", type=Path, required=True)
    parser.add_argument("--candidates", type=Path, required=True)
    parser.add_argument("--results", type=Path, required=True)
    parser.add_argument("--prior-results", type=Path, action="append", default=[])
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()
    category = load_category(args.category)
    candidates = {(str(row["asin"]), str(row["goods_id"])): row for row in rows(args.candidates)}
    existing = {row.get("vision_revision_key") for row in rows(args.results) if row.get("api_status") == "success"}
    prior_by_pair: dict[tuple[str, str], list[tuple[Path, dict]]] = {}
    counters = Counter()
    for path in args.prior_results:
        for row in rows(path):
            if row.get("api_status") != "success":
                continue
            key = (str(row.get("asin") or ""), str(row.get("goods_id") or ""))
            if key in candidates:
                prior_by_pair.setdefault(key, []).append((path, row))
                counters["历史成功且进入本次候选"] += 1
    imported = []
    for key, choices in prior_by_pair.items():
        current = candidates[key]
        current_key = vision_revision(category, current)
        if current_key in existing:
            counters["新任务中已存在"] += 1
            continue
        matched = None
        for source_path, prior in sorted(choices, key=lambda item: rank(item[1]), reverse=True):
            if prior.get("vision_revision_key") != current_key:
                counters["视觉协议或图片版本不一致"] += 1
                continue
            old_amazon = digest(str(prior.get("amazon_image_path") or ""))
            old_temu = digest(str(prior.get("temu_image_path") or ""))
            if not old_amazon or not old_temu:
                counters["历史图片缺失"] += 1
                continue
            amazon_bytes_equal = old_amazon == digest(str(current["amazon_image_path"]))
            current_amazon_url = canonical_amazon_image_url(current.get("amazon_image_url"))
            prior_amazon_url = canonical_amazon_image_url(prior.get("amazon_image_url"))
            amazon_source_equal = bool(current_amazon_url and current_amazon_url == prior_amazon_url)
            temu_bytes_equal = old_temu == digest(str(current["temu_image_path"]))
            if not (temu_bytes_equal and (amazon_bytes_equal or amazon_source_equal)):
                counters["图片身份不一致"] += 1
                continue
            facts = normalize_facts(category, prior)
            if facts is None:
                counters["非完整七模块"] += 1
                continue
            matched = (source_path, prior, facts)
            break
        if matched is None:
            continue
        source_path, prior, facts = matched
        imported.append({
            "asin": current["asin"], "goods_id": current["goods_id"],
            "pair_revision": current["pair_revision"], "vision_revision_key": current_key,
            "attempt": 0, "api_status": "success", "visual_facts": facts,
            "module_result": score(category, facts), "model": prior.get("model"),
            "response_id": prior.get("response_id"), "usage": {}, "elapsed_seconds": 0,
            "reused_from_prior": True, "prior_result_path": str(source_path),
            "prior_vision_revision_key": prior.get("vision_revision_key"),
            "reuse_identity": "asin+goods_id+temu_sha256+(amazon_sha256_or_same_amazon_cdn_source)+seven_modules",
        })
        existing.add(current_key)
        counters["成功迁移并跳过API"] += 1
    if imported:
        args.results.parent.mkdir(parents=True, exist_ok=True)
        with args.results.open("a", encoding="utf-8") as stream:
            for row in imported:
                stream.write(json.dumps(row, ensure_ascii=False) + "\n")
    report = {
        "规则": "仅复用同ASIN、同goodsId、TEMU图片SHA256一致、Amazon图片SHA256一致或仅CDN缩放后缀不同，且包含完整七模块的历史成功结果",
        "本次候选数": len(candidates), "迁移计数": dict(counters),
        "迁移组合": [{"asin": row["asin"], "goods_id": row["goods_id"], "来源": row["prior_result_path"]} for row in imported],
    }
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"本次候选数": len(candidates), "历史第三层复用数": len(imported)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
