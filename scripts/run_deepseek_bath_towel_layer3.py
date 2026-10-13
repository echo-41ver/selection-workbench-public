"""Run the bath-towel-set DeepSeek layer-3 queue with one deferred retry.

Queue policy is intentionally explicit:

* primary queue: each pair gets one provider attempt;
* transient failures are held, never retried immediately;
* after the primary queue drains, wait for the configured cooldown and run
  exactly one retry wave;
* anything still failing remains visible as ``待复核`` in the fixed workbook.

The script is resumable.  Results are append-only and the immutable snapshot,
revision keys and state live below a provider-specific run directory.  No API
request is made by ``--validate-only``.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from selection_workflow.bath_towel_deepseek import (  # noqa: E402
    extract_visual_facts,
    model_name,
    vision_revision_key,
)
from selection_workflow.bath_towel_layer3_v1 import MODULES, score_facts  # noqa: E402
from selection_workflow.bath_towel_cache import refresh_candidate_identity, reusable_success  # noqa: E402
from selection_workflow.excel_delivery import data_workbook_path, publish_view_copies  # noqa: E402


DEFAULT_BASE = ROOT / "runs" / "bath_towel_set"
DEFAULT_CANDIDATE = DEFAULT_BASE / "stage12_v2" / "layer3_candidates_bath_towel_set.jsonl"
DEFAULT_CONFIG = DEFAULT_BASE / "stage12_config.json"
DEFAULT_RUN = DEFAULT_BASE / "stage123" / "deepseek_bath_towel_200"
DEFAULT_RESULTS = DEFAULT_BASE / "stage123" / "results.jsonl"
DEFAULT_FINAL_EXCEL = DEFAULT_BASE / "excel" / "浴巾套装_第一二三层.xlsx"

_WRITE_LOCK = threading.Lock()


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def atomic_write(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temporary, path)


def iter_jsonl(path: Path) -> Iterable[dict[str, Any]]:
    if not path.exists():
        return
    with path.open("r", encoding="utf-8") as source:
        for line_number, line in enumerate(source, 1):
            if not line.strip():
                continue
            try:
                value = json.loads(line)
            except json.JSONDecodeError as exc:
                raise RuntimeError(f"JSONL第{line_number}行损坏: {path}: {exc}") from exc
            if isinstance(value, dict):
                yield value


def pair_id(row: Mapping[str, Any]) -> str:
    raw = "|".join(str(row.get(name) or "") for name in ("asin", "goods_id", "pair_revision"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


def source_signature(path: Path) -> dict[str, Any]:
    stat = path.stat()
    return {"path": str(path.resolve()), "size": stat.st_size, "mtime_ns": stat.st_mtime_ns}


def candidate_rows(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row in iter_jsonl(path):
        identity = pair_id(row)
        if identity in seen:
            continue
        seen.add(identity)
        row = dict(row)
        row["pair_id"] = identity
        rows.append(row)
    if not rows:
        raise RuntimeError(f"第三层候选为空: {path}")
    return rows


def ensure_snapshot(
    *,
    candidate_file: Path,
    stage12_config: Path,
    run_dir: Path,
    refresh: bool = False,
) -> tuple[Path, Path, Path]:
    frozen = run_dir / "frozen"
    frozen.mkdir(parents=True, exist_ok=True)
    frozen_candidates = frozen / "layer3_candidates_bath_towel_set.jsonl"
    frozen_rows = frozen / "layer12_rows_bath_towel_set.jsonl"
    frozen_summary = frozen / "layer12_summary_bath_towel_set.json"
    marker = run_dir / "snapshot_source.json"
    if not refresh and frozen_candidates.exists() and frozen_rows.exists() and frozen_summary.exists() and marker.exists():
        try:
            previous = json.loads(marker.read_text(encoding="utf-8"))
            source_unchanged = candidate_file.exists() and previous.get("source") == source_signature(candidate_file)
            config_unchanged = stage12_config.exists() and previous.get("config") == source_signature(stage12_config)
        except (OSError, ValueError, json.JSONDecodeError):
            source_unchanged = config_unchanged = False
        if source_unchanged and config_unchanged:
            return frozen_candidates, frozen_rows, frozen_summary

    if stage12_config.exists():
        command = [
            sys.executable, str(ROOT / "scripts" / "run_incremental_stage12.py"), "snapshot",
            "--config", str(stage12_config), "--snapshot-dir", str(frozen), "--path-key", "bath_towel_set",
        ]
        completed = subprocess.run(command, cwd=ROOT, text=True, capture_output=True, encoding="utf-8", errors="replace")
        if completed.returncode:
            raise RuntimeError(f"第三层冻结快照失败: {(completed.stderr or completed.stdout)[-3000:]}")
    else:
        if not candidate_file.exists():
            raise RuntimeError(f"找不到候选文件或stage12配置: {candidate_file}")
        shutil.copy2(candidate_file, frozen_candidates)
        raise RuntimeError("缺少stage12配置，无法生成第一二层完整冻结快照；未发起API请求")

    if not frozen_candidates.exists() or not frozen_rows.exists() or not frozen_summary.exists():
        raise RuntimeError("冻结快照输出不完整，未发起API请求")
    atomic_write(marker, {"created_at": utc_now(), "source": source_signature(candidate_file), "config": source_signature(stage12_config)})
    return frozen_candidates, frozen_rows, frozen_summary


def add_revision_keys(source: Path, destination: Path) -> list[dict[str, Any]]:
    rows = candidate_rows(source)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f".{destination.name}.{os.getpid()}.tmp")
    with temporary.open("w", encoding="utf-8") as target:
        for row in rows:
            row["pair_id"] = pair_id(row)
            try:
                row["vision_revision_key"] = vision_revision_key(
                    amazon_image=row["amazon_image_path"], temu_image=row["temu_image_path"], pair=row,
                )
            except (KeyError, OSError, ValueError) as exc:
                row["vision_revision_key"] = ""
                row["vision_revision_error"] = str(exc)[:300]
            target.write(json.dumps(row, ensure_ascii=False) + "\n")
    os.replace(temporary, destination)
    return rows


def load_latest_results(path: Path) -> dict[str, dict[str, Any]]:
    latest: dict[str, dict[str, Any]] = {}
    for row in iter_jsonl(path):
        identity = str(row.get("pair_id") or pair_id(row))
        latest[identity] = row
    return latest


def append_result(path: Path, row: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with _WRITE_LOCK:
        with path.open("a", encoding="utf-8") as target:
            target.write(json.dumps(row, ensure_ascii=False) + "\n")
            target.flush()


def retryable_failure(reason: str) -> bool:
    text = str(reason or "").lower()
    permanent = (
        "401", "403", "404", "400", "unauthorized", "forbidden", "permission",
        "invalid api key", "model not found", "invalid_request", "缺少", "image file not found",
        "no such file", "schema invalid", "bad request",
    )
    if any(token in text for token in permanent):
        return False
    transient = (
        "408", "409", "425", "429", "500", "502", "503", "504", "529",
        "timeout", "timed out", "connection", "temporarily", "unavailable",
        "overloaded", "rate limit", "too many", "reset by peer", "json", "decode",
        "empty response", "server error",
    )
    return any(token in text for token in transient)


def context_from_pair(pair: Mapping[str, Any], side: str) -> dict[str, Any]:
    value = pair.get(f"{side}_product_facts") or {}
    fields = value.get("fields") if isinstance(value, Mapping) else {}
    if not isinstance(fields, Mapping):
        fields = {}
    return {
        "material": fields.get("material"), "size": fields.get("size"),
        "specifications": fields.get("specifications"), "sku": fields.get("sku"),
        "card_text": fields.get("card_text"), "gate_text": value.get("gate_text") if isinstance(value, Mapping) else None,
    }


def process_pair(pair: Mapping[str, Any], *, phase: str, attempt: int, timeout: float, ledger: Path | None = None) -> dict[str, Any]:
    started = time.monotonic()
    base = {
        "pair_id": pair_id(pair), "asin": str(pair.get("asin") or ""), "goods_id": str(pair.get("goods_id") or ""),
        "pair_revision": pair.get("pair_revision"), "vision_revision_key": pair.get("vision_revision_key") or "",
        "attempt": attempt, "attempt_phase": phase, "started_at": utc_now(), "model": model_name(),
        "provider_request_count": 0,
    }

    def observe_request(effort: str):
        request_id = uuid.uuid4().hex
        def observe(event: dict[str, Any]) -> None:
            base["provider_request_count"] += int(event.get("provider_requests", 0))
            if ledger is not None:
                append_result(ledger, {"schema_version": "provider-request-ledger-v2", "pair_id": base["pair_id"],
                                      "request_id": request_id, "stage": effort, "started_at": base["started_at"], **event})
        return observe
    amazon_image = Path(str(pair.get("amazon_image_path") or ""))
    temu_image = Path(str(pair.get("temu_image_path") or ""))
    if not amazon_image.is_file() or not temu_image.is_file():
        reason = f"image file not found: Amazon={amazon_image.is_file()} TEMU={temu_image.is_file()}"
        return {**base, "api_status": "failed", "retryable": False, "failure_reason": reason, "usage": {}, "elapsed_seconds": round(time.monotonic() - started, 3)}
    try:
        response = extract_visual_facts(
            amazon_image=amazon_image, temu_image=temu_image,
            amazon_title=str(pair.get("amazon_title") or ""), temu_title=str(pair.get("temu_title") or ""),
            amazon_context=context_from_pair(pair, "amazon"), temu_context=context_from_pair(pair, "temu"),
            timeout_seconds=timeout, max_attempts=1, reasoning_effort="low",
            request_observer=observe_request("low"),
        )
        if response.get("status") != "success":
            reason = str(response.get("failure_reason") or "provider request failed")
            return {**base, "api_status": "failed", "retryable": retryable_failure(reason), "failure_reason": reason,
                    "usage": response.get("usage") or {}, "attempts": response.get("attempts") or [],
                    "model": response.get("model") or model_name(), "elapsed_seconds": round(time.monotonic() - started, 3)}
        facts = response["facts"]
        scored = score_facts(facts)
        low_usage = response.get("usage") or {}
        low_attempts = response.get("attempts") or []
        confidence = float(facts.get("overall_confidence") or 0.0)
        total = scored.get("total_score")
        near_same_boundary = isinstance(total, (int, float)) and 85 <= total <= 91
        needs_high = (
            near_same_boundary
            or (bool(scored.get("hard_conflicts")) and confidence < 0.75)
        )
        effort_path = ["low"]
        if needs_high:
            high = extract_visual_facts(
                amazon_image=amazon_image, temu_image=temu_image,
                amazon_title=str(pair.get("amazon_title") or ""), temu_title=str(pair.get("temu_title") or ""),
                amazon_context=context_from_pair(pair, "amazon"), temu_context=context_from_pair(pair, "temu"),
                timeout_seconds=timeout, max_attempts=1, reasoning_effort="high",
                request_observer=observe_request("high"),
            )
            effort_path.append("high")
            if high.get("status") == "success":
                facts = high["facts"]
                scored = score_facts(facts)
            response = high if high.get("status") == "success" else response
            high_usage = high.get("usage") or {}
            usage = {
                key: int(low_usage.get(key, 0) or 0) + int(high_usage.get(key, 0) or 0)
                for key in {"input_tokens", "cached_input_tokens", "output_tokens", "reasoning_tokens", "total_tokens"}
            }
            attempts = low_attempts + high.get("attempts", [])
        else:
            usage = low_usage
            attempts = low_attempts
        return {**base, "api_status": "success", "retryable": False, "failure_reason": None,
                "visual_facts": facts, "module_result": scored, "usage": usage,
                "attempts": attempts, "model": response.get("model") or model_name(),
                "reasoning_effort_path": effort_path,
                "response_id": response.get("response_id") or "", "elapsed_seconds": round(time.monotonic() - started, 3)}
    except Exception as exc:
        reason = str(exc).replace("\r", " ").replace("\n", " ")[:800]
        return {**base, "api_status": "failed", "retryable": retryable_failure(reason), "failure_reason": reason,
                "usage": {}, "elapsed_seconds": round(time.monotonic() - started, 3)}


def run_wave(
    items: list[dict[str, Any]], *, phase: str, attempt: int, workers: int, timeout: float,
    results_path: Path, latest: dict[str, dict[str, Any]], counters: dict[str, int],
) -> None:
    if not items:
        return
    batch_size = max(workers * 3, workers)
    completed_count = 0
    for offset in range(0, len(items), batch_size):
        batch = items[offset:offset + batch_size]
        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix=f"deepseek-{phase}") as pool:
            futures = {pool.submit(process_pair, pair, phase=phase, attempt=attempt, timeout=timeout,
                                   ledger=results_path.with_name("single_request_usage.jsonl")): pair for pair in batch}
            for future in as_completed(futures):
                pair = futures[future]
                try:
                    result = future.result()
                except Exception as exc:
                    result = {
                        "pair_id": pair_id(pair), "asin": pair.get("asin"), "goods_id": pair.get("goods_id"),
                        "pair_revision": pair.get("pair_revision"), "vision_revision_key": pair.get("vision_revision_key") or "",
                        "attempt": attempt, "attempt_phase": phase, "api_status": "failed", "retryable": retryable_failure(str(exc)),
                        "failure_reason": str(exc)[:800], "usage": {}, "elapsed_seconds": 0,
                    }
                identity = str(result.get("pair_id") or pair_id(pair))
                append_result(results_path, result)
                latest[identity] = result
                completed_count += 1
                counters["requests"] += int(result.get("provider_request_count", 0) or 0)
                if result.get("api_status") == "success":
                    counters["success"] += 1
                else:
                    counters["failed"] += 1
                if completed_count % 100 == 0 or completed_count == len(items):
                    print(json.dumps({"phase": phase, "completed": completed_count, "total": len(items), "success": counters["success"], "failed": counters["failed"]}, ensure_ascii=False), flush=True)


def export_excel(*, frozen_rows: Path, frozen_summary: Path, frozen_candidates: Path, results: Path, final_excel: Path, run_dir: Path, summary: dict[str, Any]) -> None:
    third_summary = run_dir / "third_summary.json"
    atomic_write(third_summary, summary)
    data_output = data_workbook_path(final_excel)
    command = [
        sys.executable, str(ROOT / "scripts" / "export_incremental_layer123_excel.py"),
        "--layer12-rows", str(frozen_rows), "--layer12-summary", str(frozen_summary),
        "--candidates", str(frozen_candidates), "--results", str(results),
        "--third-summary", str(third_summary), "--output", str(data_output),
    ]
    completed = subprocess.run(command, cwd=ROOT, text=True, capture_output=True, encoding="utf-8", errors="replace")
    if completed.returncode:
        raise RuntimeError(f"第一二三层Excel生成失败: {(completed.stderr or completed.stdout)[-4000:]}")
    validator = [
        sys.executable, str(ROOT / "scripts" / "validate_incremental_layer123_excel.py"),
        "--book", str(data_output), "--layer12-summary", str(frozen_summary),
    ]
    checked = subprocess.run(validator, cwd=ROOT, text=True, capture_output=True, encoding="utf-8", errors="replace")
    if checked.returncode:
        raise RuntimeError(f"第一二三层Excel校验失败: {(checked.stderr or checked.stdout)[-4000:]}")
    publish_view_copies(data_output, final_excel, keep_snapshots=3)
    (run_dir / "excel_export_stdout.log").write_text(completed.stdout[-10000:], encoding="utf-8")
    (run_dir / "excel_validate_stdout.log").write_text(checked.stdout[-10000:], encoding="utf-8")


def run(args: argparse.Namespace) -> dict[str, Any]:
    run_dir: Path = args.run_dir.resolve()
    run_dir.mkdir(parents=True, exist_ok=True)
    results_path: Path = args.results.resolve()
    state_path = run_dir / "state.json"
    snapshot_candidates, frozen_rows, frozen_summary = ensure_snapshot(
        candidate_file=args.candidate_file.resolve(), stage12_config=args.stage12_config.resolve(),
        run_dir=run_dir, refresh=args.refresh_snapshot,
    )
    frozen_candidates = run_dir / "frozen" / "layer3_candidates_bath_towel_set_with_revision.jsonl"
    if (
        args.refresh_snapshot
        or not frozen_candidates.exists()
        or frozen_candidates.stat().st_mtime_ns < snapshot_candidates.stat().st_mtime_ns
    ):
        rows = add_revision_keys(snapshot_candidates, frozen_candidates)
    else:
        rows = candidate_rows(frozen_candidates)
    rows = [refresh_candidate_identity(row) for row in rows]
    if args.limit:
        rows = rows[:args.limit]
        limited = run_dir / "frozen" / "layer3_candidates_bath_towel_set_limited.jsonl"
        with limited.open("w", encoding="utf-8") as target:
            for row in rows:
                target.write(json.dumps(row, ensure_ascii=False) + "\n")
        frozen_candidates = limited
    if args.validate_only:
        return {"status": "VALIDATED_ONLY", "candidates": len(rows), "results": str(results_path), "paid_api_requests": 0}

    latest = load_latest_results(results_path)
    # A success with the same immutable pair/revision is terminal.  A failure
    # with attempt >=2 is exhausted and is never retried on resume.
    def result_for_current_revision(row: Mapping[str, Any]) -> dict[str, Any]:
        prior = latest.get(str(row["pair_id"]), {})
        exact_failure = (prior.get("api_status") != "success" and bool(row.get("vision_revision_key"))
                         and prior.get("vision_revision_key") == row.get("vision_revision_key")
                         and prior.get("model") == model_name())
        return prior if exact_failure or reusable_success(prior, row) else {}

    primary = [
        row for row in rows
        if result_for_current_revision(row).get("api_status") != "success"
        and int(result_for_current_revision(row).get("attempt", 0) or 0) == 0
    ]
    started = utc_now()
    counters = {"requests": 0, "success": 0, "failed": 0}
    atomic_write(state_path, {"phase": "PRIMARY", "started_at": started, "workers": args.workers, "retry_rounds": 1, "cooldown_seconds": args.cooldown_seconds, "candidate_count": len(rows), "results": str(results_path)})
    run_wave(primary, phase="PRIMARY", attempt=1, workers=args.workers, timeout=args.timeout, results_path=results_path, latest=latest, counters=counters)

    deferred = [
        row for row in rows
        if result_for_current_revision(row).get("api_status") != "success"
        and result_for_current_revision(row).get("retryable")
        and int(result_for_current_revision(row).get("attempt", 0) or 0) == 1
    ]
    atomic_write(state_path, {"phase": "PRIMARY_DRAINED", "started_at": started, "primary_requests": counters["requests"], "primary_success": counters["success"], "primary_failed": counters["failed"], "deferred_count": len(deferred), "retry_rounds": 1, "cooldown_seconds": args.cooldown_seconds, "workers": args.workers, "results": str(results_path)})
    if deferred:
        time.sleep(max(0.0, args.cooldown_seconds))
        atomic_write(state_path, {"phase": "RETRY_WAVE", "started_at": started, "primary_requests": counters["requests"], "deferred_count": len(deferred), "workers": args.workers, "results": str(results_path)})
        run_wave(deferred, phase="RETRY_TAIL", attempt=2, workers=args.workers, timeout=args.timeout, results_path=results_path, latest=latest, counters=counters)

    final_success = sum(1 for row in rows if latest.get(row["pair_id"], {}).get("api_status") == "success")
    exhausted = sum(1 for row in rows if latest.get(row["pair_id"], {}).get("api_status") != "success")
    retry_success = sum(1 for row in rows if latest.get(row["pair_id"], {}).get("attempt_phase") == "RETRY_TAIL" and latest.get(row["pair_id"], {}).get("api_status") == "success")
    summary = {
        "schema_version": "deepseek-bath-towel-layer3-run-v1",
        "phase": "COMPLETE", "provider": "deepseek", "model": model_name(), "workers": args.workers,
        "started_at": started, "completed_at": utc_now(), "candidate_count": len(rows),
        "primary_requests": len(primary), "primary_success": max(0, counters["success"] - retry_success),
        "primary_failed": max(0, len(primary) - max(0, counters["success"] - retry_success)),
        "retry_requests": len(deferred), "retry_success": retry_success, "exhausted_or_review": exhausted,
        "current_provider_success": final_success, "failed_review": exhausted,
        "retry_policy": "主队列每对一次；主队列排空后冷却；仅补跑一轮；仍失败保留待复核",
        "cooldown_seconds": args.cooldown_seconds, "results": str(results_path),
        "excel": str(args.final_excel.resolve()), "paid_api_requests": counters["requests"],
    }
    atomic_write(run_dir / "summary.json", summary)
    atomic_write(state_path, summary)
    if not args.no_excel and not args.limit:
        export_excel(frozen_rows=frozen_rows, frozen_summary=frozen_summary, frozen_candidates=frozen_candidates, results=results_path, final_excel=args.final_excel.resolve(), run_dir=run_dir, summary=summary)
    return summary


def main() -> None:
    parser = argparse.ArgumentParser(description="浴巾套装 DeepSeek 第三层：至多4并发、尾部一次补跑")
    parser.add_argument("--candidate-file", type=Path, default=DEFAULT_CANDIDATE)
    parser.add_argument("--stage12-config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--run-dir", type=Path, default=DEFAULT_RUN)
    parser.add_argument("--results", type=Path, default=DEFAULT_RESULTS)
    parser.add_argument("--final-excel", type=Path, default=DEFAULT_FINAL_EXCEL)
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--cooldown-seconds", type=float, default=15.0)
    parser.add_argument("--timeout", type=float, default=180.0)
    parser.add_argument("--limit", type=int, default=0, help="仅本地/样本运行时限制候选数；限制模式不覆盖最终Excel")
    parser.add_argument("--refresh-snapshot", action="store_true")
    parser.add_argument("--validate-only", action="store_true", help="只检查快照和本地输入，不调用API")
    parser.add_argument("--no-excel", action="store_true")
    parser.add_argument("--model", default="deepseek-flash")
    parser.add_argument("--base-url", default="https://api.deepseek.com")
    args = parser.parse_args()
    os.environ["OPENAI_VISION_MODEL"] = args.model
    os.environ["OPENAI_BASE_URL"] = args.base_url
    if not 1 <= args.workers <= 4:
        parser.error("--workers必须在1到4之间")
    if args.cooldown_seconds < 0 or args.timeout <= 0:
        parser.error("冷却时间不能为负，timeout必须大于0")
    try:
        print(json.dumps(run(args), ensure_ascii=False, indent=2), flush=True)
    except Exception as exc:
        print(json.dumps({"status": "FAILED_BEFORE_OR_DURING_RUN", "error": str(exc)[:4000], "paid_api_requests": 0 if args.validate_only else "unknown"}, ensure_ascii=False), file=sys.stderr)
        raise


if __name__ == "__main__":
    main()
