"""Import an Amazon Best Sellers frontend workbook as an atomic JSONL snapshot."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

import openpyxl
from openpyxl.cell.cell import Cell
from openpyxl.utils.datetime import from_excel


HYPERLINK_FORMULA = re.compile(
    r'^=HYPERLINK\(\s*"((?:[^"]|"")*)"\s*[,;]\s*"(?:[^"]|"")*"\s*\)$',
    re.IGNORECASE,
)


def cell_value(cell: Cell):
    return cell.value


def hyperlink_url(cell: Cell) -> str | None:
    """Return the real URL from native links, HYPERLINK formulas, or legacy text."""
    if cell.hyperlink and cell.hyperlink.target:
        return str(cell.hyperlink.target).strip()
    value = cell.value
    if isinstance(value, str):
        match = HYPERLINK_FORMULA.match(value.strip())
        if match:
            return match.group(1).replace('""', '"').strip()
        if value.strip().lower().startswith(("https://", "http://")):
            return value.strip()
    return None


def is_https_url(value: str | None) -> bool:
    parsed = urlparse(value or "")
    return parsed.scheme == "https" and bool(parsed.netloc)


def sha256(path: Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            value.update(chunk)
    return value.hexdigest()


def iso_value(value, epoch) -> str | None:
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, (int, float)):
        parsed = from_excel(value, epoch)
    else:
        return str(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.isoformat().replace("+00:00", "Z")


def main() -> None:
    parser = argparse.ArgumentParser(description="导入 Amazon Best Sellers 前端榜单工作簿")
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    source = args.input.resolve()
    if not source.exists():
        raise FileNotFoundError(source)
    args.output_dir.mkdir(parents=True, exist_ok=True)
    source_hash = sha256(source)
    evidence = args.output_dir / f"amazon_frontend_bestsellers_{source_hash[:12]}.xlsx"
    if not evidence.exists():
        shutil.copy2(source, evidence)

    workbook = openpyxl.load_workbook(source, read_only=False, data_only=False)
    sheet = workbook["商品列表"] if "商品列表" in workbook.sheetnames else workbook[workbook.sheetnames[0]]
    rows = list(sheet.iter_rows())
    header_index = next((i for i, row in enumerate(rows) if "ASIN" in [cell_value(cell) for cell in row]), None)
    if header_index is None:
        raise RuntimeError("未找到包含 ASIN 的表头")
    headers = [cell_value(cell) for cell in rows[header_index]]
    records: dict[str, dict] = {}
    duplicates: list[str] = []
    for source_row, cells in enumerate(rows[header_index + 1:], header_index + 2):
        row = dict(zip(headers, [cell_value(cell) for cell in cells]))
        cells_by_header = dict(zip(headers, cells))
        asin = str(row.get("ASIN") or "").strip().upper()
        if not asin:
            continue
        if asin in records:
            duplicates.append(asin)
            continue
        rank_text = str(row.get("榜单排名") or "")
        match = re.search(r"\d+", rank_text)
        rank = int(match.group()) if match else None
        observed_at = iso_value(row.get("采集时间(UTC)"), workbook.epoch)
        product_url = hyperlink_url(cells_by_header["Amazon商品链接"])
        main_image_url = hyperlink_url(cells_by_header["Amazon主图链接"])
        if not is_https_url(product_url) or not is_https_url(main_image_url):
            raise RuntimeError(f"第{source_row}行缺少可解析的HTTPS商品或主图URL")
        records[asin] = {
            "asin": asin,
            "title": row.get("Amazon标题") or None,
            "product_url": product_url,
            "main_image_url": main_image_url,
            "specifications": row.get("Amazon规格") or row.get("商品规格/SKU") or None,
            "variants": row.get("Amazon变体/ASIN") or None,
            "brand_seller": row.get("品牌/卖家") or None,
            "detail_bsr": row.get("详情页BSR") or None,
            "availability": row.get("前台库存提示") or row.get("库存") or None,
            "detail_fields_status": row.get("详情页字段状态") or row.get("字段状态") or None,
            "card_text": row.get("原始榜单卡片文本") or None,
            "market": "US",
            "observed_at": observed_at,
            "best_seller_evidence": {
                "qualified": True,
                "status": "VERIFIED_FRONTEND_RANK",
                "rank": rank,
                "evidence": row.get("Best Seller证据") or None,
                "source_url": row.get("榜单来源URL") or None,
            },
            "source_name": "Amazon US Best Sellers frontend cards",
            "source_file": str(source),
            "source_sha256": source_hash,
            "source_row": source_row,
            "raw_record": {str(k): v for k, v in row.items() if k is not None},
        }
    workbook.close()
    if not records:
        raise RuntimeError("工作簿没有可导入的 ASIN")

    destination = args.output_dir / "amazon_frontend_bestsellers_latest.jsonl"
    temporary = destination.with_suffix(".tmp")
    with temporary.open("w", encoding="utf-8", newline="\n") as stream:
        for record in records.values():
            stream.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
    os.replace(temporary, destination)
    manifest = {
        "source_file": str(source), "evidence_copy": str(evidence), "source_sha256": source_hash,
        "amazon_count": len(records), "duplicate_asins_ignored": sorted(set(duplicates)),
        "best_seller_verified": sum(x["best_seller_evidence"]["qualified"] is True for x in records.values()),
        "output": str(destination), "external_api_requests": 0,
    }
    manifest_path = args.output_dir / "amazon_import_manifest.json"
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(manifest, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
