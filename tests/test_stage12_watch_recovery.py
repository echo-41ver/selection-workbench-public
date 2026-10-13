import json
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import run_incremental_stage12 as runner
import web_console as web


def test_poll_deadline_expiry_never_sleeps_negative(tmp_path, monkeypatch):
    clock = iter([0, .999999, 1.000001])
    sleeps = []
    monkeypatch.setattr(runner.time, 'monotonic', lambda: next(clock))
    monkeypatch.setattr(runner.time, 'sleep', sleeps.append)
    runner.wait_for_poll(1, tmp_path / 'STOP')
    assert len(sleeps) == 1 and 0 < sleeps[0] < .5


def test_stop_skips_poll_sleep(tmp_path, monkeypatch):
    stop = tmp_path / 'STOP'
    stop.write_text('stop')
    monkeypatch.setattr(runner.time, 'sleep', lambda _: (_ for _ in ()).throw(AssertionError('slept')))
    runner.wait_for_poll(1, stop)


def test_live_task_watcher_clears_only_its_old_failure(tmp_path, monkeypatch):
    import psutil
    base = tmp_path / 'runs/task'; base.mkdir(parents=True)
    output = base / 'stage12'; output.mkdir()
    config = base / 'stage12_config.json'
    config.write_text(json.dumps({'output_dir': str(output)}))
    owner = output / 'stage12_watch_owner.json'
    owner.write_text(json.dumps({'pid': 10, 'created': 42, 'config': str(config)}))
    process = SimpleNamespace(is_running=lambda: True, status=lambda: 'running',
        create_time=lambda: 42, cmdline=lambda: ['python', str(tmp_path / 'scripts/run_incremental_stage12.py'),
                                               'watch', '--config', str(config)])
    monkeypatch.setattr(psutil, 'Process', lambda _: process)
    details = {'状态': '存在阻断', '阻断阶段': ['stage12', 'worker-02-new'],
               '阶段': {'stage12': {'状态': '失败，需要处理', '退出码': 1}}}
    web.reconcile_stage12_watch(tmp_path, base, details)
    assert details['阻断阶段'] == ['worker-02-new'] and details['状态'] == '存在阻断'
    assert '退出码' not in details['阶段']['stage12']
    details['阻断阶段'] = ['stage12']
    web.reconcile_stage12_watch(tmp_path, base, details)
    assert details['状态'] == '运行中'
    process.create_time = lambda: 43
    details = {'状态': '存在阻断', '阻断阶段': ['stage12']}
    web.reconcile_stage12_watch(tmp_path, base, details)
    assert details['状态'] == '存在阻断'
