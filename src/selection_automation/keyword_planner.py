from __future__ import annotations

import argparse
import json
import re
from collections import defaultdict
from pathlib import Path
from typing import Any, Iterable


STOP = {
    "for", "and", "with", "from", "soft", "absorbent", "premium", "luxury", "quick", "dry",
    "bathroom", "hotel", "spa", "home", "highly", "extra", "large", "perfect", "pack", "piece",
}
MATERIAL = ("turkish cotton", "egyptian cotton", "pima cotton", "bamboo viscose", "microfiber", "cotton", "linen")
WEAVE = ("waffle", "terry", "jacquard", "velour", "ribbed", "zero twist", "ring spun")
PATTERN = ("striped", "stripe", "geometric", "floral", "solid", "checkered", "bordered")
CERTIFICATION = ("oeko tex standard 100", "oeko tex", "gots certified", "gots", "organic content standard")
FEATURE = ("quick dry", "fade resistant", "low lint", "no lint", "hotel quality", "coral fleece", "oversized")


def _title(row: dict[str, Any]) -> str:
    return " ".join(str(row.get(key) or "") for key in ("title", "amazon_title", "Amazon标题"))


def _first_phrase(text: str, phrases: Iterable[str]) -> str | None:
    lower = text.lower()
    return next((phrase for phrase in phrases if phrase in lower), None)


def _piece_count(text: str) -> str | None:
    match = re.search(r"\b(\d{1,2})[-\s]*(?:piece|pc|pcs|pack)\b", text, re.I)
    if not match:
        match = re.search(r"\bset\s+of\s+(\d{1,2})\b", text, re.I)
    return f"{match.group(1)} piece" if match else None


def _search_text(row: dict[str, Any]) -> str:
    specs = " ".join(
        f"{item.get('label', '')} {item.get('value', '')}"
        for item in (row.get("specifications") or []) if isinstance(item, dict)
    )
    text = re.sub(r"[^a-z0-9% -]+", " ", f"{_title(row)} {specs}".lower())
    return re.sub(r"\bringspun\b", "ring spun", text)


def extract_keyword_candidates(row: dict[str, Any], base_query: str) -> list[dict[str, Any]]:
    text = _search_text(row)
    material = _first_phrase(text, MATERIAL)
    if "turkish" in text and "cotton" in text:
        material = "turkish cotton"
    weave = _first_phrase(text, WEAVE)
    pattern = _first_phrase(text, PATTERN)
    certification = _first_phrase(text, CERTIFICATION)
    if certification and certification.startswith("oeko"):
        certification = "oeko tex"
    count = _piece_count(text)
    feature = _first_phrase(text, FEATURE)
    has_mixed_set = "bath towel" in text and "hand towel" in text and ("washcloth" in text or "wash cloth" in text)
    candidates: list[tuple[str, str, list[str], int]] = []
    add = candidates.append
    if material and certification:
        add((f"{material} {certification} bath towel set", "材质与认证组合", [material, certification], 100))
    if material and weave:
        add((f"{material} {weave} bath towel set", "材质与织法组合", [material, weave], 95))
    if count and material:
        add((f"{count} {material} bath towel set", "件数与材质组合", [count, material], 90))
    if certification:
        add((f"{certification} bath towel set", "认证特征", [certification], 85))
    if material:
        add((f"{material} {base_query}", "材质或产地材质特征", [material], 80))
    if weave:
        add((f"{weave} {base_query}", "织法或表面结构", [weave], 75))
    if pattern and pattern != "solid":
        normalized_pattern = "striped" if pattern == "stripe" else pattern
        add((f"{normalized_pattern} {base_query}", "图案或边框设计", [normalized_pattern], 70))
    if has_mixed_set:
        add(("bath hand washcloth towel set", "套装组成", ["bath towel", "hand towel", "washcloth"], 68))
    if feature:
        add((f"{feature} {base_query}", "明确功能或工艺特征", [feature], 67))
    if count:
        add((f"{count} {base_query}", "套装件数", [count], 65))
    seen: set[str] = set()
    result = []
    for query, reason, evidence, priority in candidates:
        normalized = re.sub(r"\s+", " ", query).strip()
        if normalized == base_query or normalized in seen:
            continue
        seen.add(normalized)
        result.append({"query": normalized, "reason": reason, "evidence": evidence, "priority": priority})
    return result


def extract_queries(row: dict[str, Any], base_query: str) -> list[str]:
    return [base_query, *(item["query"] for item in extract_keyword_candidates(row, base_query))]


def build_product_groups(rows: list[dict[str, Any]], base_query: str, keywords_per_product: int = 6) -> list[dict[str, Any]]:
    groups = []
    for row in sorted(rows, key=lambda item: item.get("rank") or 9999):
        asin = str(row.get("asin") or row.get("ASIN") or "").strip()
        if not asin:
            continue
        groups.append({
            "asin": asin,
            "rank": row.get("rank"),
            "title": _title(row).strip(),
            "queries": extract_keyword_candidates(row, base_query),
        })
    return groups


def select_useful_queries(plan: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Keep searches only while they add specific product or structural coverage."""
    primary = next((p for p in plan if p.get('purpose') == 'CATEGORY_SAMPLE'), None)
    selected = [primary] if primary else []
    pending = [p for p in plan if p is not primary and p.get('source_asins')]
    covered, styles = set(), set()
    support_features = {'non slip', 'nonslip', 'non skid', 'rubber base', 'silicone bottom', 'waterproof'}

    def style_evidence(item):
        result = set()
        for evidence in item.get('source_evidence', []):
            for feature in evidence.get('features', []):
                if feature.get('kind') in ('structure', 'model') and feature.get('value') not in support_features:
                    result.add((evidence['asin'], feature['kind'], feature['value']))
            # The legacy towel extractor uses literal weave/pattern evidence.
            for value in evidence.get('evidence', []):
                if value in WEAVE or value in PATTERN:
                    result.add((evidence['asin'], 'surface', value))
        return result

    while pending:
        def priority(item):
            sources = set(item['source_asins'])
            return (len(sources - covered), len(style_evidence(item) - styles),
                    len(sources), -len(item['query'].split()), -len(item['query']))
        best = max(pending, key=priority)
        if not any(priority(best)[:2]):
            break
        selected.append(best)
        covered.update(best['source_asins'])
        styles.update(style_evidence(best))
        pending.remove(best)
    return selected


def build_plan(
    rows: list[dict[str, Any]], base_query: str, keyword_limit: int | None, max_products: int, keywords_per_product: int = 4
) -> list[dict[str, Any]]:
    sources: dict[str, set[str]] = defaultdict(set)
    evidence: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        asin = str(row.get("asin") or row.get("ASIN") or "").strip()
        if not asin:
            continue
        sources[base_query].add(asin)
        for item in extract_keyword_candidates(row, base_query)[:max(1, keywords_per_product or 6)]:
            query = item["query"]
            sources[query].add(asin)
            evidence[query].append({
                "asin": asin,
                "rank": row.get("rank"),
                "reason": item["reason"],
                "evidence": item["evidence"],
                "priority": item["priority"],
            })
    ranked = sorted(
        sources,
        key=lambda q: (
            q != base_query,
            -max((item["priority"] for item in evidence[q]), default=0),
            -len(sources[q]),
            len(q),
            q,
        ),
    )
    plan = [
        {
            "query": query,
            "source_asins": sorted(sources[query]),
            "source_evidence": sorted(evidence[query], key=lambda item: (item.get("rank") or 9999, item["asin"])),
            "max_products": max_products,
            "status": "PLANNED",
        }
        for query in ranked
    ]
    for item in plan:
        if item["query"] == base_query:
            item["purpose"] = "CATEGORY_SAMPLE"
    return select_useful_queries(plan)


def read_json_rows(path: Path) -> list[dict[str, Any]]:
    if path.suffix.lower() == ".jsonl":
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    value = json.loads(path.read_text(encoding="utf-8"))
    return value if isinstance(value, list) else list(value.get("records") or value.get("items") or [])


def main() -> None:
    parser = argparse.ArgumentParser(description="Extract auditable TEMU search keywords from Amazon frontend facts")
    parser.add_argument("--amazon", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--base-query", default="bath towel set")
    parser.add_argument("--keyword-limit", type=int, default=None, help="Deprecated compatibility option; keywords are no longer capped")
    parser.add_argument("--max-products", type=int, choices=(500, 1000), default=500)
    args = parser.parse_args()
    rows = read_json_rows(args.amazon)
    plan = build_plan(rows, args.base_query, args.keyword_limit, args.max_products)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps({"schema_version": "temu-keyword-plan-v1", "keywords": plan}, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"状态": "已生成", "Amazon商品": len(rows), "TEMU关键词": len(plan), "输出": str(args.output)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
