from __future__ import annotations

import argparse
import hashlib
import json
import os
import tempfile
import time
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
from typing import Any


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows = []
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        value = json.loads(line)
        if not value.get("goods_id"):
            raise ValueError(f"{path}:{number} missing goods_id")
        rows.append(value)
    return rows


def observation_complete(row: dict[str, Any]) -> bool:
    try:
        price = float(row["price_usd"])
    except (KeyError, TypeError, ValueError):
        return False
    return bool(row.get("title") and row.get("product_url") and 0 <= price < float("inf")
                and (row.get("main_image_url") or row.get("thumbnail_url") or row.get("all_image_urls")))


def observation_time(row: dict[str, Any]) -> float:
    try:
        instant = datetime.fromisoformat(str(row.get("observed_at") or "").replace("Z", "+00:00"))
        return instant.replace(tzinfo=instant.tzinfo or timezone.utc).timestamp()
    except (ValueError, OverflowError, OSError):
        return float("-inf")


def merge(inputs: list[Path], provider_path: Path | None = None) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    observations: dict[str, list[tuple[dict, dict]]] = {}
    source_rows = 0
    for path in sorted(inputs):
        for row in read_jsonl(path):
            source_rows += 1
            goods_id = str(row["goods_id"])
            source = {"snapshot": str(path.resolve()), "search_query": str(row.get("search_query") or path.parent.name),
                      "observed_at": row.get("observed_at"), "core_fields_complete": observation_complete(row),
                      "search_sort": row.get("search_sort")}
            observations.setdefault(goods_id, []).append((row, source))
    rows = []
    tracked_fields = ("title", "product_url", "price_usd", "sales_text", "rating", "review_count_text",
                      "main_image_url", "thumbnail_url", "all_image_urls")
    for goods_id, records in observations.items():
        # All primary facts come from ONE observation. A newer incomplete row
        # is retained as evidence, but cannot replace an older complete record.
        primary, primary_source = max(records, key=lambda entry: (
            observation_complete(entry[0]), observation_time(entry[0]), entry[1]["snapshot"],
            json.dumps(entry[0], ensure_ascii=False, sort_keys=True),
        ))
        queries = sorted({query for row, source in records for query in [source["search_query"], *(row.get("source_queries") or [])]})
        conflicts = {}
        for field in tracked_fields:
            values = [(row.get(field), source) for row, source in records if row.get(field) not in (None, "", [])]
            if len({json.dumps(value, sort_keys=True, ensure_ascii=False) for value, _ in values}) > 1:
                conflicts[field] = [{"value": value, "source": source} for value, source in values]
        observed_images = sorted({url for row, _ in records for url in (
            [row.get("main_image_url"), row.get("thumbnail_url"), *(row.get("all_image_urls") or [])]) if url})
        rows.append({
            **primary, "goods_id": goods_id, "source_queries": queries,
            "source": "VISIBLE_TEMU_FRONTEND_KEYWORD_UNION",
            "primary_observation": primary_source,
            "observation_sources": [source for _, source in records],
            "field_conflicts": conflicts,
            "observed_image_urls": observed_images,
            "merge_policy": "LATEST_COMPLETE_OBSERVATION_WITH_CONFLICT_PROVENANCE",
        })
    frontend_count=len(rows)
    provider_count=0
    reference_ids=set()
    reference_rows=0
    reference_status='NOT_AVAILABLE'
    frontend_ids={r['goods_id'] for r in rows}
    if provider_path and provider_path.is_file():
        payload=json.loads(provider_path.read_text(encoding='utf-8'))
        reference_status=payload.get('status') or 'UNKNOWN'
        if payload.get('status') in ('COMPLETE','PARTIAL') and payload.get('site_id')==48:
            known={r['goods_id'] for r in rows}
            for raw in payload.get('records',[]):
                gid=str(raw.get('goodsId') or '')
                if not gid: continue
                reference_rows+=1
                reference_ids.add(gid)
                if gid in known: continue
                known.add(gid);provider_count+=1
                price=raw.get('price')
                try: price=float(price) if price is not None else None
                except (TypeError,ValueError): price=None
                rows.append({'goods_id':gid,'title':raw.get('goodsNameEn') or raw.get('goodsName') or raw.get('goodsNameCn'),
                    'product_url':raw.get('productUrl') or raw.get('linkUrl'),
                    'main_image_url':raw.get('thumbnail'),'price_usd':price,'currency':'USD',
                    'sales_text':str(raw['sold']) if raw.get('sold') is not None else None,
                    'rating':raw.get('goodsScore'),'review_count_text':str(raw['reviewNum']) if raw.get('reviewNum') is not None else None,
                    'listed_at':raw.get('onSaleTime'),'listed_at_source':'GEEKBI',
                    'observed_at':raw.get('updateTime') or payload.get('queried_at'),
                    'source':'GEEKBI_SUPPLEMENT','source_queries':[],
                    'merge_policy':'FRONTEND_FIRST_PROVIDER_ONLY_MISSING_ID',
                    'provider_evidence':raw})
    rows.sort(key=lambda row: (min(row.get("source_queries") or [""]), row["goods_id"]))
    summary = {
        "schema_version": "temu-keyword-union-v1",
        "input_snapshots": [str(path.resolve()) for path in inputs],
        "source_rows": source_rows,
        "unique_goods_id": len(rows),
        "frontend_unique_goods":frontend_count,
        "provider_supplement_goods":provider_count,
        "analysis_unique_goods":len(rows),
        "provider_reference_status":reference_status,
        "provider_reference_rows":reference_rows,
        "provider_reference_goods":len(reference_ids),
        "provider_reference_overlap_goods":len(reference_ids & frontend_ids),
        "duplicate_rows_merged": source_rows - frontend_count,
        "content_sha256": hashlib.sha256("".join(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n" for row in rows).encode()).hexdigest(),
    }
    return rows, summary


def _file_signature(path: Path) -> tuple:
    value = path.stat()
    return (str(path), value.st_size, value.st_mtime_ns, value.st_ctime_ns)


@lru_cache(maxsize=64)
def _source_counts(kind: str, signatures: tuple[tuple, ...]) -> dict[str, Any]:
    """Cache only stable source files, never a legacy aggregate's labels."""
    result: dict[str, Any] = {}
    paths = [Path(item[0]) for item in signatures]
    if kind == 'union':
        frontend, market, all_ids, unclassified = set(), set(), set(), set()
        for row in read_jsonl(paths[0]):
            identity = str(row['goods_id'])
            all_ids.add(identity)
            if row.get('source') == 'VISIBLE_TEMU_FRONTEND_KEYWORD_UNION':
                frontend.add(identity)
            elif row.get('source') == 'GEEKBI_SUPPLEMENT':
                market.add(identity)
            else:
                unclassified.add(identity)
        result.update(frontend_unique_goods=len(frontend), provider_supplement_goods=len(market - frontend),
                      analysis_unique_goods=len(all_ids), source_unclassified_goods=len(unclassified))
    elif kind == 'snapshots':
        result['source_rows'] = sum(len(read_jsonl(path)) for path in paths)
    elif kind == 'progress':
        counts = [json.loads(path.read_text('utf-8')).get('raw_collected_count') for path in paths]
        result['frontend_raw_collected_count'] = (sum(counts) if all(type(value) is int and value >= 0
                                                                   for value in counts) else None)
    elif kind == 'market':
        payload = json.loads(paths[0].read_text('utf-8'))
        result['provider_reference_status'] = payload.get('status') or 'UNKNOWN'
        if payload.get('status') in ('COMPLETE', 'PARTIAL') and payload.get('site_id') == 48:
            identities = [str(row.get('goodsId')) for row in payload.get('records', []) if row.get('goodsId')]
            result.update(provider_reference_rows=len(identities), provider_reference_goods=len(set(identities)))
    if signatures != tuple(_file_signature(path) for path in paths):
        raise ValueError('Source files changed during count read')
    return result


def current_source_summary(root: Path, category: dict[str, Any]) -> dict[str, Any]:
    """Read source groups for old and new tasks without rewriting any artifact.

    Counts describe observations, current merged input, and market reference
    separately. Missing/broken evidence stays unknown; it never inherits the
    historical ``unique_goods_id`` value as a frontend collection count.
    """
    key = category['category_key']
    if not isinstance(key, str) or not key or Path(key).name != key or key in ('.', '..'):
        raise ValueError('Invalid category key')
    base = Path(root) / 'runs' / key
    workers = base / 'temu' / 'workers'
    groups = {
        'union': [base / 'temu' / 'union' / 'products.jsonl'],
        'snapshots': sorted([*workers.glob('*/*/products.jsonl'), *workers.glob('*/*/unclassified_products.jsonl')]),
        'progress': sorted(workers.glob('*/*/progress.json')),
        'market': [base / 'market' / 'geekbi.json'],
    }
    result = {name: None for name in ('source_rows', 'frontend_raw_collected_count', 'frontend_unique_goods',
              'provider_supplement_goods', 'analysis_unique_goods', 'source_unclassified_goods',
              'provider_reference_rows', 'provider_reference_goods', 'provider_reference_status')}
    errors = []
    for kind, paths in groups.items():
        if not paths or any(not path.is_file() for path in paths):
            continue
        try:
            result.update(_source_counts(kind, tuple(_file_signature(path) for path in paths)))
        except (OSError, UnicodeError, ValueError, TypeError, AttributeError):
            errors.append(kind)
    result['source_count_errors'] = errors
    return result


def atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    os.close(handle)
    temp_path = Path(temporary)
    try:
        temp_path.write_text(content, encoding="utf-8")
        # Windows readers/parallel publishers can briefly hold the destination
        # without delete sharing. Keep the old complete JSON until replace succeeds.
        deadline = time.monotonic() + 2.0
        while True:
            try:
                os.replace(temp_path, path)
                break
            except PermissionError:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise
                time.sleep(min(0.05, remaining))
    finally:
        temp_path.unlink(missing_ok=True)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", type=Path, action="append", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--summary", type=Path, required=True)
    args = parser.parse_args()
    rows, summary = merge(args.input)
    if not rows:
        raise SystemExit("refusing to replace union snapshot with zero products")
    atomic_write(args.output, "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows))
    atomic_write(args.summary, json.dumps(summary, ensure_ascii=False, indent=2))
    print(json.dumps(summary, ensure_ascii=False))


if __name__ == "__main__":
    main()
