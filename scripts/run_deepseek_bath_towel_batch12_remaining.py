"""Finish failed bath-towel pairs using one Amazon image plus up to 12 TEMU images.

The provider request is batched, while durable results remain pair-addressable.
Batch usage is written once to a separate ledger; per-pair ``usage`` is an
exact integer allocation whose sum equals the provider usage.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import threading
import time
import uuid
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping

ROOT = Path(__file__).resolve().parents[1]
import sys

sys.path.insert(0, str(ROOT / "src"))

from selection_workflow.bath_towel_deepseek import (  # noqa: E402
    FACT_SCHEMA,
    FIXED_RELATION_PROMPT,
    _api_key,
    _data_url,
    _sanitize_error,
    _short_context,
    _usage_dict,
    extract_visual_facts,
    model_name,
    provider_endpoint,
    validate_facts,
)
from selection_workflow.bath_towel_layer3_v1 import MODULES, score_facts  # noqa: E402
from selection_workflow.bath_towel_cache import refresh_candidate_identity, reusable_success  # noqa: E402


DEFAULT_BASE = ROOT / "runs" / "bath_towel_set"
DEFAULT_CANDIDATES = DEFAULT_BASE / "stage123" / "deepseek_bath_towel_200" / "frozen" / "layer3_candidates_bath_towel_set_with_revision.jsonl"
DEFAULT_RESULTS = DEFAULT_BASE / "stage123" / "results.jsonl"
DEFAULT_RUN_DIR = DEFAULT_BASE / "stage123" / "deepseek_batch12_remaining"
USAGE_KEYS = ("input_tokens", "cached_input_tokens", "output_tokens", "reasoning_tokens", "total_tokens")
WRITE_LOCK = threading.Lock()
CLIENT_LOCAL = threading.local()
FATAL_STOP = threading.Event()


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def iter_jsonl(path: Path) -> Iterable[dict[str, Any]]:
    if not path.exists():
        return
    with path.open("r", encoding="utf-8") as source:
        for line in source:
            if line.strip():
                value = json.loads(line)
                if isinstance(value, dict):
                    yield value


def pair_id(row: Mapping[str, Any]) -> str:
    raw = "|".join(str(row.get(name) or "") for name in ("asin", "goods_id", "pair_revision"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


def context_from_pair(pair: Mapping[str, Any], side: str) -> dict[str, Any]:
    value = pair.get(f"{side}_product_facts") or {}
    fields = value.get("fields") if isinstance(value, Mapping) else {}
    if not isinstance(fields, Mapping):
        fields = {}
    return {
        "material": fields.get("material"), "size": fields.get("size"),
        "specifications": fields.get("specifications"), "sku": fields.get("sku"),
        "card_text": fields.get("card_text"),
        "gate_text": value.get("gate_text") if isinstance(value, Mapping) else None,
    }


def append_jsonl(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with WRITE_LOCK:
        with path.open("a", encoding="utf-8") as target:
            target.write(json.dumps(value, ensure_ascii=False) + "\n")
            target.flush()


def latest_results(path: Path) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for row in iter_jsonl(path):
        result[str(row.get("pair_id") or pair_id(row))] = row
    return result


def split_usage(usage: Mapping[str, Any], count: int) -> list[dict[str, int]]:
    shares = [{key: 0 for key in USAGE_KEYS} for _ in range(count)]
    for key in USAGE_KEYS:
        total = int(usage.get(key, 0) or 0)
        quotient, remainder = divmod(total, count)
        for index in range(count):
            shares[index][key] = quotient + (1 if index < remainder else 0)
    return shares


def batch_schema(count: int) -> dict[str, Any]:
    return {
        "type": "object", "additionalProperties": False, "required": ["candidates"],
        "properties": {
            "candidates": {
                "type": "array", "minItems": count, "maxItems": count,
                "items": {
                    "type": "object", "additionalProperties": False,
                    "required": ["i", "m", "c"],
                    "properties": {
                        "i": {"type": "string"},
                        "m": FACT_SCHEMA["properties"]["m"],
                        "c": FACT_SCHEMA["properties"]["c"],
                    },
                },
            }
        },
    }


BATCH_PROMPT = FIXED_RELATION_PROMPT.replace(
    "第一张图是 Amazon 主图，第二张图是 TEMU 主图。",
    "第一张图是唯一的 Amazon 基准商品；之后按文字标签依次提供多个 TEMU 候选商品图片。",
).replace(
    "只能使用这两张图片中的可见事实。",
    "每个候选只能使用Amazon基准图与该候选图中的可见事实。",
) + """

本次必须分别、独立判断每个 TEMU 候选，禁止候选之间互相比较，禁止排名，禁止只挑最像的一个；多个候选可以同时属于同款，也可以全部不是。输出 candidates 数组，数量必须与输入候选完全一致。i 必须原样返回紧邻该图片之前的候选短ID（例如 C01），不得遗漏、重复、改写或增加ID。每个候选的 m/c 规则与单对JSON完全相同。
"""


def client_for_thread(timeout: float):
    from openai import OpenAI

    identity = (model_name(), provider_endpoint(), timeout)
    client = getattr(CLIENT_LOCAL, "client", None)
    if client is None or getattr(CLIENT_LOCAL, "identity", None) != identity:
        client = OpenAI(api_key=_api_key(), base_url=provider_endpoint(), max_retries=0, timeout=timeout)
        CLIENT_LOCAL.client = client
        CLIENT_LOCAL.identity = identity
    return client


def call_batch(rows: list[dict[str, Any]], timeout: float, observer: Callable[[dict[str, Any]], None] | None = None) -> dict[str, Any]:
    amazon = rows[0]
    amazon_image = Path(str(amazon.get("amazon_image_path") or ""))
    if not amazon_image.is_file():
        raise RuntimeError(f"Amazon image file not found: {amazon_image}")
    content: list[dict[str, Any]] = [
        {"type": "input_text", "text": "Amazon基准主图："},
        {"type": "input_image", "image_url": _data_url(amazon_image), "detail": "high"},
    ]
    expected: dict[str, dict[str, Any]] = {}
    for index, row in enumerate(rows, 1):
        short_id = f"C{index:02d}"
        expected[short_id] = row
        temu_image = Path(str(row.get("temu_image_path") or ""))
        if not temu_image.is_file():
            raise RuntimeError(f"TEMU image file not found: {temu_image}")
        content.extend([
            {"type": "input_text", "text": f"TEMU候选ID={short_id}："},
            {"type": "input_image", "image_url": _data_url(temu_image), "detail": "high"},
        ])
    started = time.monotonic()
    client = client_for_thread(timeout)
    if observer:
        observer({"status": "request_started", "provider_requests": 1})
    response = client.responses.create(
        model=model_name(), instructions=BATCH_PROMPT,
        input=[{"role": "user", "content": content}],
        text={"format": {"type": "json_schema", "name": "bath_towel_batch12_facts", "strict": True, "schema": batch_schema(len(rows))}},
        reasoning={"effort": "low"}, max_output_tokens=24000,
    )
    usage = _usage_dict(response)
    response_id = str(getattr(response, "id", "") or "")
    if observer:
        observer({"status": "response_received", "provider_requests": 0, "usage": usage, "response_id": response_id})
    raw = json.loads(str(getattr(response, "output_text", "") or ""))
    items = raw.get("candidates") if isinstance(raw, Mapping) else None
    if not isinstance(items, list):
        raise ValueError("批量响应缺少candidates数组")
    returned: dict[str, dict[str, Any]] = {}
    for item in items:
        if not isinstance(item, Mapping):
            raise ValueError("批量响应候选项不是对象")
        short_id = str(item.get("i") or "")
        if short_id in returned:
            raise ValueError(f"批量响应重复ID: {short_id}")
        returned[short_id] = validate_facts({"m": item.get("m"), "c": item.get("c")})
    if set(returned) != set(expected):
        raise ValueError(f"批量响应ID不完整: expected={sorted(expected)} actual={sorted(returned)}")
    return {
        "facts": returned, "mapping": expected, "usage": usage,
        "response_id": response_id,
        "elapsed_seconds": round(time.monotonic() - started, 3),
    }


def needs_high(facts: Mapping[str, Any], scored: Mapping[str, Any]) -> bool:
    confidence = float(facts.get("overall_confidence") or 0.0)
    return bool(scored.get("hard_conflicts") and confidence < 0.65)


def call_high(row: Mapping[str, Any], timeout: float, observer: Callable[[dict[str, Any]], None] | None = None) -> dict[str, Any]:
    return extract_visual_facts(
        amazon_image=row["amazon_image_path"], temu_image=row["temu_image_path"],
        amazon_title=str(row.get("amazon_title") or ""), temu_title=str(row.get("temu_title") or ""),
        amazon_context=context_from_pair(row, "amazon"), temu_context=context_from_pair(row, "temu"),
        timeout_seconds=timeout, max_attempts=1, reasoning_effort="high",
        request_observer=observer,
    )


def process_batch(rows: list[dict[str, Any]], *, results: Path, ledger: Path, timeout: float) -> dict[str, Any]:
    batch_id = hashlib.sha256("|".join(pair_id(row) for row in rows).encode()).hexdigest()[:20]
    started_at = utc_now()
    requests = 0
    high_count = 0
    completed_pairs = 0
    batch = None

    def observe_request(stage: str):
        request_id = uuid.uuid4().hex
        def observe(event: dict[str, Any]) -> None:
            nonlocal requests
            requests += int(event.get("provider_requests", 0))
            append_jsonl(ledger, {"schema_version": "provider-request-ledger-v2", "batch_id": batch_id,
                                 "request_id": request_id, "started_at": started_at, "stage": stage, **event})
        return observe

    if FATAL_STOP.is_set():
        return {"status": "skipped", "success_pairs": 0, "failed_pairs": len(rows), "requests": 0, "high": 0}
    try:
        batch = call_batch(rows, timeout, observe_request("low"))
        shares = split_usage(batch["usage"], len(rows))
        high_count = 0
        high_usage_total = {key: 0 for key in USAGE_KEYS}
        for index, (short_id, row) in enumerate(batch["mapping"].items()):
            facts = batch["facts"][short_id]
            scored = score_facts(facts)
            usage = shares[index]
            effort_path = ["low"]
            response_id = batch["response_id"]
            request_share = 1 if index == 0 else 0
            if needs_high(facts, scored):
                high = call_high(row, timeout, observe_request("high"))
                request_share += 1
                effort_path.append("high")
                high_count += 1
                for key in USAGE_KEYS:
                    high_tokens = int((high.get("usage") or {}).get(key, 0) or 0)
                    usage[key] += high_tokens
                    high_usage_total[key] += high_tokens
                if high.get("status") == "success":
                    facts = high["facts"]
                    scored = score_facts(facts)
                    response_id = high.get("response_id") or response_id
            result = {
                "pair_id": str(row.get("pair_id") or pair_id(row)),
                "asin": str(row.get("asin") or ""), "goods_id": str(row.get("goods_id") or ""),
                "pair_revision": row.get("pair_revision"), "vision_revision_key": row.get("vision_revision_key") or "",
                "attempt": 1, "attempt_phase": "BATCH12_LOW", "started_at": started_at,
                "api_status": "success", "retryable": False, "failure_reason": None,
                "visual_facts": facts, "module_result": scored, "usage": usage,
                "model": model_name(), "response_id": response_id,
                "reasoning_effort_path": effort_path, "provider_request_count": request_share,
                "batch_id": batch_id, "batch_size": len(rows), "batch_candidate_id": short_id,
                "elapsed_seconds": batch["elapsed_seconds"],
            }
            append_jsonl(results, result)
            completed_pairs += 1
        append_jsonl(ledger, {
            "schema_version": "provider-request-ledger-v2",
            "batch_id": batch_id, "started_at": started_at, "status": "success", "asin": rows[0].get("asin"),
            "pair_count": len(rows), "provider_requests": 0, "high_reruns": high_count,
            "elapsed_seconds": batch["elapsed_seconds"],
        })
        return {"status": "success", "success_pairs": len(rows), "failed_pairs": 0,
                "requests": requests, "high": high_count, "usage": batch["usage"]}
    except Exception as exc:
        reason = _sanitize_error(exc)
        if "402" in reason or "Insufficient Balance" in reason or "invalid api key" in reason.lower():
            FATAL_STOP.set()
        append_jsonl(ledger, {
            "schema_version": "provider-request-ledger-v2",
            "batch_id": batch_id, "started_at": started_at, "status": "failed", "asin": rows[0].get("asin"),
            "pair_count": len(rows), "provider_requests": 0, "high_reruns": high_count, "failure_reason": reason,
        })
        if not FATAL_STOP.is_set() and len(rows) > 1 and batch is None and requests:
            midpoint = len(rows) // 2
            children = [
                process_batch(part, results=results, ledger=ledger, timeout=timeout)
                for part in (rows[:midpoint], rows[midpoint:]) if part
            ]
            success_pairs = sum(int(item.get("success_pairs", 0) or 0) for item in children)
            failed_pairs = sum(int(item.get("failed_pairs", 0) or 0) for item in children)
            return {
                "status": "success" if failed_pairs == 0 else "failed",
                "success_pairs": success_pairs, "failed_pairs": failed_pairs,
                "requests": requests + sum(int(item.get("requests", 0) or 0) for item in children),
                "high": sum(int(item.get("high", 0) or 0) for item in children),
            }
        return {"status": "failed", "success_pairs": completed_pairs, "failed_pairs": len(rows) - completed_pairs,
                "requests": requests, "high": high_count, "failure_reason": reason}


def run(args: argparse.Namespace) -> dict[str, Any]:
    FATAL_STOP.clear()
    candidates = []
    for row in iter_jsonl(args.candidates):
        row = refresh_candidate_identity(row)
        candidates.append(row)
    if not candidates:
        raise RuntimeError("第三层候选为空，不能报告完成")
    latest = latest_results(args.results)
    remaining = [row for row in candidates if not reusable_success(latest.get(row["pair_id"], {}), row)]
    plan = {"candidate_count": len(candidates), "cached_success": len(candidates) - len(remaining),
            "remaining_pairs": len(remaining), "would_request": len(remaining), "paid_api_requests": 0,
            "candidate_set_key": hashlib.sha256("|".join(sorted(row["vision_revision_key"] for row in candidates)).encode()).hexdigest()}
    if getattr(args, "plan_only", False):
        print(json.dumps(plan, ensure_ascii=False), flush=True)
        return plan
    if remaining and not _api_key():
        raise RuntimeError("缺少DEEPSEEK_API_KEY")
    if args.limit_pairs:
        remaining = remaining[:args.limit_pairs]
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in remaining:
        groups[str(row.get("asin") or "")].append(row)
    batches = [items[offset:offset + args.batch_size] for items in groups.values() for offset in range(0, len(items), args.batch_size)]
    args.run_dir.mkdir(parents=True, exist_ok=True)
    ledger = args.run_dir / "batch_usage.jsonl"
    summary_path = args.run_dir / "summary.json"
    started = utc_now()
    totals = {"batches": len(batches), "pairs": len(remaining), "success_pairs": 0, "failed_pairs": 0, "provider_requests": 0, "high_reruns": 0}
    with ThreadPoolExecutor(max_workers=args.workers, thread_name_prefix="deepseek-batch12") as pool:
        futures = {pool.submit(process_batch, batch, results=args.results, ledger=ledger, timeout=args.timeout): batch for batch in batches}
        for number, future in enumerate(as_completed(futures), 1):
            outcome = future.result()
            totals["provider_requests"] += int(outcome.get("requests", 0))
            totals["high_reruns"] += int(outcome.get("high", 0))
            totals["success_pairs"] += int(outcome.get("success_pairs", 0) or 0)
            totals["failed_pairs"] += int(outcome.get("failed_pairs", 0) or 0)
            if number % 10 == 0 or number == len(batches):
                print(json.dumps({"completed_batches": number, **totals}, ensure_ascii=False), flush=True)
    usage = {key: 0 for key in USAGE_KEYS}
    successful_batches = 0
    ledger_requests = 0
    started_requests: set[str] = set()
    received_requests: set[str] = set()
    legacy_unknown_requests = 0
    for row in iter_jsonl(ledger):
        ledger_requests += int(row.get("provider_requests", 0) or 0)
        if row.get("status") == "request_started":
            started_requests.add(str(row["request_id"]))
        if row.get("status") == "response_received":
            received_requests.add(str(row["request_id"]))
            for key in USAGE_KEYS:
                usage[key] += int((row.get("usage") or {}).get(key, 0) or 0)
        if row.get("status") == "failed" and row.get("schema_version") != "provider-request-ledger-v2":
            legacy_unknown_requests += int(row.get("provider_requests", 0) or 0)
        if row.get("status") == "success":
            successful_batches += 1
            for key in USAGE_KEYS:
                usage[key] += int((row.get("batch_low_usage") or {}).get(key, 0) or 0)
                usage[key] += int((row.get("high_usage") or {}).get(key, 0) or 0)
    summary = {
        "schema_version": "deepseek-bath-towel-batch12-v1", "started_at": started, "completed_at": utc_now(),
        "batch_size": args.batch_size, "workers": args.workers, **plan, **totals,
        "paid_api_requests": totals["provider_requests"],
        "ledger_provider_requests": ledger_requests,
        "usage_incomplete_requests": len(started_requests - received_requests) + legacy_unknown_requests,
        "accounted_usage": usage,
        "successful_batch_low_usage": usage, "successful_batches_in_ledger": successful_batches,
        "results": str(args.results.resolve()), "ledger": str(ledger.resolve()),
    }
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2), flush=True)
    return summary


def main() -> None:
    parser = argparse.ArgumentParser(description="浴巾套装剩余失败对：Amazon一对TEMU十二的DeepSeek批量复跑")
    parser.add_argument("--candidates", type=Path, default=DEFAULT_CANDIDATES)
    parser.add_argument("--results", type=Path, default=DEFAULT_RESULTS)
    parser.add_argument("--run-dir", type=Path, default=DEFAULT_RUN_DIR)
    parser.add_argument("--batch-size", type=int, default=12, choices=range(1, 13))
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--timeout", type=float, default=300.0)
    parser.add_argument("--limit-pairs", type=int, default=0, help="样本门禁使用；0表示全部剩余候选")
    parser.add_argument("--plan-only", action="store_true", help="只计算共享成功缓存与剩余任务，不创建客户端、不写结果、不请求API")
    parser.add_argument("--model", default="deepseek-flash", help="浴巾专用模型；换模型须显式指定，不继承其他任务的全局模型变量")
    parser.add_argument("--base-url", default="https://api.deepseek.com")
    args = parser.parse_args()
    os.environ["OPENAI_VISION_MODEL"] = args.model
    os.environ["OPENAI_BASE_URL"] = args.base_url
    run(args)


if __name__ == "__main__":
    main()
