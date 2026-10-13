#!/usr/bin/env python3
"""Replace multiple ProxyShare/AdsPower workers concurrently; 60s is a speed target."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from selection_automation.background_launch import run_background_launcher


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--workers", required=True, help="Comma-separated worker numbers, e.g. 1,2,3")
    parser.add_argument("--start-collector", action="store_true")
    parser.add_argument("--category", type=Path)
    parser.add_argument("--result", type=Path, help="Isolated result file for this request")
    args = parser.parse_args()
    workers = sorted({int(value) for value in args.workers.split(",") if value.strip()})
    if not workers or any(value not in range(1, 33) for value in workers):
        raise SystemExit("workers must be within 1..32")
    started = time.monotonic()

    def replace(worker: int) -> dict:
        command = [sys.executable, str(ROOT / "scripts" / "replace_proxyshare_worker_environment.py"), "--worker", str(worker)]
        if args.start_collector:
            command.append("--start-collector")
        if args.category:
            command += ["--category", str(args.category)]
        # The speed target is observational: a slow AdsPower startup must not
        # cancel a valid replacement halfway through and strand the worker.
        completed = run_background_launcher(command, cwd=ROOT, timeout=600)
        if completed.returncode:
            raise RuntimeError(f"worker {worker}: {(completed.stderr or completed.stdout)[-1500:]}")
        line = next((line for line in reversed(completed.stdout.splitlines()) if line.strip().startswith("{")), "{}")
        return json.loads(line)

    results = []
    with ThreadPoolExecutor(max_workers=len(workers)) as pool:
        futures = {pool.submit(replace, worker): worker for worker in workers}
        for future in as_completed(futures):
            try:
                results.append(future.result())
            except Exception as exc:
                results.append({"worker": futures[future], "status": "FAILED", "reason": str(exc)})
    elapsed = round(time.monotonic() - started, 3)
    output = {
        "status": "BATCH_PARTIAL_FAILED" if any(r.get("status") == "FAILED" for r in results) else "BATCH_REPLACED",
        "workers": workers,
        "elapsed_seconds": elapsed,
        "speed_target_seconds": 60,
        "speed_target_met": elapsed < 60,
        "results": sorted(results, key=lambda item: item["worker"]),
    }
    print(json.dumps(output, ensure_ascii=False))
    from replace_proxyshare_worker_environment import atomic_json
    atomic_json(args.result or ROOT / "runtime" / "proxyshare-chains" / "batch_latest.json", output)


if __name__ == "__main__":
    main()
