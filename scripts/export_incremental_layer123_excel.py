"""Stream a frozen incremental layer-1/2/3 snapshot to the category workbook."""
from __future__ import annotations

import argparse
import json
import math
import os
import uuid
import sys
from pathlib import Path
from urllib.parse import urlparse

import xlsxwriter

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from selection_workflow.bath_towel_layer3_v1 import MODULES, score_facts  # noqa: E402
from selection_workflow.bath_towel_cache import reusable_success, normalized_visual_facts  # noqa: E402
from selection_workflow.excel_verdict_style import LEGEND, verdict_formats, verdict_kind

MODULE_KEYS = [
    "surface_design_weave_border", "towel_construction_pile_edge",
    "set_composition", "color_pattern_variant",
]
HEADERS = [
    "序号", "Amazon主图", "ASIN", "Amazon标题", "TEMU主图", "TEMU商品ID", "TEMU标题",
    "第一层文字事实", "图片向量相似度", "第二层状态", "Amazon商品链接", "TEMU商品链接",
    "第三层状态", "同一基础产品事实", "视觉证据", "视觉置信度", "表面设计织法边框/40",
    "毛巾结构绒面缝边/27", "可见套装组成/21", "颜色图案变体/12",
    "第三层总分", "最终判断", "判断依据", "总token", "耗时秒", "模型",
]
LAST_COLUMN = len(HEADERS) - 1
COLUMN_WIDTHS = [8,13,15,38,13,20,42,46,18,34,18,18,18,18,58,15,18,18,18,18,14,22,48,13,12,18]
COUNTED_MATCH_STATES = {"确认同款", "同款（存在规格变体）"}


def facts_for_display(facts: dict) -> dict:
    modules = facts.get("modules") or {}
    evidence: list[str] = []
    for key in MODULE_KEYS:
        row = modules.get(key) or {}
        def evidence_text(value: object) -> str:
            if isinstance(value, list):
                return "；".join(str(item).strip() for item in value if str(item).strip())
            return str(value or "").strip()

        left = evidence_text(row.get("amazon_evidence"))
        right = evidence_text(row.get("temu_evidence"))
        reason = str(row.get("reason") or row.get("comparison_reason") or "").strip()
        if left or right:
            evidence.append(f"Amazon：{left or '未知'}；TEMU：{right or '未知'}" + (f"；{reason}" if reason else ""))
    confidence = facts.get("overall_confidence")
    if isinstance(confidence, (int, float)) and confidence <= 1:
        confidence = round(confidence * 100, 2)
    base = facts.get("same_base_product", facts.get("base_relation", ""))
    if isinstance(base, bool):
        base = "yes" if base else "no"
    elif base is None or str(base).strip() == "":
        base = "uncertain"
    return {
        "base_relation": str(base or ""),
        "evidence": evidence[:4],
        "confidence": confidence,
    }


def iter_jsonl(path: Path):
    if not path.exists():
        return
    with path.open("r", encoding="utf-8") as source:
        for line in source:
            if line.strip():
                yield json.loads(line)


def read_records(path: Path) -> list[dict]:
    if not path.exists():
        return []
    if path.suffix.lower() == ".json":
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, list) else list(value.get("rows") or [])
    return list(iter_jsonl(path))


def pair_key(row: dict) -> tuple[str, str]:
    return str(row.get("asin") or ""), str(row.get("goods_id") or "")


def same_pair_inputs(current: dict, prior: dict) -> bool:
    if current.get("pair_revision") and prior.get("pair_revision"):
        return current["pair_revision"] == prior["pair_revision"]
    fields = (
        "amazon_fact_version", "temu_fact_version", "vector_revision",
        "amazon_image_path", "temu_image_path", "amazon_image_url", "temu_image_url",
    )
    return all(current.get(name) == prior.get(name) for name in fields)


def is_https(value: object) -> bool:
    parsed = urlparse(str(value or ""))
    return parsed.scheme.lower() == "https" and bool(parsed.netloc)


def hyperlink_formula(url: object, label: str) -> str:
    clean = str(url or "").replace('"', '""')
    return f'=HYPERLINK("{clean}","{label}")'


def second_status(status: str) -> str:
    return {
        "STAGE1_EXCLUDED": "第一层排除：文字硬冲突",
        "CANDIDATE": "进入第三层：图片向量达到候选门槛",
        "BELOW_VECTOR_THRESHOLD": "第二层暂不进入第三层：图片向量低于候选门槛",
        "WAIT_IMAGE_PROXY": "第二层待补充：等待图片下载网络",
        "WAIT_IMAGE_SOURCE": "第二层待补充：来源尚无图片",
        "FAILED_RETRYABLE": "第二层图片处理失败，等待重试",
        "FAILED_BOUNDED": "第二层图片处理失败，已达重试上限",
        "PENDING": "第一、二层待处理",
    }.get(status, "第一、二层待处理")


def fallback_outcome(status: str) -> tuple[str, str, str]:
    if status == "STAGE1_EXCLUDED":
        return "不适用", "不同款（文字硬冲突）", "第一层已取得双边明确的身份级文字硬冲突"
    if status == "BELOW_VECTOR_THRESHOLD":
        return "第二层未进入第三层", "第二层未入围", "图片向量相似度低于浴巾套装60分候选门槛，不进入第三层或人工待复核队列"
    if status in {"WAIT_IMAGE_PROXY", "WAIT_IMAGE_SOURCE", "FAILED_RETRYABLE", "FAILED_BOUNDED"}:
        return "等待补充图片证据", "待补充证据", "图片缺失或处理失败，未将缺失证据记为0分或不同款"
    if status == "CANDIDATE":
        return "第三层待运行", "第三层待运行", "已进入第三层候选队列，尚未取得付费模型结果"
    return "前两层待处理", "待补充证据", "前两层尚未取得完整状态"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--layer12-rows", type=Path, required=True)
    parser.add_argument("--layer12-summary", type=Path, required=True)
    parser.add_argument("--candidates", type=Path, required=True)
    parser.add_argument("--results", type=Path)
    parser.add_argument("--prior-candidates", type=Path)
    parser.add_argument("--prior-results", type=Path)
    parser.add_argument("--third-summary", type=Path)
    parser.add_argument("--same-limit", type=int, default=20)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--max-data-rows-per-sheet", type=int, default=1_048_572,
        help="每个明细表的数据行上限；Excel总行上限减去4行标题。",
    )
    args = parser.parse_args()
    if not 1 <= args.max_data_rows_per_sheet <= 1_048_572:
        parser.error("--max-data-rows-per-sheet 必须在1到1048572之间")

    summary = json.loads(args.layer12_summary.read_text(encoding="utf-8"))
    if bool(args.prior_candidates) != bool(args.prior_results):
        parser.error("--prior-candidates 和 --prior-results 必须同时提供")
    candidates = read_records(args.candidates)
    prior_candidates = read_records(args.prior_candidates) if args.prior_candidates else []
    current_by_key = {pair_key(row): row for row in candidates}
    prior_candidates = [
        row for row in prior_candidates
        if pair_key(row) in current_by_key and same_pair_inputs(current_by_key[pair_key(row)], row)
    ]
    valid_results: dict[tuple[str, str], dict] = {}
    stale_results = 0

    def ingest(candidate_rows: list[dict], result_path: Path | None) -> None:
        nonlocal stale_results
        by_key = {pair_key(row): row for row in candidate_rows}
        for row in iter_jsonl(result_path) if result_path else []:
            current = by_key.get(pair_key(row), {})
            if not reusable_success(row, current, expected_model="deepseek-flash", expected_endpoint="https://api.deepseek.com",
                                    allow_previous_prompt=True):
                stale_results += 1
                continue
            valid_results[pair_key(row)] = {**row, "visual_facts": normalized_visual_facts(row)}

    # Previous-provider results are loaded first. Current-provider successes
    # override them only for the same unchanged pair.
    ingest(prior_candidates, args.prior_results)
    ingest(candidates, args.results)
    candidate_keys = {pair_key(row) for row in [*prior_candidates, *candidates]}
    third_summary = json.loads(args.third_summary.read_text(encoding="utf-8")) if args.third_summary and args.third_summary.exists() else {}
    valid_total_tokens = sum(
        int((row.get("usage") or {}).get("total_tokens", 0) or 0)
        for row in valid_results.values()
    )
    counted_by_asin: dict[str, int] = {}
    for key, row in valid_results.items():
        facts = row.get("visual_facts") or {}
        if not facts:
            continue
        current_score = score_facts(facts)
        if str(current_score.get("state") or "") in COUNTED_MATCH_STATES:
            counted_by_asin[key[0]] = counted_by_asin.get(key[0], 0) + 1

    all_asins = {
        str(row.get("asin") or "")
        for row in iter_jsonl(args.layer12_rows)
        if str(row.get("asin") or "")
    }
    unresolved = set()
    for pair in iter_jsonl(args.layer12_rows):
        status = pair.get('pair_status')
        if status in ('STAGE1_EXCLUDED', 'BELOW_VECTOR_THRESHOLD'): continue
        result = valid_results.get(pair_key(pair), {})
        state = score_facts(result['visual_facts'])['state'] if result.get('visual_facts') else ''
        if status != 'CANDIDATE' or not (state in COUNTED_MATCH_STATES or state.startswith('不同款')):
            unresolved.add(str(pair['asin']))
    if args.same_limit < 0: raise ValueError('同款门槛不可为负数')
    low_asins = {asin for asin in all_asins if counted_by_asin.get(asin, 0) <= args.same_limit and asin not in unresolved}
    low_name = f'已核验采样同款不超过{args.same_limit}'

    args.output.parent.mkdir(parents=True, exist_ok=True)
    temporary = args.output.with_name(
        f".{args.output.stem}.{os.getpid()}.{uuid.uuid4().hex}.tmp.xlsx"
    )
    if temporary.exists():
        temporary.unlink()
    book = xlsxwriter.Workbook(temporary, {"constant_memory": True, "strings_to_urls": False})
    sm = book.add_worksheet("汇总与规则")
    sm.hide_gridlines(2)
    navy = "#1F4E78"
    title = book.add_format({"font_name":"Arial","bold":True,"font_size":15,"font_color":"white","bg_color":navy,"align":"center","valign":"vcenter"})
    note = book.add_format({"font_name":"Arial","font_size":10,"italic":True,"bg_color":"#D9EAF7","text_wrap":True,"valign":"vcenter"})
    header = book.add_format({"font_name":"Arial","font_size":10,"bold":True,"font_color":"white","bg_color":"#4472C4","align":"center","valign":"vcenter","text_wrap":True})
    text = book.add_format({"font_name":"Arial","font_size":10,"text_wrap":True,"valign":"top"})
    center = book.add_format({"font_name":"Arial","font_size":10,"text_wrap":True,"align":"center","valign":"top"})
    number = book.add_format({"font_name":"Arial","font_size":10,"num_format":"0.00","align":"center","valign":"top"})
    link = book.add_format({"font_name":"Arial","font_size":10,"font_color":"blue","underline":1,"align":"center","valign":"vcenter"})
    verdicts = verdict_formats(book)
    expected_rows = int(summary["actual_pairs"])
    detail_sheet_count = max(1, math.ceil(expected_rows / args.max_data_rows_per_sheet))
    detail_sheets: list[object] = []

    def add_detail_sheet(index: int):
        name = "全量综合结果" if detail_sheet_count == 1 else f"全量综合结果_{index}"
        sheet = book.add_worksheet(name)
        sheet.hide_gridlines(2)
        sheet.merge_range(0, 0, 0, LAST_COLUMN, "Amazon–TEMU 持续同款核验（第一、二、三层综合）", title)
        sheet.merge_range(1, 0, 1, LAST_COLUMN, "第一层仅以明确文字硬冲突排除；第二层本地图片向量只负责分流；第三层只比较两张主图中可见的品类模块。"+LEGEND, note)
        sheet.set_row(0, 28); sheet.set_row(1, 34)
        for col, value in enumerate(HEADERS): sheet.write(3, col, value, header)
        for col, width in enumerate(COLUMN_WIDTHS): sheet.set_column(col, col, width)
        sheet.freeze_panes(4, 0)
        detail_sheets.append(sheet)
        return sheet

    ws = add_detail_sheet(1)
    for index in range(2, detail_sheet_count + 1):
        add_detail_sheet(index)

    low_ws = book.add_worksheet(low_name)
    low_ws.hide_gridlines(2)
    low_ws.merge_range(0, 0, 0, LAST_COLUMN, low_name + "的Amazon商品及其全部TEMU商品对", title)
    low_ws.merge_range(1, 0, 1, LAST_COLUMN, "按Amazon ASIN统计已完成第三层的确认同款、同款规格变体和待复核；合计小于等于5的Amazon，其在主表中的全部Amazon–TEMU商品对均列入本表。", note)
    low_ws.set_row(0, 28); low_ws.set_row(1, 42)
    for col, value in enumerate(HEADERS): low_ws.write(3, col, value, header)
    for col, width in enumerate(COLUMN_WIDTHS): low_ws.set_column(col, col, width)
    low_ws.freeze_panes(4, 0)

    def write_output_row(sheet, excel_row: int, values: list[object], pair: dict, *, amazon_only: bool = False) -> None:
        formats = verdicts[verdict_kind(values[HEADERS.index('最终判断')])]
        text, center, number, link = (formats[name] for name in ('text', 'center', 'number', 'link'))
        for col, value in enumerate(values):
            if col in {1,4,10,11}:
                continue
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                sheet.write_number(excel_row, col, float(value), number)
            elif value is None:
                sheet.write_blank(excel_row, col, None, center)
            else:
                sheet.write(excel_row, col, value, text if col in {3,6,7,14,22} else center)
        links = ((1,"amazon_image_url","查看主图"),(10,"amazon_product_url","查看商品"))
        if not amazon_only:
            links += ((4,"temu_image_url","查看主图"),(11,"temu_product_url","查看商品"))
        for col, key_name, label in links:
            url = pair.get(key_name)
            if is_https(url): sheet.write_formula(excel_row, col, hyperlink_formula(url, label), link, label)
            else: sheet.write(excel_row, col, "链接缺失", center)
        if amazon_only:
            for col in (4, 11): sheet.write(excel_row, col, "", center)
        sheet.set_row(excel_row, 42)

    counts = {"same":0,"different":0,"review":0,"evidence":0,"unrun":0,"missing":0,"hard":0,"below":0}
    yn = {"yes":"是", "no":"否", "uncertain":"不确定"}
    rows = 0
    low_rows = 0
    for pair in iter_jsonl(args.layer12_rows):
        rows += 1
        sheet_index = (rows - 1) // args.max_data_rows_per_sheet
        if sheet_index >= len(detail_sheets):
            ws = add_detail_sheet(sheet_index + 1)
        else:
            ws = detail_sheets[sheet_index]
        sheet_row = (rows - 1) % args.max_data_rows_per_sheet
        excel_row = sheet_row + 4
        status = str(pair.get("pair_status") or "")
        l3 = valid_results.get(pair_key(pair)); facts = {}; scored = {}; modules = {}; usage = {}
        third, final, reason = fallback_outcome(status)
        if status == "STAGE1_EXCLUDED": counts["hard"] += 1
        elif status == "BELOW_VECTOR_THRESHOLD": counts["below"] += 1
        elif status != "CANDIDATE": counts["missing"] += 1
        if status == "CANDIDATE" and l3:
            facts = l3.get("visual_facts") or {}
            scored = l3.get("module_result") or l3.get("seven_module_result") or {}
            if facts:
                # Reapply the current deterministic Python policy to cached paid
                # facts. Rule-only fixes must not trigger another API request.
                scored = score_facts(facts)
            modules = scored.get("modules") or {}; usage = l3.get("usage") or {}
            third = "第三层已完成"
            final = str(scored.get("state") or "待复核")
            reason = str(scored.get("reason") or "第三层结果缺少判断依据")
            if final.startswith("同款") or final == "确认同款": counts["same"] += 1
            elif final == "不同款": counts["different"] += 1
            elif final == "待复核": counts["review"] += 1
            else: counts["evidence"] += 1
        elif status == "CANDIDATE": counts["unrun"] += 1
        display = facts_for_display(facts) if facts else {"base_relation":"", "evidence":[], "confidence":None}
        scores = [(modules.get(key) or {}).get("score") for key in MODULE_KEYS]
        values = [
            rows, None, str(pair.get("asin") or ""), pair.get("amazon_title"), None,
            str(pair.get("goods_id") or ""), pair.get("temu_title"), pair.get("stage1_text_fact"),
            pair.get("vector_score"), second_status(status), None, None, third,
            yn.get(display.get("base_relation"), ""), "；".join(display.get("evidence") or []), display.get("confidence"),
            *scores, scored.get("total_score"), final, reason, usage.get("total_tokens"),
            l3.get("elapsed_seconds") if l3 else None, l3.get("model") if l3 else "",
        ]
        write_output_row(ws, excel_row, values, pair)

        asin = str(pair.get("asin") or "")
        if asin in low_asins:
            low_rows += 1
            low_values = list(values)
            low_values[0] = low_rows
            write_output_row(low_ws, low_rows + 3, low_values, pair)
    for index, sheet in enumerate(detail_sheets):
        sheet_rows = min(args.max_data_rows_per_sheet, max(0, rows - index * args.max_data_rows_per_sheet))
        sheet.autofilter(3, 0, sheet_rows + 3, LAST_COLUMN)
    low_ws.autofilter(3, 0, low_rows + 3, LAST_COLUMN)
    if rows != expected_rows: raise RuntimeError(f"导出行数与冻结快照不一致: {rows} != {summary['actual_pairs']}")

    sm.set_column("A:A", 36); sm.set_column("B:B", 92); sm.merge_range("A1:B1", "持续同款核验汇总与规则", title)
    states = summary.get("states") or {}
    # Candidate counts are not API requests in one-to-many batches. Unknown
    # billing data must stay unknown, rather than falsely reporting zero.
    paid_api_requests = third_summary.get("provider_requests") if third_summary else None
    entries = [
        ("数据路径", summary.get("label")), ("固定快照时间", summary.get("snapshot_at")),
        ("Amazon商品数", summary.get("amazon_products")), ("TEMU去重商品数", summary.get("temu_products")),
        ("全量商品对", rows), ("第一层文字硬冲突排除", states.get("STAGE1_EXCLUDED",0)),
        ("第二层低于候选门槛", states.get("BELOW_VECTOR_THRESHOLD",0)), ("图片或处理证据待补", counts["missing"]),
        ("第三层候选", len(candidate_keys)), ("第三层API成功", len(valid_results)),
        ("第三层旧修订或无效结果未采用", stale_results), ("第三层确认同款", counts["same"]),
        ("第三层判定不同款", counts["different"]), ("第三层人工待复核", counts["review"]),
        ("第三层待补充证据", counts["evidence"]), ("第三层尚未运行", counts["unrun"]),
        ("本轮付费API请求", paid_api_requests if paid_api_requests is not None else "未附请求账本，请查运行目录batch_usage.jsonl"),
        ("成功结果分摊token（不含失败请求）", valid_total_tokens), ("第三层模型", "、".join(sorted({str(row.get("model") or "未记录") for row in valid_results.values()})) if valid_results else "未启动"),
        ("第一层规则", "只在双边文字明确证明身份级冲突时排除；缺失字段是未知，不当作冲突。"),
        ("第二层规则", f"本地ResNet50图片向量达到{summary.get('candidate_threshold')}分才进入第三层。低于门槛标记为第二层未入围，不进入人工待复核；向量分不加入第三层总分。"),
        ("第三层总分构成", "浴巾套装四个纯视觉模块合计100分：表面设计织法边框40、毛巾结构与可见材质外观27、可见套装组成21、颜色变体12。图案样式和相对边缘位置优先；确切纤维成分由第一层文字判断，不凭两图猜测。"),
        ("确认同款门槛", "暂定：纯视觉总分≥88、无有效硬冲突、三个关键模块达到最低分、四个模块均有效且证据质量至少为中。阈值状态为待人工真值集校准。"),
        ("不同款、补证与待复核", "可见身份级硬冲突优先判不同款；纯视觉总分≤69且关键身份模块存在双边明确不同证据时判不同款；相似不等于不同款。任一视觉模块未知时进入补证队列；四模块完整但落在灰区时进入人工待复核。"),
        ("视觉置信度", "模型对图片可见性和双边事实完整度的自报把握度，不是同款概率。无效数值不得用于确认同款；API失败不作为有效置信度0写入。"),
        ("当前限制", "当前仍是每侧一张主图。第三层未运行、第二层未入围和待补充证据均与人工待复核分开统计。"),
        ("明细分表数", len(detail_sheets)),
        (low_name + "的Amazon商品数", len(low_asins)),
    ]
    for row_index, (name, value) in enumerate(entries, 1):
        sm.write(row_index, 0, name, header if row_index == 1 else text); sm.write(row_index, 1, value, text)
        sm.set_row(row_index, 34 if isinstance(value,str) and len(value)>50 else 22)
    sm.freeze_panes(1, 0)
    book.close()
    try:
        os.replace(temporary, args.output)
    finally:
        temporary.unlink(missing_ok=True)
    print(json.dumps({"状态":"已生成","文件":str(args.output),"商品对":rows,"明细分表数":len(detail_sheets),"第三层有效结果":len(valid_results),low_name + "的Amazon商品":len(low_asins),"低同款Amazon的全部商品对":low_rows,"付费API请求":paid_api_requests}, ensure_ascii=False))


if __name__ == "__main__":
    main()
