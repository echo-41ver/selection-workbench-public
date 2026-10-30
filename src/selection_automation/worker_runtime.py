"""Per-worker OS leases and cooperative supervisor handoff (no process-name killing)."""
from __future__ import annotations

import json
import os
import re
import subprocess
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

from .merge_temu_snapshots import atomic_write


class WorkerStopRequested(RuntimeError):
    pass


class WorkerLease:
    def __init__(self, directory: Path, worker: str, *, purpose: str = "collector", timeout: float = 0, task_stop_path: Path | None = None):
        if not re.fullmatch(r"[A-Za-z0-9_.-]+", worker):
            raise ValueError("invalid worker name")
        self.directory, self.worker = directory, worker
        self.purpose, self.timeout = purpose, timeout
        self.task_stop_path = task_stop_path
        self.token = uuid.uuid4().hex
        self.handle = None
        self.owner_path = directory / f"{worker}.owner.json"
        self.stop_path = directory / f"{worker}.stop.json"

    def __enter__(self):
        from .route_guard import RouteLock
        deadline = time.monotonic() + self.timeout
        while True:
            try:
                # Admission is atomic with route changes, but waiting for a
                # busy worker must never retain the shared route lock.
                with RouteLock(self.directory.parent):
                    return self._enter_locked()
            except RuntimeError as exc:
                if str(exc) != f"worker already owned: {self.worker}" or time.monotonic() >= deadline:
                    raise
            time.sleep(0.1)

    def _enter_locked(self):
        self.directory.mkdir(parents=True, exist_ok=True)
        self.handle = (self.directory / f"{self.worker}.lock").open("a+b")
        self.handle.seek(0, os.SEEK_END)
        if self.handle.tell() == 0:
            self.handle.write(b"\0"); self.handle.flush()
        deadline = time.monotonic()
        while True:
            self.handle.seek(0)
            try:
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(self.handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(self.handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                if time.monotonic() >= deadline:
                    self.handle.close(); self.handle = None
                    raise RuntimeError(f"worker already owned: {self.worker}")
                time.sleep(0.1)
        try:
            if self.purpose == "collector":
                self.check_stop()
            atomic_write(self.owner_path, json.dumps({
                "pid": os.getpid(), "worker": self.worker, "token": self.token, "purpose": self.purpose,
            }))
            return self
        except BaseException:
            self.__exit__(None, None, None)
            raise

    def check_stop(self):
        if self.task_stop_path is not None and self.task_stop_path.exists():
            raise WorkerStopRequested(f"task stopped for {self.worker}")
        if self.stop_path.exists():
            raise WorkerStopRequested(f"environment replacement requested for {self.worker}")

    def __exit__(self, *_):
        if self.handle is None:
            return
        try:
            owner = json.loads(self.owner_path.read_text("utf-8"))
            if owner.get("token") == self.token:
                self.owner_path.unlink(missing_ok=True)
        except (OSError, ValueError):
            pass
        self.handle.seek(0)
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(self.handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            fcntl.flock(self.handle, fcntl.LOCK_UN)
        self.handle.close(); self.handle = None


def clear_stale_stop_requests(directory: Path, workers: list[str]) -> list[str]:
    """Clear stop requests left by a previous task, only with the OS lease held."""
    cleared = []
    for worker in workers:
        stop_path = directory / f"{worker}.stop.json"
        if not stop_path.exists():
            continue
        with WorkerLease(directory, worker, purpose="replacement"):
            if stop_path.exists():
                stop_path.unlink()
                cleared.append(worker)
    return cleared


def run_collector(command: list[str], *, lease: WorkerLease, cwd: Path, stream,
                  health_check=None, health_interval: float = 10) -> int:
    """Only stop the child owned by this supervisor; other workers stay alive."""
    lease.check_stop()
    child = subprocess.Popen(command, cwd=cwd, stdout=stream, stderr=subprocess.STDOUT, creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
    next_health = time.monotonic() + health_interval
    try:
        while child.poll() is None:
            lease.check_stop()
            if health_check is not None and time.monotonic() >= next_health:
                health_check()
                next_health = time.monotonic() + health_interval
            time.sleep(0.25)
        lease.check_stop()
        return child.returncode
    finally:
        # Windows terminate() bypasses Node signal handlers. Let the collector
        # observe its owned stop file and release BiDi interception first.
        if child.poll() is None and "--stop-file" in command:
            index = command.index("--stop-file")
            if index + 1 < len(command) and Path(command[index + 1]).resolve() == lease.stop_path.resolve() and lease.stop_path.exists():
                try:
                    child.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    pass
        if child.poll() is None:
            child.terminate()
            try:
                child.wait(timeout=10)
            except subprocess.TimeoutExpired:
                child.kill(); child.wait()


def interruptible_wait(seconds: float, lease: WorkerLease):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        lease.check_stop()
        time.sleep(min(0.25, max(0, end - time.monotonic())))


def transport_recovery_allowed(progress: dict) -> bool:
    if progress.get('manual_action_required') is True or progress.get('automatic_retry') is False:
        return False
    # Bootstrap and steady-state collectors report different transport statuses.
    if progress.get('http_block') or progress.get('http_status') in (403, 429, '403', '429'):
        return False
    if progress.get('status') == 'STOPPED_PROXY_OR_NETWORK_FAILURE':
        return True
    return (progress.get("status") in {"STOPPED_PROXY_CONNECTION", "STOPPED_BOOTSTRAP_NAVIGATION"}
            and progress.get("automatic_recovery_eligible") is True
            and progress.get("resume_requires_session_rotation") is True)



@contextmanager
def replacement_guard(directory: Path, worker: str, supervisor_token: str | None = None):
    if supervisor_token:
        owner = json.loads((directory / f"{worker}.owner.json").read_text("utf-8"))
        # Windows venv launchers can insert an intermediate Python process.
        import psutil
        ancestor_ids = {process.pid for process in psutil.Process().parents()}
        if (owner.get("pid") not in ancestor_ids or owner.get("token") != supervisor_token
                or owner.get("worker") != worker or owner.get("purpose") != "collector"):
            raise RuntimeError("replacement must be invoked by the owning supervisor")
        # Parent synchronously waits for us while retaining its OS lease.
        yield
        return
    with WorkerLease(directory, f"{worker}.replacement", purpose="replacement"):
        stop_path = directory / f"{worker}.stop.json"
        atomic_write(stop_path, json.dumps({"requested_by": os.getpid(), "at": time.time()}))
        try:
            with WorkerLease(directory, worker, purpose="replacement", timeout=300):
                yield
        finally:
            stop_path.unlink(missing_ok=True)


def collector_belongs_to_worker(args, root, worker, task_key):
    """Fail closed unless Node's actual entry and exact task/worker output match."""
    root = Path(root).resolve()
    try:
        if worker not in {f'worker-{i:02d}-new' for i in range(1, 33)}:
            return False
        if not isinstance(task_key, str) or not task_key or Path(task_key).name != task_key or task_key in {'.', '..'}:
            return False
        if len(args) < 4 or Path(args[0]).name.lower() not in {'node', 'node.exe'}:
            return False
        # Our launcher uses node SCRIPT. Options/other entrypoints cannot be
        # treated as collectors merely because a later argument names one.
        if Path(args[1]).resolve() != root / 'scripts/temu_flower_collect.mjs':
            return False
        if args.count('--output') != 1:
            return False
        output = Path(args[args.index('--output') + 1]).resolve()
        worker_output = (root / 'runs' / task_key / 'temu' / 'workers' / worker).resolve()
        parts = output.relative_to(worker_output).parts
        return len(parts) == 1 and bool(parts[0])
    except (ValueError, IndexError, TypeError, OSError):
        return False


def stop_owned_collectors(root, worker, task_key, timeout=5):
    """Stop exact owned collectors with psutil PID/create-time checks, never a shell name scan."""
    import psutil
    owned = []
    for process in psutil.process_iter(['name', 'cmdline']):
        try:
            if (process.info.get('name') or '').lower() not in {'node', 'node.exe'}:
                continue
            if not collector_belongs_to_worker(process.info['cmdline'] or [], root, worker, task_key):
                continue
            process.terminate()
            owned.append(process)
        except psutil.NoSuchProcess:
            continue
        except psutil.AccessDenied:
            raise RuntimeError('OWNED_COLLECTOR_STOP_ACCESS_DENIED') from None
    _, alive = psutil.wait_procs(owned, timeout=timeout)
    if alive:
        raise RuntimeError('OWNED_COLLECTOR_STOP_TIMEOUT')
    return {'stopped_owned_collectors': len(owned)}
