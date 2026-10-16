from pathlib import Path
import subprocess
import sys

import pytest

from selection_automation.worker_runtime import WorkerLease, clear_stale_stop_requests, run_collector


def test_collector_ownership_follows_runs_junction_without_claiming_other_workers(tmp_path):
    from selection_automation.worker_runtime import collector_belongs_to_worker
    root = tmp_path / 'install'
    store = tmp_path / 'stored-runs'
    root.mkdir(); store.mkdir()
    if sys.platform == 'win32':
        subprocess.run(['cmd', '/c', 'mklink', '/J', str(root / 'runs'), str(store)],
            check=True, capture_output=True)
    else:
        (root / 'runs').symlink_to(store, target_is_directory=True)
    worker = 'worker-01-new'
    output = root / 'runs/task/temu/workers' / worker / 'query'
    output.mkdir(parents=True)
    args = ['node', str(root / 'scripts/temu_flower_collect.mjs'), '--output', str(output)]
    assert collector_belongs_to_worker(args, root, worker, 'task')
    assert not collector_belongs_to_worker(args, root, 'worker-02-new', 'task')
    args[-1] = str(tmp_path / 'foreign/query')
    assert not collector_belongs_to_worker(args, root, worker, 'task')


def test_new_task_clears_only_stale_worker_stop(tmp_path: Path):
    old = tmp_path / 'worker-04-new.stop.json'
    old.write_text('{"requested_by":0}', encoding='utf-8')
    assert clear_stale_stop_requests(tmp_path, ['worker-01-new', 'worker-04-new']) == ['worker-04-new']
    assert not old.exists()
    with WorkerLease(tmp_path, 'worker-04-new'):
        pass


def test_new_task_does_not_clear_stop_while_worker_is_owned(tmp_path: Path):
    old = tmp_path / 'worker-04-new.stop.json'
    with WorkerLease(tmp_path, 'worker-04-new', purpose='replacement'):
        old.write_text('{"requested_by":0}', encoding='utf-8')
        with pytest.raises(RuntimeError, match='already owned'):
            clear_stale_stop_requests(tmp_path, ['worker-04-new'])
        assert old.exists()


def test_live_collector_receives_health_checks_without_restart(tmp_path):
    observations = []
    with WorkerLease(tmp_path, 'worker-01-new') as lease:
        result = run_collector([sys.executable, '-c', 'import time; time.sleep(1)'],
            lease=lease, cwd=tmp_path, stream=subprocess.DEVNULL,
            health_check=lambda: observations.append(True), health_interval=.1)
    assert result == 0
    assert observations
