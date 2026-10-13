"""Operate the incremental dual-source stage 1/2 pipeline."""
from __future__ import annotations

import argparse
import ctypes
import json
import os
import signal
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BACKGROUND_CREATION_FLAGS = getattr(subprocess, "NORMAL_PRIORITY_CLASS", 0) if os.name == "nt" else 0
sys.path.insert(0, str(ROOT / "src"))

from selection_workflow.incremental_stage12 import IncrementalStage12, PathSpec, SourceSpec, VECTOR_REVISION
from selection_workflow.excel_delivery import data_workbook_path, publish_view_copies


def load_config(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def resolve(path: str) -> Path:
    candidate = Path(path)
    return candidate if candidate.is_absolute() else ROOT / candidate


def wait_for_poll(seconds, stop_file):
    deadline = time.monotonic() + seconds
    while not stop_file.exists():
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        time.sleep(min(0.5, remaining))


class WatchInstance:
    """OS-owned lock: process exit releases it, so stale PID files are safe."""

    def __init__(self, output: Path):
        self.path = output / "stage12_watch.pid"
        self.stream = None

    def acquire(self) -> None:
        stream = self.path.open("a+b")
        try:
            if stream.tell() == 0:
                stream.write(b" ")
                stream.flush()
            stream.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            stream.close()
            print(json.dumps({"状态": "第一二层watch已运行，本次不启动第二实例", "锁": str(self.path)}, ensure_ascii=False))
            raise SystemExit(75)
        stream.seek(0)
        stream.truncate()
        stream.write(str(os.getpid()).encode("ascii"))
        stream.flush()
        self.stream = stream

    def release(self) -> None:
        if self.stream is not None:
            self.stream.close()
            self.stream = None


def process_is_running(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        kernel = ctypes.windll.kernel32
        kernel.OpenProcess.argtypes = [ctypes.c_ulong, ctypes.c_int, ctypes.c_ulong]
        kernel.OpenProcess.restype = ctypes.c_void_p
        kernel.GetExitCodeProcess.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_ulong)]
        kernel.CloseHandle.argtypes = [ctypes.c_void_p]
        handle = kernel.OpenProcess(0x1000, False, pid)
        if not handle:
            return False
        try:
            exit_code = ctypes.c_ulong()
            return bool(kernel.GetExitCodeProcess(handle, ctypes.byref(exit_code))) and exit_code.value == 259  # STILL_ACTIVE
        finally:
            kernel.CloseHandle(handle)
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def excel_lock_path(output: Path) -> Path:
    return output / "excel_export.pid"


def active_excel_export_pid(output: Path) -> int | None:
    lock = excel_lock_path(output)
    if not lock.exists():
        return None
    try:
        pid = int(lock.read_text(encoding="ascii").strip())
    except (OSError, ValueError):
        pid = 0
    if process_is_running(pid):
        return pid
    lock.unlink(missing_ok=True)
    return None


def claim_excel_export(output: Path) -> tuple[bool, int | None]:
    lock = excel_lock_path(output)
    for _ in range(2):
        active = active_excel_export_pid(output)
        if active and active != os.getpid():
            return False, active
        try:
            descriptor = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            continue
        with os.fdopen(descriptor, "w", encoding="ascii") as target:
            target.write(str(os.getpid()))
        return True, os.getpid()
    return False, active_excel_export_pid(output)


def release_excel_export(output: Path) -> None:
    lock = excel_lock_path(output)
    try:
        if int(lock.read_text(encoding="ascii").strip()) == os.getpid():
            lock.unlink(missing_ok=True)
    except (FileNotFoundError, OSError, ValueError):
        pass


def candidate_lock_path(output: Path) -> Path:
    return output / "candidate_export.pid"


def active_candidate_export_pid(output: Path) -> int | None:
    lock = candidate_lock_path(output)
    if not lock.exists():
        return None
    try:
        pid = int(lock.read_text(encoding="ascii").strip())
    except (OSError, ValueError):
        pid = 0
    if process_is_running(pid):
        return pid
    lock.unlink(missing_ok=True)
    return None


def claim_candidate_export(output: Path) -> tuple[bool, int | None]:
    lock = candidate_lock_path(output)
    for _ in range(2):
        active = active_candidate_export_pid(output)
        if active and active != os.getpid():
            return False, active
        try:
            descriptor = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            continue
        with os.fdopen(descriptor, "w", encoding="ascii") as target:
            target.write(str(os.getpid()))
        return True, os.getpid()
    return False, active_candidate_export_pid(output)


def release_candidate_export(output: Path) -> None:
    lock = candidate_lock_path(output)
    try:
        if int(lock.read_text(encoding="ascii").strip()) == os.getpid():
            lock.unlink(missing_ok=True)
    except (FileNotFoundError, OSError, ValueError):
        pass


class StatusOnlyVectorizer:
    """Avoid loading the large local model for a read-only status command."""
    revision = VECTOR_REVISION

    def encode(self, image_path: Path):  # pragma: no cover - defensive guard
        raise RuntimeError("status-only vectorizer cannot encode images")


def validate_category_handoff(config: dict) -> None:
    # Category exports must never silently inherit a legacy shower rule or threshold.
    # Unversioned historical standalone configs retain their established defaults.
    if config.get("schema_version") == "incremental-stage12-category-v1":
        import math
        from selection_workflow.text_gate_registry import GATES
        from selection_automation.rules import read_frozen
        gate = config.get("text_gate")
        rule = config.get("stage12_rule_version") or config.get("rule_version")
        threshold = config.get("candidate_threshold")
        if not isinstance(gate, str) or not gate.strip() or not isinstance(rule, str) or not rule.strip():
            raise ValueError("类别配置不完整：请补全 text_gate（类别规则）和 rule_version（规则版本）。在类别规则配置中修正并重新导出 stage12_config.json 后重试；已有采集数据保留。")
        if type(threshold) not in (int, float) or not math.isfinite(threshold) or not 0 <= threshold <= 100:
            raise ValueError("类别向量阈值未设置或无效：candidate_threshold 必须是 0 到 100 的数值。请在规则/阈值配置中填写并重新导出 stage12_config.json 后重试；不会自动套用其他品类的阈值。")
        if gate.startswith("declarative:"):
            try:
                read_frozen(gate.split(":", 1)[1])
            except (OSError, ValueError) as exc:
                raise ValueError("类别规则无法读取或校验：请检查 text_gate 指向的规则文件，修正保存后重新导出配置。原因：" + str(exc)) from exc
        elif gate not in GATES:
            raise ValueError("类别规则未注册：" + gate + "。请选择本类别已审核的规则，或使用 declarative:规则文件路径，保存后重新导出配置。")


def open_pipeline(config: dict, *, status_only: bool = False, configure: bool = True) -> IncrementalStage12:
    validate_category_handoff(config)
    image_download = config.get("image_download") or {}
    proxy_env = str(image_download.get("temu_proxy_url_env") or "").strip()
    proxy_url = os.environ.get(proxy_env) if proxy_env else None
    pipeline = IncrementalStage12(
        resolve(config["output_dir"]), threshold=float(config.get("candidate_threshold", 50)),
        max_image_retries=int(config.get("max_image_retries", 3)),
        temu_image_proxy_url=proxy_url,
        require_temu_image_proxy=bool(image_download.get("require_temu_proxy", False)),
        image_download_timeout=float(image_download.get("timeout_seconds", 30)),
        image_download_workers=int(image_download.get("workers", 1)),
        vector_batch_size=int(config.get("vector_batch_size", 1)),
        text_gate_workers=int(config.get("text_gate_workers", 1)),
        text_gate_name=str(config.get("text_gate") or "shower_curtain_v2"),
        rule_version=str(config.get("rule_version") or config.get("stage12_rule_version") or "shower-curtain-three-layer-v3.1-module-facts"),
        stage12_rule_version=config.get("stage12_rule_version"),
        vectorizer=StatusOnlyVectorizer() if status_only else None,
    )
    sources = [SourceSpec(
        x["key"], x["platform"], x["adapter"], resolve(x["path"]),
        str(x.get("snapshot_mode") or "upsert").lower(),
    ) for x in config["sources"]]
    paths = [PathSpec(x["key"], x["amazon_source"], x["temu_source"], x["label"]) for x in config["paths"]]
    if configure:
        pipeline.configure(sources, paths)
    cache_seed = str(config.get("cache_seed_database") or "").strip()
    if cache_seed and not status_only:
        pipeline.seed_cache_from_database(resolve(cache_seed))
    return pipeline


def chinese_status(status: dict) -> dict:
    counters = status.get("counters", {})
    state_names = {
        "PENDING": "待处理", "FAILED_RETRYABLE": "失败待重试", "FAILED_BOUNDED": "失败已达重试上限",
        "STAGE1_EXCLUDED": "第一层硬冲突排除", "BELOW_VECTOR_THRESHOLD": "低于向量门槛（未进第三层）",
        "CANDIDATE": "第三层候选",
        "WAIT_IMAGE_PROXY": "等待图片下载代理",
        "WAIT_IMAGE_SOURCE": "等待来源补充图片",
    }
    groups = []
    for g in status["groups"]:
        states = {state_names.get(k, k): v for k, v in g["states"].items()}
        pending = sum(v for k, v in g["states"].items() if k in ("PENDING", "FAILED_RETRYABLE", "WAIT_IMAGE_PROXY", "WAIT_IMAGE_SOURCE"))
        groups.append({
            "组别": g["label"], "Amazon商品数": g["amazon_products"], "TEMU商品数": g["temu_products"],
            "应有商品对": g["expected_pairs"], "已建商品对": g["actual_pairs"],
            "已处理商品对": g["actual_pairs"] - pending, "待处理数量": pending,
            "各状态": states, "第三层候选数": g["candidates"],
        })
    return {
        "数据库": status["database"],
        "数据源": status["sources"],
        "分组": groups,
        "图片下载失败数": status["image_failures"],
        "源数据缺图商品数": status.get("missing_image_products", 0),
        "复用数": {
            "商品对": counters.get("pairs_reused", 0), "图片": counters.get("image_reused", 0),
            "向量": counters.get("vector_reused", 0),
        },
        "新计算向量": counters.get("vectors_computed", 0),
        "已处理商品对": counters.get("pairs_processed", 0),
        "TEMU图片代理": {
            "必须配置": status.get("temu_image_proxy_required", False),
            "已经配置": status.get("temu_image_proxy_configured", False),
            "凭据显示": "不写入日志",
        },
        "TEMU图片下载网络": "电脑本地直连（绕过系统代理）" if status.get("temu_image_network_route") == "LOCAL_DIRECT_WITHOUT_SYSTEM_PROXY" else "独立配置代理",
        "图片缓存模式": status.get("image_cache_mode"),
        "并发与批处理": {
            "本地直连图片下载并发": status.get("image_download_workers"),
            "本地向量批量": status.get("vector_batch_size"),
            "第一层文字并发进程": status.get("text_gate_workers"),
        },
    }


def read_only_status(config: dict) -> dict:
    """Read a consistent status snapshot without loading a model or taking a write lock."""
    database = resolve(config["output_dir"]) / "stage12.sqlite3"
    if not database.exists():
        return {"数据库": str(database), "状态": "尚未初始化"}
    con = sqlite3.connect(f"file:{database.as_posix()}?mode=ro", uri=True, timeout=10)
    con.row_factory = sqlite3.Row
    try:
        con.execute("BEGIN")
        image_download = config.get("image_download") or {}
        status = {
            "database": str(database),
            "sources": [dict(r) for r in con.execute(
                "SELECT source_key,platform,snapshot_mode,received_records,last_success_at,last_error FROM sources ORDER BY source_key"
            )],
            "groups": [],
            "image_failures": con.execute("SELECT count(*) FROM images WHERE status='FAILED'").fetchone()[0],
            "missing_image_products": con.execute("""SELECT count(*) FROM products
                WHERE coalesce(json_extract(record_json,'$.main_image_url'),'')=''
                  AND coalesce(json_extract(record_json,'$.local_image_path'),'')=''""").fetchone()[0],
            "counters": {r[0]: r[1] for r in con.execute("SELECT name,value FROM counters ORDER BY name")},
            "temu_image_proxy_required": bool(image_download.get("require_temu_proxy", False)),
            "temu_image_proxy_configured": False,
            "temu_image_network_route": "LOCAL_DIRECT_WITHOUT_SYSTEM_PROXY",
            "image_cache_mode": "CONTENT_HASH_LOCAL_CACHE",
            "image_download_workers": int(image_download.get("workers", 1)),
            "vector_batch_size": int(config.get("vector_batch_size", 1)),
            "text_gate_workers": int(config.get("text_gate_workers", 1)),
        }
        pair_statuses = [r[0] for r in con.execute("SELECT DISTINCT status FROM pairs")]
        for path in con.execute("SELECT * FROM paths ORDER BY path_key"):
            amazon_n = con.execute("SELECT count(*) FROM products WHERE source_key=?", (path["amazon_source"],)).fetchone()[0]
            temu_n = con.execute("SELECT count(*) FROM products WHERE source_key=?", (path["temu_source"],)).fetchone()[0]
            # idx_pairs_pending starts with status.  Counting one status at a
            # time stays fast on million-row databases; grouping after a
            # path_key filter otherwise forces an expensive table walk.
            states = {
                pair_status: con.execute(
                    "SELECT count(*) FROM pairs WHERE status=? AND path_key=?",
                    (pair_status, path["path_key"]),
                ).fetchone()[0]
                for pair_status in pair_statuses
            }
            status["groups"].append({
                "path_key": path["path_key"], "label": path["label"],
                "amazon_products": amazon_n, "temu_products": temu_n,
                "expected_pairs": amazon_n * temu_n, "actual_pairs": sum(states.values()),
                "states": states, "candidates": con.execute(
                    "SELECT count(*) FROM candidates WHERE path_key=?", (path["path_key"],)
                ).fetchone()[0],
            })
        return chinese_status(status)
    finally:
        con.rollback()
        con.close()
def run_cycle(
    pipeline: IncrementalStage12, config: dict, *,
    materialize: bool = True, include_full_status: bool = True, process: bool = True,
) -> dict:
    ingest = []
    for source in config["sources"]:
        try:
            ingest.append(pipeline.ingest_source(source["key"]))
        except Exception as exc:
            ingest.append({"source": source["key"], "error": str(exc), "retained_previous": True})
    if not process:
        return {"接收": ingest, "处理": {"selected": 0, "mode": "INGEST_ONLY"},
                "候选队列": {"状态": "仅接收快照，不启动分析或导出"}}
    batch = pipeline.process_batch(int(config.get("batch_size", 20)))
    queues = pipeline.materialize_candidates() if materialize else {"状态": "本轮跳过；按配置定时或空闲时更新"}
    result = {"接收": ingest, "处理": batch, "候选队列": queues}
    if include_full_status:
        result["状态"] = chinese_status(pipeline.status())
    return result


def export_excel(pipeline: IncrementalStage12, config: dict) -> list[dict]:
    outputs = []
    for path in config["paths"]:
        snapshot = pipeline.materialize_layer12_export(path["key"])
        summary_path = Path(snapshot["summary"])
        summary_payload = json.loads(summary_path.read_text(encoding="utf-8"))
        summary_payload["below_threshold_action"] = config.get("below_threshold_action", "REVIEW_NOT_FINAL_DIFFERENT")
        summary_path.write_text(json.dumps(summary_payload, ensure_ascii=False, indent=2), encoding="utf-8")
        destination = resolve((config.get("excel") or {}).get("output") or f"{config['output_dir']}/持续同款核验_第一二层.xlsx")
        if len(config["paths"]) > 1:
            destination = destination.with_name(f"{destination.stem}_{path['key']}{destination.suffix}")
        data_destination = data_workbook_path(destination)
        command = [
            sys.executable, str(ROOT / "scripts" / "export_incremental_stage12_excel.py"),
            "--rows", snapshot["rows"], "--summary", snapshot["summary"], "--output", str(data_destination),
        ]
        child_env = os.environ.copy()
        # A redirected Windows child otherwise uses the active ANSI code page,
        # while this parent expects UTF-8.  Make manual and scheduled exports
        # deterministic without changing the user's global console settings.
        child_env["PYTHONIOENCODING"] = "utf-8"
        completed = subprocess.run(
            command, cwd=ROOT, text=True, capture_output=True,
            encoding="utf-8", errors="replace", env=child_env,
        )
        if completed.returncode:
            raise RuntimeError(f"Excel导出失败: {completed.stderr[-1000:]}")
        delivery = publish_view_copies(data_destination, destination)
        if not delivery.get("固定查看副本已更新"):
            # The data workbook and an immutable timestamped snapshot are
            # already safe, but the fixed path is the user's contract.  Make
            # the export fail visibly so the continuous watcher retries after
            # WPS/Excel releases its file lock instead of recording a false
            # successful publication.
            raise RuntimeError(
                "固定Excel查看副本未覆盖；文件可能被WPS/Excel占用："
                f"{destination}；数据源和时间戳快照已生成"
            )
        outputs.append({
            "组别": path["key"], "Excel查看副本": str(destination),
            "Excel数据源": str(data_destination), "商品对": snapshot["row_count"],
            "查看副本发布": delivery,
        })
    return outputs


def main() -> None:
    parser = argparse.ArgumentParser(description="品类化增量第一、二层管线（不调用外部模型）")
    parser.add_argument("command", choices=("init", "once", "reconcile", "watch", "status", "stop", "continue", "compare-json", "excel", "candidates", "snapshot"))
    parser.add_argument("--config", type=Path, default=ROOT / "config" / "incremental_stage12.json")
    parser.add_argument("--max-cycles", type=int)
    parser.add_argument("--no-process", action="store_true", help="只接收快照，不计算向量")
    parser.add_argument("--snapshot-dir", type=Path, help="第三层冻结快照输出目录")
    parser.add_argument("--path-key", help="需要冻结的独立分析路径")
    args = parser.parse_args()
    config = load_config(args.config)
    if args.command not in ("stop", "status"):
        try:
            validate_category_handoff(config)
        except ValueError as exc:
            raise SystemExit(str(exc)) from exc
    output = resolve(config["output_dir"]); output.mkdir(parents=True, exist_ok=True)
    stop_file = output / "STOP"
    if args.command == "stop":
        stop_file.write_text("用户请求停止", encoding="utf-8")
        print(json.dumps({"状态": "已写入停止标记", "文件": str(stop_file)}, ensure_ascii=False)); return
    if args.command == "continue" and stop_file.exists():
        stop_file.unlink()
    if args.command == "status":
        print(json.dumps(read_only_status(config), ensure_ascii=False, indent=2)); return
    watch_instance = WatchInstance(output) if args.command in ('watch','once','init','continue','reconcile') else None
    if watch_instance:
        watch_instance.acquire()
        if args.command == 'watch':
            import psutil
            from selection import save
            save(output / 'stage12_watch_owner.json', {
                'pid': os.getpid(), 'created': psutil.Process().create_time(),
                'config': str(args.config.resolve())})
    pipeline = None
    try:
        pipeline = open_pipeline(
            config,
            status_only=(args.no_process and args.command in ("once", "watch")) or args.command in ("status", "init", "continue", "reconcile", "excel", "candidates", "snapshot"),
            configure=args.command not in ("excel", "candidates", "snapshot"),
        )
        pipeline.stop_requested=lambda:stop_file.exists()
        if watch_instance:pipeline.migrate_image_retries(watch_instance)
        if args.command in ("init", "status", "continue"):
            print(json.dumps(chinese_status(pipeline.status()), ensure_ascii=False, indent=2)); return
        if args.command == "reconcile":
            # Reconcile complete source snapshots without loading the vector
            # model or seeding the large shared cache.  This is intentionally
            # separate from ``once --no-process`` so repair is fast and safe.
            results = []
            for source in config["sources"]:
                try:
                    results.append(pipeline.ingest_source(source["key"]))
                except Exception as exc:
                    results.append({"source": source["key"], "error": str(exc), "retained_previous": True})
            print(json.dumps({"接收": results, "状态": chinese_status(pipeline.status())}, ensure_ascii=False, indent=2)); return
        if args.command == "compare-json":
            path = output / "comparison_snapshot.json"
            path.write_text(json.dumps(pipeline.comparison_rows(), ensure_ascii=False, indent=2), encoding="utf-8")
            print(json.dumps({"状态": "已生成固定快照对照数据", "路径": str(path)}, ensure_ascii=False)); return
        if args.command == "excel":
            claimed, active_pid = claim_excel_export(output)
            if not claimed:
                print(json.dumps({"状态": "已有Excel后台更新正在运行，本次不重复启动", "PID": active_pid}, ensure_ascii=False, indent=2)); return
            try:
                print(json.dumps({"状态": "第一、二层Excel已更新", "输出": export_excel(pipeline, config)}, ensure_ascii=False, indent=2))
            finally:
                release_excel_export(output)
            return
        if args.command == "candidates":
            claimed, active_pid = claim_candidate_export(output)
            if not claimed:
                print(json.dumps({"状态": "已有候选队列后台更新正在运行，本次不重复启动", "PID": active_pid}, ensure_ascii=False, indent=2)); return
            try:
                print(json.dumps({"状态": "第三层候选队列已更新", "输出": pipeline.materialize_candidates()}, ensure_ascii=False, indent=2))
            finally:
                release_candidate_export(output)
            return
        if args.command == "snapshot":
            if not args.snapshot_dir or not args.path_key:
                raise SystemExit("snapshot需要--snapshot-dir和--path-key")
            snapshot = pipeline.materialize_layer3_snapshot(args.path_key, resolve(str(args.snapshot_dir)))
            print(json.dumps({"状态": "第三层一致快照已生成", **snapshot}, ensure_ascii=False, indent=2)); return
        if args.command == "once":
            if args.no_process:
                result = {"接收": [pipeline.ingest_source(x["key"]) for x in config["sources"]], "状态": chinese_status(pipeline.status())}
            else:
                result = run_cycle(pipeline, config)
            print(json.dumps(result, ensure_ascii=False, indent=2)); return
        started = time.monotonic(); active = 0.0; waiting = 0.0; cycles = 0
        excel_config = config.get("excel") or {}
        last_excel_at = 0.0; processed_at_export = -1
        excel_process: subprocess.Popen | None = None
        candidate_interval = max(0.0, float(config.get("candidate_materialize_interval_seconds", 0)))
        candidate_background = bool(config.get("candidate_materialize_background", False))
        last_candidate_at = 0.0
        processed_at_candidate_export = -1
        candidate_process: subprocess.Popen | None = None
        while not stop_file.exists() and (args.max_cycles is None or cycles < args.max_cycles):
            cycle_start = time.monotonic()
            should_materialize = not args.no_process and (candidate_interval == 0 or time.monotonic() - last_candidate_at >= candidate_interval)
            result = run_cycle(
                pipeline, config,
                materialize=should_materialize and not candidate_background,
                include_full_status=False,
                process=not args.no_process,
            )
            counter_row = pipeline.con.execute("SELECT value FROM counters WHERE name='pairs_processed'").fetchone()
            cycle_processed = int(counter_row[0] if counter_row else 0)
            idle_materialize = (
                not args.no_process
                and int((result.get("处理") or {}).get("selected", 0)) == 0
                and cycle_processed > processed_at_candidate_export
            )
            if candidate_background and (should_materialize or idle_materialize):
                if candidate_process is not None and candidate_process.poll() is None:
                    result["候选队列"] = {"状态": "后台更新中，本轮不重复启动", "PID": candidate_process.pid}
                elif active_candidate_export_pid(output):
                    result["候选队列"] = {"状态": "检测到已有后台更新，本轮不重复启动", "PID": active_candidate_export_pid(output)}
                else:
                    child_env = os.environ.copy()
                    child_env.update({"PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1", "PYTHONUNBUFFERED": "1"})
                    stdout_path = output / "candidate_export_stdout.log"
                    stderr_path = output / "candidate_export_stderr.log"
                    with stdout_path.open("ab") as candidate_stdout, stderr_path.open("ab") as candidate_stderr:
                        candidate_process = subprocess.Popen(
                            [sys.executable, str(Path(__file__).resolve()), "candidates", "--config", str(args.config.resolve())],
                            cwd=ROOT, stdout=candidate_stdout, stderr=candidate_stderr, env=child_env,
                            creationflags=BACKGROUND_CREATION_FLAGS,
                        )
                    result["候选队列"] = {"状态": "已启动后台更新", "PID": candidate_process.pid}
                    processed_at_candidate_export = cycle_processed
                last_candidate_at = time.monotonic()
            elif should_materialize:
                processed_at_candidate_export = cycle_processed
                last_candidate_at = time.monotonic()
            elif idle_materialize:
                result["候选队列"] = pipeline.materialize_candidates()
                processed_at_candidate_export = cycle_processed
                last_candidate_at = time.monotonic()
            active += time.monotonic() - cycle_start; cycles += 1
            result["时间"] = {"实际处理秒": round(active, 3), "等待秒": round(waiting, 3), "整体经过秒": round(time.monotonic()-started, 3)}
            current_processed = cycle_processed
            excel_interval = float(excel_config.get("interval_seconds", 900))
            excel_delta = int(excel_config.get("min_processed_delta", 1000))
            scheduled_export = (
                time.monotonic() - last_excel_at >= excel_interval
                and current_processed - processed_at_export >= excel_delta
            )
            # The last batch may be smaller than min_processed_delta.  Once no
            # immediately runnable pair remains, force one final frozen snapshot
            # so the workbook cannot stop short of the database's latest state.
            final_idle_export = (
                int((result.get("处理") or {}).get("selected", 0)) == 0
                and current_processed > processed_at_export
            )
            if not args.no_process and excel_config.get("auto_update") and (scheduled_export or final_idle_export):
                try:
                    export_started = False
                    if excel_config.get("background", False):
                        if excel_process is not None and excel_process.poll() is None:
                            result["Excel"] = {"状态": "后台更新中，本轮不重复启动", "PID": excel_process.pid}
                        elif active_excel_export_pid(output):
                            result["Excel"] = {"状态": "检测到已有后台更新，本轮不重复启动", "PID": active_excel_export_pid(output)}
                        else:
                            stdout_path = output / "excel_export_stdout.log"
                            stderr_path = output / "excel_export_stderr.log"
                            child_env = os.environ.copy()
                            child_env.update({"PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1", "PYTHONUNBUFFERED": "1"})
                            with stdout_path.open("ab") as excel_stdout, stderr_path.open("ab") as excel_stderr:
                                excel_process = subprocess.Popen(
                                    [sys.executable, str(Path(__file__).resolve()), "excel", "--config", str(args.config.resolve())],
                                    cwd=ROOT, stdout=excel_stdout, stderr=excel_stderr, env=child_env,
                                    creationflags=BACKGROUND_CREATION_FLAGS,
                                )
                            result["Excel"] = {"状态": "已启动后台更新", "PID": excel_process.pid, "固定查看文件": str(resolve(excel_config["output"]))}
                            export_started = True
                    else:
                        result["Excel"] = export_excel(pipeline, config)
                        export_started = True
                    if export_started:
                        last_excel_at = time.monotonic(); processed_at_export = current_processed
                except Exception as exc:
                    result["Excel"] = {"状态": "更新失败，分析继续", "原因": str(exc)}
            print(json.dumps(result, ensure_ascii=False), flush=True)
            wait_for = float(config.get("poll_seconds", 8)); wait_start = time.monotonic()
            wait_for_poll(wait_for, stop_file)
            waiting += time.monotonic() - wait_start
    finally:
        if pipeline is not None:
            pipeline.close()
        if watch_instance:
            watch_instance.release()


if __name__ == "__main__":
    main()
