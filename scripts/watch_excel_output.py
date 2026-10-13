"""Single-instance watcher for one of the three continuously published workbooks."""
from __future__ import annotations

import argparse
import ctypes
import hashlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from selection_workflow.excel_delivery import data_workbook_path, publish_view_copies  # noqa: E402


class ExportBusy(RuntimeError):
    pass


class ExportGuard:
    def __init__(self, base: Path, kind: str):
        self.path = base / "excel_watch" / "excel_generation.pid"
        self.kind = kind

    def __enter__(self):
        for _ in range(2):
            if self.path.exists():
                try:
                    value = json.loads(self.path.read_text(encoding="utf-8"))
                    pid = int(value.get("pid") or 0)
                except (OSError, ValueError, json.JSONDecodeError):
                    pid = 0
                if running(pid):
                    raise ExportBusy(f"另一个Excel正在生成: {value.get('kind')}, PID={pid}")
                self.path.unlink(missing_ok=True)
            try:
                descriptor = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            except FileExistsError:
                continue
            with os.fdopen(descriptor, "w", encoding="utf-8") as target:
                json.dump({"pid": os.getpid(), "kind": self.kind}, target, ensure_ascii=False)
            return self
        raise ExportBusy("Excel全局生成锁正被占用")

    def __exit__(self, *_):
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
            if int(value.get("pid") or 0) == os.getpid():
                self.path.unlink(missing_ok=True)
        except (OSError, ValueError, json.JSONDecodeError):
            pass


def running(pid: int) -> bool:
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


def signature(paths: list[Path], optional_paths: list[Path] | None = None) -> str | None:
    if not paths:
        return None
    digest = hashlib.sha256()
    for path, required in [(path, True) for path in paths] + [(path, False) for path in optional_paths or []]:
        digest.update(str(path).encode())
        try:
            stat = path.stat()
        except FileNotFoundError:
            if required:
                return None
            digest.update(b"missing")
            continue
        digest.update(f"{stat.st_size}:{stat.st_mtime_ns}".encode())
    return digest.hexdigest()


def source_signature(kind: str, base: Path, config: Path) -> str | None:
    """Track committed WAL writes as well as the code/config used to export."""
    optional = [ROOT / "config" / "categories" / f"{base.name}.json"]
    dependencies = [Path(__file__), ROOT / "src" / "selection_workflow" / "excel_delivery.py"]
    if kind == "temu":
        required = [base / "temu" / "union" / "products.jsonl", base / "temu" / "union" / "summary.json"]
        dependencies += [ROOT / "scripts" / "export_temu_union_excel.py", ROOT / "scripts" / "validate_temu_union_excel.py"]
    else:
        if not config.exists():
            return None
        database = stage12_database_from_config(config)
        required = [config, database]
        optional.append(Path(str(database) + "-wal"))
        dependencies += [ROOT / "scripts" / "run_incremental_stage12.py"]
        dependencies += sorted((ROOT / "src" / "selection_workflow").glob("*.py"))
        if kind == "layer12":
            dependencies.append(ROOT / "scripts" / "export_incremental_stage12_excel.py")
        else:
            optional.append(base / "stage123" / "results.jsonl")
            optional.append(base / "stage123" / "legacy_paid_cache_v3.json")
            dependencies += [ROOT / "scripts" / "export_incremental_layer123_excel.py", ROOT / "scripts" / "validate_incremental_layer123_excel.py"]
    return signature(required, sorted(set(optional + dependencies)))


def require_fixed_publication(delivery: dict) -> None:
    if not delivery.get("固定查看副本已更新"):
        raise RuntimeError("固定Excel查看副本尚未覆盖，将在下轮重试：" + str(delivery.get("固定查看副本错误") or delivery.get("查看快照错误") or "查看副本未发布"))


def atomic_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + f".{os.getpid()}.tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temporary, path)


def call(command: list[str]) -> str:
    env = os.environ.copy()
    env.update({"PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"})
    completed = subprocess.run(command, cwd=ROOT, text=True, capture_output=True, encoding="utf-8", errors="replace", env=env)
    if completed.returncode:
        raise RuntimeError((completed.stderr or completed.stdout)[-2000:])
    return completed.stdout.strip()


def export(kind: str, base: Path, config: Path) -> dict:
    if kind == "temu":
        source = base / "temu" / "union"
        destination = base / "excel" / "TEMU_US_浴巾套装_关键词采集明细.xlsx"
        data_destination = data_workbook_path(destination)
        call([sys.executable, str(ROOT / "scripts" / "export_temu_union_excel.py"), "--input", str(source), "--output", str(data_destination), "--data-only"])
        call([sys.executable, str(ROOT / "scripts" / "validate_temu_union_excel.py"), "--book", str(data_destination), "--summary", str(source / "summary.json")])
        delivery = publish_view_copies(data_destination, destination, keep_snapshots=3)
        require_fixed_publication(delivery)
        return {"查看文件": str(destination), "发布": delivery}
    if kind == "layer12":
        output = call([sys.executable, str(ROOT / "scripts" / "run_incremental_stage12.py"), "excel", "--config", str(config)])
        exported = json.loads(output).get("输出")
        if not exported:
            raise ExportBusy("第一二层导出已由其他进程接管，稍后确认发布")
        for workbook in exported:
            require_fixed_publication(workbook.get("查看副本发布") or {})
        return {"运行结果": output[-2000:]}

    frozen = base / "stage123" / "frozen"
    frozen.mkdir(parents=True, exist_ok=True)
    call([sys.executable, str(ROOT / "scripts" / "run_incremental_stage12.py"), "snapshot", "--config", str(config), "--snapshot-dir", str(frozen), "--path-key", base.name])
    rows = frozen / f"layer12_rows_{base.name}.jsonl"
    summary = frozen / f"layer12_summary_{base.name}.json"
    candidates = frozen / f"layer3_candidates_{base.name}.jsonl"
    results = base / "stage123" / "results.jsonl"
    destination = base / "excel" / "浴巾套装_第一二三层.xlsx"
    data_destination = data_workbook_path(destination)
    command = [sys.executable, str(ROOT / "scripts" / "export_incremental_layer123_excel.py"), "--layer12-rows", str(rows), "--layer12-summary", str(summary), "--candidates", str(candidates), "--output", str(data_destination)]
    if results.exists():
        command += ["--results", str(results)]
    call(command)
    call([sys.executable, str(ROOT / "scripts" / "validate_incremental_layer123_excel.py"), "--book", str(data_destination), "--layer12-summary", str(summary)])
    delivery = publish_view_copies(data_destination, destination, keep_snapshots=3)
    require_fixed_publication(delivery)
    return {"查看文件": str(destination), "第三层结果文件存在": results.exists(), "发布": delivery}


def stage12_database_from_config(config: Path) -> Path:
    """Resolve the active stage12 DB; do not assume the legacy ``stage12`` dir."""
    payload = json.loads(config.read_text(encoding="utf-8"))
    output_dir = Path(str(payload["output_dir"]))
    if not output_dir.is_absolute():
        output_dir = ROOT / output_dir
    return output_dir / "stage12.sqlite3"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--kind", choices=("temu", "layer12", "layer123"), required=True)
    parser.add_argument("--category-key", default="bath_towel_set")
    parser.add_argument("--interval", type=float, default=60)
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    base = ROOT / "runs" / args.category_key
    config = base / "stage12_config.json"
    state = base / "excel_watch" / f"{args.kind}.json"
    lock = base / "excel_watch" / f"{args.kind}.pid"
    lock.parent.mkdir(parents=True, exist_ok=True)
    if lock.exists():
        try:
            prior = int(lock.read_text(encoding="ascii").strip())
        except (OSError, ValueError):
            prior = 0
        if running(prior):
            print(f"{args.kind} Excel watcher 已运行，PID={prior}")
            raise SystemExit(75)
        lock.unlink(missing_ok=True)
    try:
        descriptor = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        print(f"{args.kind} Excel watcher 正由另一个启动器接管")
        raise SystemExit(75)
    with os.fdopen(descriptor, "w", encoding="ascii") as target:
        target.write(str(os.getpid()))
    last = None
    try:
        while True:
            try:
                current = source_signature(args.kind, base, config)
                source_error = None
            except (OSError, ValueError, KeyError) as error:
                current, source_error = None, str(error)
            payload = {"schema_version": "continuous-excel-watch-v1", "kind": args.kind, "pid": os.getpid(), "updated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "source_signature": current}
            if source_error:
                payload["error"] = source_error
            if current and current != last:
                try:
                    with ExportGuard(base, args.kind):
                        result = export(args.kind, base, config)
                    payload.update({"status": "PUBLISHED", "result": result})
                    last = current
                except Exception as error:
                    payload.update({"status": "EXPORT_FAILED_PREVIOUS_WORKBOOK_PRESERVED", "error": str(error)[:3000]})
            else:
                payload["status"] = "WATCHING_NO_CHANGE" if current else "WAITING_FOR_INPUT"
            atomic_json(state, payload)
            if args.once:
                if payload["status"].startswith("EXPORT_FAILED"):
                    raise SystemExit(payload["error"])
                return
            time.sleep(max(15.0, args.interval))
    finally:
        try:
            if int(lock.read_text(encoding="ascii").strip()) == os.getpid():
                lock.unlink(missing_ok=True)
        except (OSError, ValueError):
            pass


if __name__ == "__main__":
    main()
