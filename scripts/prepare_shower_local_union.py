"""Prepare one immutable local shower-curtain union without modifying source files."""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

ROOT = Path(__file__).resolve().parents[1]
TARGET_ASINS = {"B00LS9UD2M", "B0C5LS9J6P"}


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def rows(path: Path):
    with path.open(encoding="utf-8-sig") as source:
        for line in source:
            if line.strip():
                yield json.loads(line)


def clean_image(url: str) -> str:
    parsed = urlsplit(str(url or "").strip())
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--category", type=Path, required=True)
    parser.add_argument("--existing-temu", type=Path, required=True)
    parser.add_argument("--external-csv", type=Path, required=True)
    parser.add_argument("--external-json", type=Path, required=True)
    parser.add_argument("--amazon-jsonl", type=Path, required=True)
    parser.add_argument("--cache-seed-database", type=Path, required=True)
    args = parser.parse_args()
    category = json.loads(args.category.read_text(encoding="utf-8"))
    base = ROOT / "runs" / category["category_key"]
    amazon_out = base / "amazon" / "amazon_frontend_latest.jsonl"
    temu_out = base / "temu" / "union" / "products.jsonl"
    source_dir = base / "temu" / "sources"
    amazon_out.parent.mkdir(parents=True, exist_ok=True)
    temu_out.parent.mkdir(parents=True, exist_ok=True)
    source_dir.mkdir(parents=True, exist_ok=True)

    amazon = [row for row in rows(args.amazon_jsonl) if str(row.get("asin")) in TARGET_ASINS]
    if {str(row.get("asin")) for row in amazon} != TARGET_ASINS:
        raise SystemExit("Amazon输入未同时包含两个目标ASIN")

    merged: dict[str, dict] = {}
    for row in rows(args.existing_temu):
        goods = str(row.get("goods_id") or row.get("goodsId") or "").strip()
        if goods:
            merged[goods] = {**row, "goods_id": goods, "source_names": ["owner_temu_frontend_17571"]}
    owner_count = len(merged)
    csv_rows: list[dict] = []
    json_rows: list[dict] = []
    overlaps_by_source = {"external_csv": 0, "external_json": 0}
    conflicts = 0

    def merge_external(external: dict, source_name: str) -> None:
        nonlocal conflicts
        goods = external["goods_id"]
        prior = merged.get(goods)
        if prior is None:
            merged[goods] = external
            return
        overlaps_by_source[source_name] += 1
        observed_conflicts = list(prior.get("source_field_conflicts") or [])
        for field in ("title", "main_image_url", "product_url", "price_usd", "sales_text"):
            left = str(prior.get(field) or "").strip()
            right = str(external.get(field) or "").strip()
            if left and right and left != right:
                observed_conflicts.append({"field": field, "kept": left, "observed": right, "source": source_name})
            elif not left and right:
                prior[field] = external[field]
        if observed_conflicts:
            conflicts += 1
            prior["source_field_conflicts"] = observed_conflicts
        prior["source_names"] = sorted(set(prior.get("source_names", [])) | {source_name})
        prior.setdefault("source_observations", {})[source_name] = external

    with args.external_csv.open(encoding="utf-8-sig", newline="") as source:
        for row in csv.DictReader(source):
            goods = str(row.get("goodsId") or row.get("goods_id") or "").strip()
            if not goods:
                continue
            external = {
                "goods_id": goods,
                "title": row.get("title"),
                "price_usd": row.get("price"),
                "market_price_usd": row.get("marketPrice"),
                "sales_text": row.get("sales"),
                "thumbnail_url": clean_image(row.get("thumb") or ""),
                "main_image_url": clean_image(row.get("thumb") or ""),
                "product_url": row.get("link") or f"https://www.temu.com/goods.html?goods_id={goods}",
                "category": row.get("category"),
                "query_hit_count": row.get("query_hit_count"),
                "search_queries": [item.strip() for item in str(row.get("queries") or "").split("|") if item.strip()],
                "site": "TEMU_US",
                "currency": "USD",
                "source_names": ["external_csv"],
            }
            csv_rows.append(external)
            merge_external(external, "external_csv")

    payload = json.loads(args.external_json.read_text(encoding="utf-8-sig"))
    if isinstance(payload, dict):
        payload = next((payload[key] for key in ("products", "items", "records", "data") if isinstance(payload.get(key), list)), [])
    if not isinstance(payload, list):
        raise SystemExit("products.json不是可识别的商品数组")
    for row in payload:
        goods = str(row.get("goods_id") or row.get("goodsId") or "").strip()
        if not goods:
            continue
        external = {
            "goods_id": goods,
            "title": row.get("title"),
            "price_usd": row.get("price"),
            "sales_text": row.get("sales_tip") or row.get("sales_num"),
            "rating": row.get("rating"),
            "review_count": row.get("review_count"),
            "mall_id": row.get("mall_id"),
            "current_sku_id": row.get("current_sku_id"),
            "thumbnail_url": clean_image(row.get("image") or ""),
            "main_image_url": clean_image(row.get("image") or ""),
            "product_url": row.get("url") or f"https://www.temu.com/goods.html?goods_id={goods}",
            "site": "TEMU_US",
            "currency": "USD",
            "source_names": ["external_json"],
        }
        json_rows.append(external)
        merge_external(external, "external_json")

    observed = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with amazon_out.open("w", encoding="utf-8", newline="\n") as target:
        for row in sorted(amazon, key=lambda item: str(item["asin"])):
            target.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")
    with temu_out.open("w", encoding="utf-8", newline="\n") as target:
        for goods in sorted(merged):
            record = merged[goods]
            record.setdefault("observed_at", observed)
            target.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n")
    for destination, records in (
        (source_dir / "owner_frontend.jsonl", rows(args.existing_temu)),
        (source_dir / "external_csv.jsonl", csv_rows),
        (source_dir / "external_json.jsonl", json_rows),
    ):
        with destination.open("w", encoding="utf-8", newline="\n") as target:
            for record in records:
                target.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n")

    stage12 = {
        "schema_version": "incremental-stage12-local-shower-union-v1",
        "rule_version": category["stage3"]["rule_version"],
        "stage12_rule_version": category["stage3"]["rule_version"],
        "text_gate": "shower_curtain_v2",
        "output_dir": str(base / "stage12"),
        "cache_seed_database": str(args.cache_seed_database.resolve()),
        "poll_seconds": 2,
        "batch_size": 10000,
        "text_gate_workers": 16,
        "vector_batch_size": 64,
        "max_image_retries": 3,
        "candidate_threshold": 50,
        "below_threshold_action": category["stage2"]["below_threshold_action"],
        "candidate_materialize_interval_seconds": 30,
        "candidate_materialize_background": True,
        "excel": {"auto_update": False, "background": True, "output": str(base / "excel" / "浴帘本地并集20260922_第一二层.xlsx")},
        "image_download": {"workers": 64, "timeout_seconds": 30, "require_temu_proxy": False, "network_route": "local_direct_without_system_proxy"},
        "sources": [
            {"key": "amazon_shower_local_two", "platform": "AMAZON", "adapter": "amazon_generic", "snapshot_mode": "replace", "path": str(amazon_out)},
            {"key": "temu_shower_local_union", "platform": "TEMU", "adapter": "temu_frontend", "snapshot_mode": "replace", "path": str(temu_out)},
        ],
        "paths": [{"key": category["category_key"], "label": "2个Amazon浴帘 × 本地TEMU goods_id去重并集", "amazon_source": "amazon_shower_local_two", "temu_source": "temu_shower_local_union"}],
    }
    (base / "category.json").write_text(json.dumps(category, ensure_ascii=False, indent=2), encoding="utf-8")
    (base / "stage12_config.json").write_text(json.dumps(stage12, ensure_ascii=False, indent=2), encoding="utf-8")
    manifest = {
        "created_at": observed,
        "inputs": {
            "owner_temu": {"path": str(args.existing_temu.resolve()), "sha256": sha256(args.existing_temu), "unique_goods": owner_count},
            "external_csv": {"path": str(args.external_csv.resolve()), "sha256": sha256(args.external_csv), "rows": len(csv_rows), "unique_goods": len({row['goods_id'] for row in csv_rows})},
            "external_json": {"path": str(args.external_json.resolve()), "sha256": sha256(args.external_json), "rows": len(json_rows), "unique_goods": len({row['goods_id'] for row in json_rows})},
            "amazon": {"path": str(args.amazon_jsonl.resolve()), "sha256": sha256(args.amazon_jsonl), "selected_asins": sorted(TARGET_ASINS)},
        },
        "dedupe": {"overlap_by_source": overlaps_by_source, "union_unique_goods": len(merged), "rows_with_field_conflicts": conflicts},
        "outputs": {"amazon": str(amazon_out), "temu_union": str(temu_out), "stage12_config": str(base / "stage12_config.json")},
    }
    (base / "input_manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(manifest, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
