"""Run prepared local shower-curtain layers sequentially in one background process."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]


def save(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temporary, path)


def pair_counts(database: Path) -> dict[str, int]:
    if not database.exists():
        return {}
    with sqlite3.connect(f"file:{database.as_posix()}?mode=ro", uri=True, timeout=10) as connection:
        return {str(status): int(count) for status, count in connection.execute(
            "SELECT status,count(*) FROM pairs GROUP BY status"
        )}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--category", type=Path, required=True)
    parser.add_argument("--config", type=Path, required=True)
    args = parser.parse_args()
    category = json.loads(args.category.read_text(encoding="utf-8"))
    config = json.loads(args.config.read_text(encoding="utf-8"))
    base = ROOT / "runs" / category["category_key"]
    logs = base / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    state_path = base / "local_analysis_state.json"
    database = Path(config["output_dir"]) / "stage12.sqlite3"
    stage12_stop = Path(config["output_dir"]) / "STOP"
    candidate = Path(config["output_dir"]) / f"layer3_candidates_{category['category_key']}.jsonl"
    results = base / "stage123" / "results.jsonl"
    reuse_report = base / "stage123" / "prior_layer3_reuse_report.json"
    user_stop = base / "STOP"
    stage12_stop.unlink(missing_ok=True)
    user_stop.unlink(missing_ok=True)
    environment = os.environ.copy()
    environment.update({"PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8", "PYTHONUNBUFFERED": "1"})
    save(state_path, {"状态": "启动中", "supervisor_pid": os.getpid(), "品类": category["category_name_zh"]})
    stage12_stdout = (logs / "stage12.log").open("ab")
    stage12_stderr = (logs / "stage12_error.log").open("ab")
    watcher = subprocess.Popen(
        [sys.executable, str(ROOT / "scripts" / "run_incremental_stage12.py"), "watch", "--config", str(args.config)],
        cwd=ROOT, env=environment, stdout=stage12_stdout, stderr=stage12_stderr,
    )
    try:
        while not user_stop.exists():
            states = pair_counts(database)
            active = sum(states.get(name, 0) for name in ("PENDING", "FAILED_RETRYABLE"))
            save(state_path, {
                "状态": "第一二层运行中" if active or not states else "第一二层收尾中",
                "supervisor_pid": os.getpid(), "stage12_pid": watcher.pid, "各状态": states,
            })
            if states and active == 0:
                break
            if watcher.poll() is not None:
                raise RuntimeError(f"第一二层异常退出：{watcher.returncode}")
            time.sleep(10)
        if user_stop.exists():
            save(state_path, {"状态": "已停止", "supervisor_pid": os.getpid()})
            return
        stage12_stop.write_text("stage12 complete", encoding="utf-8")
        watcher.wait(timeout=60)
        subprocess.run(
            [sys.executable, str(ROOT / "scripts" / "run_incremental_stage12.py"), "candidates", "--config", str(args.config)],
            cwd=ROOT, env=environment, check=True, stdout=subprocess.DEVNULL,
        )
        subprocess.run(
            [sys.executable, str(ROOT / "scripts" / "publish_all.py"), "--category", str(args.category)],
            cwd=ROOT, env=environment, check=False, stdout=subprocess.DEVNULL,
        )
        if not candidate.exists():
            raise RuntimeError("第三层候选文件未生成")
        prior_root = Path(r"D:\2026孙宇璠\选品skill\output\shower_curtain")
        prior_results = sorted(prior_root.rglob("pair_results.jsonl"))
        reuse_command = [
            sys.executable, str(ROOT / "scripts" / "import_prior_shower_layer3.py"),
            "--category", str(args.category), "--candidates", str(candidate),
            "--results", str(results), "--report", str(reuse_report),
        ]
        for prior in prior_results:
            reuse_command.extend(["--prior-results", str(prior)])
        with (logs / "prior_layer3_reuse.log").open("ab") as output, (logs / "prior_layer3_reuse_error.log").open("ab") as error:
            subprocess.run(reuse_command, cwd=ROOT, env=environment, check=True, stdout=output, stderr=error)
        for attempt in (1, 2):
            save(state_path, {
                "状态": "第三层运行中", "supervisor_pid": os.getpid(), "尝试轮": attempt,
                "分组大小": 12, "并发": category["stage3"]["max_concurrency"],
            })
            with (logs / "stage3.log").open("ab") as output, (logs / "stage3_error.log").open("ab") as error:
                completed = subprocess.run(
                    [sys.executable, str(ROOT / "scripts" / "run_category_layer3.py"), "--category", str(args.category),
                     "--candidates", str(candidate), "--results", str(results)],
                    cwd=ROOT, env=environment, stdout=output, stderr=error,
                )
            if results.with_name("provider_blocked.json").exists():
                save(state_path, {"状态": "DeepSeek鉴权或余额阻断", "supervisor_pid": os.getpid(), "尝试轮": attempt})
                return
            if completed.returncode:
                raise RuntimeError(f"第三层运行器异常退出：{completed.returncode}")
        subprocess.run(
            [sys.executable, str(ROOT / "scripts" / "publish_all.py"), "--category", str(args.category)],
            cwd=ROOT, env=environment, check=True, stdout=subprocess.DEVNULL,
        )
        save(state_path, {"状态": "已完成", "supervisor_pid": os.getpid(), "分组大小": 12, "结果目录": str(base / "excel")})
    except Exception as exc:
        save(state_path, {"状态": "运行失败", "supervisor_pid": os.getpid(), "原因": f"{type(exc).__name__}: {exc}"})
        raise
    finally:
        if watcher.poll() is None:
            watcher.terminate()
        stage12_stdout.close()
        stage12_stderr.close()


if __name__ == "__main__":
    main()
