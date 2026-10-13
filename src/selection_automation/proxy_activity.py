"""Short, process-owned permission for one worker's TEMU proxy traffic."""
from __future__ import annotations

import json
import os
from pathlib import Path
import threading
import time
import uuid

from .merge_temu_snapshots import atomic_write


def activity_directory(root: Path, worker: int) -> Path:
    if type(worker) is not int or worker not in range(1, 33):
        raise ValueError('worker must be 1..32')
    return Path(root) / 'runtime' / 'proxyshare-chains' / 'activity' / f'worker-{worker}'


def _revoked_at(directory: Path) -> float:
    try:
        return float((directory / 'revoked_at').read_text(encoding='utf-8'))
    except FileNotFoundError:
        return 0
    except (OSError, ValueError):
        return float('inf')


class ProxyActivityLease:
    """Keep traffic enabled only while this collector or verification wait lives."""
    def __init__(self, root: Path, worker: int, purpose: str = 'collector'):
        if purpose not in ('collector', 'manual_verification'):
            raise ValueError('unsupported proxy activity purpose')
        self.runtime = Path(root)/'runtime'
        self.directory = activity_directory(root, worker)
        self.worker = worker
        self.purpose = purpose
        self.path = self.directory / f'{uuid.uuid4().hex}.json'
        self.issued_at = 0.0
        self.stopped = threading.Event()
        self.thread = None

    def _refresh(self) -> bool:
        if _revoked_at(self.directory) >= self.issued_at:
            return False
        atomic_write(self.path, json.dumps({
            'worker': self.worker, 'pid': os.getpid(), 'purpose': self.purpose,
            'issued_at': self.issued_at, 'expires_at': time.time() + 10,
        }))
        return True

    def _renew(self):
        while not self.stopped.wait(2):
            try:
                if not self._refresh():
                    break
            except OSError:
                # Failed refresh expires closed; never extend permission in memory.
                pass

    def __enter__(self):
        from .route_guard import RouteLock
        with RouteLock(self.runtime):
            return self._enter_locked()

    def _enter_locked(self):
        self.issued_at = time.time()
        if not self._refresh():
            raise RuntimeError('proxy activity was revoked during startup')
        self.thread = threading.Thread(target=self._renew, daemon=True)
        self.thread.start()
        return self

    def __exit__(self, *_):
        self.stopped.set()
        if self.thread is not None:
            self.thread.join(timeout=3)
        self.path.unlink(missing_ok=True)


def revoke_worker_activity(root: Path, worker: int) -> None:
    directory = activity_directory(root, worker)
    # Existing refresh threads cannot restore a lease older than this marker.
    atomic_write(directory / 'revoked_at', str(time.time()))
    for path in directory.glob('*.json'):
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass


def revoke_all_activity(root: Path) -> None:
    workers=set(range(1,5))
    for directory in (Path(root)/'runtime/proxyshare-chains/activity').glob('worker-*'):
        try: number=int(directory.name.removeprefix('worker-'))
        except ValueError: continue
        if 1<=number<=32: workers.add(number)
    for worker in sorted(workers):
        revoke_worker_activity(root, worker)
