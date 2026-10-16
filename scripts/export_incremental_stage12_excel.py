"""Stream an incremental stage-1/2 snapshot to the established audit workbook."""
from __future__ import annotations

import argparse
import json
import math
import os
import uuid
from collections import Counter
from pathlib import Path
from urllib.parse import urlparse

import xlsxwriter


def is_https(value: object) -> bool:
    parsed = urlparse(str(value or ""))
    return parsed.scheme.lower() == "https" and bool(parsed.netloc)


def hyperlink_formula(url: object, label: str) -> str:
    clean = str(url or "").replace('"', '""')
    return f'=HYPERLINK("{clean}","{label}")'


def route_status(value: str, below_action: str) -> tuple[str, str]:
    states = {
        "STAGE1_EXCLUDED": ("第一层排除：文字硬冲突", "hard"),
        "CANDIDATE": ("进入第三层：图片向量达到候选门槛", "candidate"),
        "BELOW_VECTOR_THRESHOLD": (
            "第二层排除：图片向量低于同款门槛"
            if below_action == "FINAL_DIFFERENT"
            else "第二层未入围：图片向量低于候选门槛"
            if below_action == "NOT_SELECTED_NOT_MANUAL_REVIEW"
            else "第二层低于候选门槛：保留待复核",
            "hard" if below_action == "FINAL_DIFFERENT" else "pending",
        ),
        "WAIT_IMAGE_PROXY": ("第二层待补充：等待图片下载网络", "missing"),
        "WAIT_IMAGE_SOURCE": ("第二层待补充：来源尚无图片", "missing"),
        "FAILED_RETRYABLE": ("第二层图片处理失败，等待重试", "missing"),
        "FAILED_BOUNDED": ("第二层图片处理失败，已达重试上限", "missing"),
        "PENDING": ("第一、二层待处理", "pending"),
    }
    return states.get(value, (value or "待处理", "pending"))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--rows", type=Path, required=True)
    parser.add_argument("--summary", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--max-data-rows-per-sheet", type=int, default=1_048_572,
        help="每个明细表的数据行上限；Excel总行上限减去4行标题。",
    )
    args = parser.parse_args()
    summary_data = json.loads(args.summary.read_text(encoding="utf-8"))
    if not 1 <= args.max_data_rows_per_sheet <= 1_048_572:
        parser.error("--max-data-rows-per-sheet 必须在1到1048572之间")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    temporary = args.output.with_name(
        f".{args.output.stem}.{os.getpid()}.{uuid.uuid4().hex}.tmp.xlsx"
    )
    if temporary.exists():
        temporary.unlink()

    book = xlsxwriter.Workbook(temporary, {"constant_memory": True, "strings_to_urls": False})
    summary = book.add_worksheet("汇总")
    summary.hide_gridlines(2)
    navy = "#1F4E78"
    title = book.add_format({"font_name": "Arial", "bold": True, "font_size": 15, "font_color": "white", "bg_color": navy, "align": "center", "valign": "vcenter"})
    note = book.add_format({"font_name": "Arial", "font_size": 10, "italic": True, "text_wrap": True, "bg_color": "#D9EAF7", "valign": "vcenter"})
    header = book.add_format({"font_name": "Arial", "font_size": 10, "bold": True, "font_color": "white", "bg_color": "#4472C4", "align": "center", "valign": "vcenter", "text_wrap": True})
    text = book.add_format({"font_name": "Arial", "font_size": 10, "text_wrap": True, "valign": "top"})
    score = book.add_format({"font_name": "Arial", "font_size": 10, "num_format": "0.00", "align": "center", "valign": "top"})
    link = book.add_format({"font_name": "Arial", "font_size": 10, "font_color": "blue", "underline": 1, "align": "center", "valign": "vcenter"})
    styles = {
        "hard": book.add_format({"font_name": "Arial", "font_size": 10, "bg_color": "#FCE4D6", "text_wrap": True, "valign": "top"}),
        "pending": book.add_format({"font_name": "Arial", "font_size": 10, "bg_color": "#FFF2CC", "text_wrap": True, "valign": "top"}),
        "candidate": book.add_format({"font_name": "Arial", "font_size": 10, "bg_color": "#E2F0D9", "text_wrap": True, "valign": "top"}),
        "missing": book.add_format({"font_name": "Arial", "font_size": 10, "bg_color": "#F4CCCC", "text_wrap": True, "valign": "top"}),
    }
    candidate_threshold = float(summary_data.get("candidate_threshold") or 50)
    threshold_label = f"{candidate_threshold:g}"
    headers = ["序号", "Amazon主图", "Amazon ASIN", "Amazon标题", "TEMU主图", "TEMU商品ID", "TEMU标题", "第一层文字事实", "图片向量相似度", "第二层状态", "Amazon商品链接", "TEMU商品链接"]
    expected_rows = int(summary_data["actual_pairs"])
    detail_sheet_count = max(1, math.ceil(expected_rows / args.max_data_rows_per_sheet))
    detail_sheets: list[object] = []

    def add_detail_sheet(index: int):
        name = "第一二层全量核验" if detail_sheet_count == 1 else f"第一二层全量核验_{index}"
        sheet = book.add_worksheet(name)
        sheet.hide_gridlines(2)
        sheet.merge_range("A1:L1", "Amazon–TEMU 持续同款核验（第一、二层）", title)
        sheet.merge_range(
            "A2:L2",
            f"第一层判断文字和规格硬冲突；第二层本地图片向量达到{threshold_label}分进入第三层候选，低于门槛标记为第二层未入围且不进入人工待复核。第三层尚未启动。",
            note,
        )
        sheet.set_row(0, 28); sheet.set_row(1, 34)
        for column, value in enumerate(headers):
            sheet.write(3, column, value, header)
        for column, width in enumerate([8, 13, 16, 38, 13, 20, 42, 46, 18, 34, 18, 18]):
            sheet.set_column(column, column, width)
        # Setting a row height for every one of millions of rows is extremely
        # expensive in xlsxwriter.  The fixed default preserves the existing
        # readable layout without creating millions of row records.
        sheet.set_default_row(42)
        sheet.freeze_panes(4, 0)
        detail_sheets.append(sheet)
        return sheet

    result = add_detail_sheet(1)

    rows = 0
    row_states: Counter[str] = Counter()
    with args.rows.open("r", encoding="utf-8") as source:
        for line in source:
            if not line.strip():
                continue
            row = json.loads(line); rows += 1
            row_states[str(row.get("pair_status") or "PENDING")] += 1
            sheet_index = (rows - 1) // args.max_data_rows_per_sheet
            if sheet_index >= len(detail_sheets):
                result = add_detail_sheet(sheet_index + 1)
            else:
                result = detail_sheets[sheet_index]
            sheet_row = (rows - 1) % args.max_data_rows_per_sheet
            excel_row = sheet_row + 4
            status_text, style_key = route_status(
                str(row.get("pair_status") or ""),
                str(summary_data.get("below_threshold_action") or "REVIEW_NOT_FINAL_DIFFERENT"),
            ); style = styles[style_key]
            result.write_number(excel_row, 0, rows, style)
            for column, key, label in ((1, "amazon_image_url", "查看主图"), (4, "temu_image_url", "查看主图"), (10, "amazon_product_url", "查看商品"), (11, "temu_product_url", "查看商品")):
                url = row.get(key)
                if is_https(url):
                    result.write_formula(excel_row, column, hyperlink_formula(url, label), link, label)
                else:
                    result.write(excel_row, column, "链接缺失", styles["missing"])
            result.write(excel_row, 2, str(row.get("asin") or ""), style)
            result.write(excel_row, 3, str(row.get("amazon_title") or ""), style)
            result.write(excel_row, 5, str(row.get("goods_id") or ""), style)
            result.write(excel_row, 6, str(row.get("temu_title") or ""), style)
            result.write(excel_row, 7, str(row.get("stage1_text_fact") or ""), style)
            if isinstance(row.get("vector_score"), (int, float)):
                result.write_number(excel_row, 8, float(row["vector_score"]), score)
            else:
                result.write_blank(excel_row, 8, None, styles["missing"] if "WAIT_IMAGE" in str(row.get("pair_status")) else style)
            result.write(excel_row, 9, status_text, style)
    for index, sheet in enumerate(detail_sheets):
        sheet_rows = min(args.max_data_rows_per_sheet, max(0, rows - index * args.max_data_rows_per_sheet))
        sheet.autofilter(3, 0, sheet_rows + 3, 11)

    if rows != expected_rows:
        raise RuntimeError(f"Excel行数与快照不一致: {rows} != {summary_data['actual_pairs']}")
    expected_states = Counter(
        {str(key): int(value) for key, value in (summary_data.get("states") or {}).items() if int(value)}
    )
    if row_states != expected_states:
        raise RuntimeError(
            "Excel明细与汇总不是同一快照: "
            f"明细状态={dict(row_states)} 汇总状态={dict(expected_states)}"
        )
    summary.set_column("A:A", 34); summary.set_column("B:B", 78)
    summary.merge_range("A1:B1", "持续同款核验第一、二层汇总", title)
    states = summary_data.get("states") or {}
    entries = [
        ("数据路径", summary_data["label"]), ("固定快照时间", summary_data["snapshot_at"]),
        ("Amazon商品数", summary_data["amazon_products"]), ("TEMU去重商品数", summary_data["temu_products"]),
        ("应有商品对", summary_data["expected_pairs"]), ("已建商品对", summary_data["actual_pairs"]),
        ("第一层文字硬冲突排除", states.get("STAGE1_EXCLUDED", 0)),
        ("第三层候选", summary_data["candidates"]),
        ("待处理", states.get("PENDING", 0) + states.get("FAILED_RETRYABLE", 0)),
        ("等待来源图片", states.get("WAIT_IMAGE_SOURCE", 0)),
        ("图片处理失败且达到上限", states.get("FAILED_BOUNDED", 0)),
        ("第二层候选门槛", summary_data["candidate_threshold"]),
        ("图片向量模型", summary_data["vector_revision"]),
        ("第三层状态", summary_data["third_layer_status"]), ("本轮付费API请求", summary_data["api_requests"]),
        ("明细分表数", len(detail_sheets)),
    ]
    for index, (name, value) in enumerate(entries, 1):
        summary.write(index, 0, name, header if index == 1 else text)
        summary.write(index, 1, value, text)
    summary.freeze_panes(1, 0)
    book.close()
    try:
        os.replace(temporary, args.output)
    finally:
        temporary.unlink(missing_ok=True)
    print(json.dumps({"状态": "已生成", "文件": str(args.output), "商品对": rows, "明细分表数": len(detail_sheets)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
