"""Export the current multi-keyword TEMU goods_id union to an auditable workbook."""
from __future__ import annotations

import argparse
import json
import os
import sys
import uuid
from pathlib import Path
from urllib.parse import urlparse

import xlsxwriter

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from selection_workflow.excel_delivery import data_workbook_path, publish_view_copies  # noqa: E402


def is_https(value: object) -> bool:
    parsed = urlparse(str(value or ""))
    return parsed.scheme == "https" and bool(parsed.netloc)


def formula(url: object, label: str) -> str:
    return f'=HYPERLINK("{str(url).replace(chr(34), chr(34) * 2)}","{label}")'


def read_rows(path: Path) -> list[dict]:
    rows: list[dict] = []
    with path.open("r", encoding="utf-8") as source:
        for number, line in enumerate(source, 1):
            if not line.strip():
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError as error:
                raise RuntimeError(f"TEMU并集快照第{number}行损坏，保留上一版Excel") from error
    return rows


def build(input_dir: Path, output: Path, *, data_only: bool = False) -> dict:
    products_path = input_dir / "products.jsonl"
    summary_path = input_dir / "summary.json"
    rows = read_rows(products_path)
    summary = json.loads(summary_path.read_text(encoding="utf-8")) if summary_path.exists() else {}
    ids = [str(row.get("goods_id") or "").strip() for row in rows]
    if not rows or any(not value for value in ids) or len(ids) != len(set(ids)):
        raise RuntimeError("TEMU并集必须非空且按goods_id唯一；拒绝覆盖正式Excel")
    expected = int(summary.get("unique_goods_id") or len(rows))
    if expected != len(rows):
        raise RuntimeError(f"TEMU汇总与明细不一致: {expected} != {len(rows)}")

    output.parent.mkdir(parents=True, exist_ok=True)
    data_output = output if data_only else data_workbook_path(output)
    temporary = data_output.with_name(f".{data_output.stem}.{os.getpid()}.{uuid.uuid4().hex}.tmp.xlsx")
    book = xlsxwriter.Workbook(temporary, {"constant_memory": True, "strings_to_urls": False})
    navy = "#1F4E78"
    title = book.add_format({"font_name": "Arial", "font_size": 15, "bold": True, "font_color": "white", "bg_color": navy, "align": "center"})
    header = book.add_format({"font_name": "Arial", "font_size": 10, "bold": True, "font_color": "white", "bg_color": "#4472C4", "align": "center", "valign": "vcenter", "text_wrap": True})
    text = book.add_format({"font_name": "Arial", "font_size": 10, "valign": "top", "text_wrap": True})
    center = book.add_format({"font_name": "Arial", "font_size": 10, "align": "center", "valign": "top", "text_wrap": True})
    money = book.add_format({"font_name": "Arial", "font_size": 10, "num_format": "$0.00", "align": "right"})
    link = book.add_format({"font_name": "Arial", "font_size": 10, "font_color": "blue", "underline": 1, "align": "center"})

    detail = book.add_worksheet("商品明细")
    audit = book.add_worksheet("汇总与审计")
    for sheet in (detail, audit):
        sheet.hide_gridlines(2)
    headers = ["序号", "TEMU商品ID", "标题", "价格(USD)", "销量文本", "商品链接", "主图链接", "全部图片链接(JSON)", "命中搜索词", "首次发现轮次", "观测时间(UTC)", "来源"]
    detail.merge_range(0, 0, 0, len(headers) - 1, "TEMU 美国站浴巾套装关键词真实前端采集明细", title)
    detail.write_row(3, 0, headers, header)
    widths = [9, 20, 60, 14, 18, 16, 16, 62, 62, 14, 24, 30]
    for index, width in enumerate(widths):
        detail.set_column(index, index, width)
    for index, row in enumerate(rows, 4):
        detail.write_number(index, 0, index - 3, center)
        detail.write_string(index, 1, str(row.get("goods_id") or ""), center)
        detail.write(index, 2, str(row.get("title") or ""), text)
        price = row.get("price_usd")
        detail.write_number(index, 3, float(price), money) if isinstance(price, (int, float)) else detail.write_blank(index, 3, None, center)
        detail.write(index, 4, str(row.get("sales_text") or ""), center)
        for column, key, label in ((5, "product_url", "打开商品"), (6, "main_image_url", "查看主图")):
            url = row.get(key)
            detail.write_formula(index, column, formula(url, label), link, label) if is_https(url) else detail.write(index, column, "链接缺失", center)
        detail.write(index, 7, json.dumps(row.get("all_image_urls") or [], ensure_ascii=False), text)
        detail.write(index, 8, "；".join(row.get("source_queries") or [str(row.get("search_query") or "")]), text)
        detail.write(index, 9, row.get("first_seen_round"), center)
        detail.write(index, 10, str(row.get("observed_at") or ""), center)
        detail.write(index, 11, str(row.get("source") or "VISIBLE_TEMU_FRONTEND"), center)
    detail.freeze_panes(4, 0)
    detail.autofilter(3, 0, len(rows) + 3, len(headers) - 1)

    audit.merge_range("A1:B1", "TEMU关键词采集汇总与goods_id审计", title)
    audit.set_column("A:A", 38)
    audit.set_column("B:B", 90)
    entries = [
        ("数据来源", "TEMU 美国站真实前端多个关键词可见搜索结果并集"),
        ("源快照数量", int(len(summary.get("input_snapshots") or []))),
        ("源记录总数", int(summary.get("source_rows") or len(rows))),
        ("唯一goods_id数", len(rows)),
        ("合并重复记录数", int(summary.get("duplicate_rows_merged") or 0)),
        ("有商品链接", sum(is_https(row.get("product_url")) for row in rows)),
        ("有主图链接", sum(is_https(row.get("main_image_url")) for row in rows)),
        ("内容SHA-256", str(summary.get("content_sha256") or "未记录")),
        ("口径说明", "只按goods_id去重；多个关键词命中同一商品时合并搜索词，不因标题或主图相同删除不同刊登。"),
    ]
    for row_index, (name, value) in enumerate(entries, 2):
        audit.write(row_index - 1, 0, name, header)
        audit.write(row_index - 1, 1, value, text)
    audit.freeze_panes(1, 0)
    book.close()
    os.replace(temporary, data_output)
    delivery = None if data_only else publish_view_copies(data_output, output, keep_snapshots=3)
    state = {"状态": "数据源已生成，等待验收和发布" if data_only else "已生成并发布", "商品数": len(rows), "Excel数据源": str(data_output), "Excel查看副本": None if data_only else str(output), "发布": delivery}
    (output.parent / "temu_excel_export_state.json").write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    return state


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--data-only", action="store_true", help="仅原子写入数据工作簿，由调用者验收后发布查看副本")
    args = parser.parse_args()
    print(json.dumps(build(args.input.resolve(), args.output.resolve(), data_only=args.data_only), ensure_ascii=False))


if __name__ == "__main__":
    main()
