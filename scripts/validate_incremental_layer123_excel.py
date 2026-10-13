"""Validate a frozen incremental category layer-1/2/3 workbook."""
from __future__ import annotations

import argparse
import json
import re
import zipfile
import hashlib
from pathlib import Path

from openpyxl import load_workbook
from openpyxl.utils import get_column_letter

EXPECTED_HEADERS = ["序号","Amazon主图","ASIN","Amazon标题","TEMU主图","TEMU商品ID","TEMU标题","第一层文字事实","图片向量相似度","第二层状态","Amazon商品链接","TEMU商品链接","第三层状态","同一基础产品事实","视觉证据","视觉置信度","表面设计织法边框/40","毛巾结构绒面缝边/27","可见套装组成/21","颜色图案变体/12","第三层总分","最终判断","判断依据","总token","耗时秒","模型"]
FINAL_STATE_COLUMN = EXPECTED_HEADERS.index("最终判断")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--book", type=Path, required=True)
    parser.add_argument("--layer12-summary", type=Path, required=True)
    parser.add_argument("--expect-api-requests", type=int)
    args = parser.parse_args()
    source_summary = json.loads(args.layer12_summary.read_text(encoding="utf-8"))
    book = load_workbook(args.book, read_only=True, data_only=False)
    # Summary first, one or more full-detail sheets, then the requested
    # low-match Amazon view with exactly the same category-column header.
    low_name=book.sheetnames[-1]
    assert low_name.startswith('已核验采样同款不超过')
    limit=int(low_name.removeprefix('已核验采样同款不超过'))
    assert book.sheetnames[0] == "汇总与规则" and book.sheetnames[-1] == low_name, book.sheetnames
    assert all(name == "全量综合结果" or name.startswith("全量综合结果_") for name in book.sheetnames[1:-1]), book.sheetnames
    sheets = [book[name] for name in book.sheetnames[1:-1]]
    assert sheets
    for sheet in sheets:
        assert [cell.value for cell in next(sheet.iter_rows(min_row=4,max_row=4))] == EXPECTED_HEADERS
    actual_rows = sum(sheet.max_row - 4 for sheet in sheets)
    assert actual_rows == int(source_summary["actual_pairs"])
    assert actual_rows == int(source_summary["amazon_products"]) * int(source_summary["temu_products"])
    low_sheet = book[low_name]
    assert [cell.value for cell in next(low_sheet.iter_rows(min_row=4,max_row=4))] == EXPECTED_HEADERS
    counted_states = {"确认同款", "同款（存在规格变体）"}
    low_row_counts: dict[str, int] = {}
    low_asins: set[str] = set()
    low_pairs = {}
    def row_identity(row):
        return (str(row[2]), str(row[5]))
    def row_digest(row):
        return hashlib.sha256(json.dumps(list(row[1:]), ensure_ascii=False, default=str).encode()).hexdigest()
    for row in low_sheet.iter_rows(min_row=5, values_only=True):
        asin = str(row[2] or "")
        assert asin
        low_asins.add(asin)
        low_row_counts[asin] = low_row_counts.get(asin, 0) + 1
        assert row_identity(row) not in low_pairs, "低同款表出现重复商品对"
        low_pairs[row_identity(row)] = row_digest(row)
    forbidden = {"STAGE1_EXCLUDED","BELOW_VECTOR_THRESHOLD","CANDIDATE","WAIT_IMAGE_SOURCE","WAIT_IMAGE_PROXY","FAILED_RETRYABLE","FAILED_BOUNDED","PENDING"}
    formula_links = 0
    full_row_counts: dict[str, int] = {}
    counted_by_asin: dict[str, int] = {}
    seen = set()
    unresolved = set()
    for row in (row for sheet in sheets for row in sheet.iter_rows(min_row=5, values_only=True)):
        asin = str(row[2] or "")
        assert asin
        key = row_identity(row)
        assert key not in seen, "主表出现重复商品对"
        seen.add(key)
        if asin in low_asins:
            assert low_pairs.get(key) == row_digest(row), "低同款表明细不等于主表完整商品对"
        full_row_counts[asin] = full_row_counts.get(asin, 0) + 1
        if str(row[12] or "") == "第三层已完成" and str(row[FINAL_STATE_COLUMN] or "") in counted_states:
            counted_by_asin[asin] = counted_by_asin.get(asin, 0) + 1
        if not (str(row[FINAL_STATE_COLUMN] or '') in counted_states or str(row[FINAL_STATE_COLUMN] or '').startswith('不同款') or str(row[9] or '').startswith('文字排除') or str(row[FINAL_STATE_COLUMN] or '') == '第二层未入围'):
            unresolved.add(asin)
        assert str(row[9] or "") not in forbidden and str(row[12] or "") not in forbidden and str(row[FINAL_STATE_COLUMN] or "") not in forbidden
        for index in (1,4,10,11):
            value = str(row[index] or "")
            assert value == "链接缺失" or value.startswith('=HYPERLINK("https://'), (index, value[:100])
            formula_links += value.startswith('=HYPERLINK("https://')
    expected_low_asins = {asin for asin in full_row_counts if counted_by_asin.get(asin, 0) <= limit and asin not in unresolved}
    assert low_asins == expected_low_asins, (low_asins, expected_low_asins)
    assert all(low_row_counts[asin] == full_row_counts[asin] for asin in low_asins)
    summary_sheet = book["汇总与规则"]
    summary = {str(row[0].value):row[1].value for row in summary_sheet.iter_rows(min_row=2) if row[0].value}
    assert int(summary["全量商品对"]) == actual_rows
    if args.expect_api_requests is not None:
        assert int(summary["本轮付费API请求"]) == args.expect_api_requests
    with zipfile.ZipFile(args.book) as archive:
        last_column = get_column_letter(len(EXPECTED_HEADERS))
        native_hyperlinks = 0
        for index, sheet in enumerate(sheets, 2):
            xml = archive.read(f"xl/worksheets/sheet{index}.xml")
            assert b'topLeftCell="A5"' in xml and b'state="frozen"' in xml
            assert f'A4:{last_column}{sheet.max_row}'.encode("ascii") in xml
            native_hyperlinks += len(re.findall(br"<hyperlink\s", xml))
        assert formula_links > 0
        low_xml = archive.read(f"xl/worksheets/sheet{len(book.sheetnames)}.xml")
        assert b'topLeftCell="A5"' in low_xml and b'state="frozen"' in low_xml
        assert f'A4:{last_column}{low_sheet.max_row}'.encode("ascii") in low_xml
    book.close()
    print(json.dumps({"状态":"验证通过","商品对":actual_rows,"同款数小于等于5的Amazon商品":len(low_asins),"低同款Amazon的全部商品对":sum(low_row_counts.values()),"公式可点击链接":formula_links,"原生超链接对象":native_hyperlinks,"付费API请求":summary["本轮付费API请求"]}, ensure_ascii=False))


if __name__ == "__main__": main()
