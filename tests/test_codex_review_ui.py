import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time

import psutil

ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import web_console as web


def test_codex_control_checks_process_identity_and_completion(tmp_path):
    path = tmp_path / 'stage123/codex_review/consumer.json'
    path.parent.mkdir(parents=True)
    row = {'pid': os.getpid(), 'created': psutil.Process().create_time(), 'target': 4879,
           'completed': 397, 'expected_tasks': 8, 'all_reviews_complete': False, 'excel_current': False}
    web.selection.save(path, row)
    state = web.codex_review_control(tmp_path)
    assert state['running'] and state['phase'] == 'Codex看图复核进行中'
    assert state['imported'] == 397 and state['target'] == 4879
    web.selection.save(path, {**row, 'created': row['created'] - 100})
    state = web.codex_review_control(tmp_path)
    assert not state['running'] and '暂停' in state['phase']
    web.selection.save(path, {**row, 'all_reviews_complete': True, 'excel_current': True})
    assert web.codex_review_control(tmp_path)['phase'] == 'Codex看图复核已完成'
    assert web.codex_review_control(tmp_path / 'missing') == {'available': False, 'running': False}


def test_codex_pool_requires_process_script_and_category_and_keeps_counts_separate(tmp_path, monkeypatch):
    base = tmp_path / 'pump_task'
    folder = base / 'stage123/codex_pool'
    folder.mkdir(parents=True)
    created = 12345.0
    pool_script = (ROOT / 'scripts/run_codex_visual_pool.py').resolve()
    category = (base / 'category.json').resolve()
    web.selection.save(folder / 'pool.json', {
        'pid': 9001, 'created': created, 'phase': '并行看图并自动接续下一组',
        'running': 6, 'pending': 1495, 'workers': 6, 'group_size': 12,
        'model': 'gpt-6-luna', 'effort': 'high', 'service_tier': 'default',
        'completed_groups': 41, 'errors': [],
    })
    web.selection.save(base / 'stage123/codex_pool_config.json', {'enabled': True})
    web.selection.save(base / 'stage123/codex_review/consumer.json', {
        'target': 768, 'completed': 696, 'all_reviews_complete': False, 'excel_current': False,
    })

    class FakeProcess:
        def is_running(self): return True
        def status(self): return psutil.STATUS_RUNNING
        def create_time(self): return created
        def cmdline(self): return [str(pool_script), '--category', str(category)]

    monkeypatch.setattr(psutil, 'Process', lambda pid: FakeProcess())
    state = web.codex_review_control(base)
    assert state['running']
    assert state['target'] == 768 and state['imported'] == 696
    assert state['pool']['pending_pairs'] == 1495
    assert state['pool']['running_groups'] == 6
    assert '6路并行' in state['phase'] and '每组12对' in state['phase']

    class WrongTaskProcess(FakeProcess):
        def cmdline(self): return [str(pool_script), '--category', str(tmp_path / 'other' / 'category.json')]

    monkeypatch.setattr(psutil, 'Process', lambda pid: WrongTaskProcess())
    state = web.codex_review_control(base)
    assert not state['running']
    assert not state['pool']['running']


def test_browser_separates_codex_progress_from_stopped_paid_api():
    html = (ROOT / 'web/index.html').read_text(encoding='utf-8')
    code = html[html.index('function stage3Button(data)'):html.index('function excelButtons(data)')]
    script = """
const nodes=new Map();
const $=id=>{if(!nodes.has(id))nodes.set(id,{style:{},setAttribute(k,v){this[k]=v;}});return nodes.get(id);};
let statusFresh=true,busy=false;
CODE
const data={category_key:'fixture',stage3_control:{enabled:false,running:false,can_stop:false,phase:'已停止',progress:{total:10855,completed:5973,pending:4879,percent:55,codex_completed:397,codex_same:0,codex_different:390,codex_unknown:7,pending_after_codex:4482},codex_review:{available:true,running:true,phase:'Codex看图复核进行中',target:4879,expected_tasks:8}}};
stage3Button(data);
if($('stage3-phase').textContent!=='已停止'||!$('stage3-count').textContent.includes('5,973'))throw Error('paid semantics changed');
if($('codex-review-controls').hidden||!$('codex-review-phase').textContent.includes('进行中'))throw Error('Codex hidden while API stopped');
for(const expected of ['397','390','7','4,482'])if(!$('codex-review-count').textContent.includes(expected))throw Error('missing Codex count '+expected);
if(!$('codex-review-target').textContent.includes('8 个累计分片'))throw Error('missing task target');
data.stage3_control.codex_review={available:true,running:false,phase:'Codex看图复核已完成',target:4879,all_reviews_complete:true,excel_current:true};
stage3Button(data);
if(!$('codex-review-phase').textContent.includes('已完成')||!$('codex-review-target').textContent.includes('Excel已更新'))throw Error('missing completion');
""".replace('CODE', code)
    result = subprocess.run(['node', '-e', script], capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stderr


def test_status_keeps_codex_batch_scan_off_request_and_singleflights(tmp_path, monkeypatch):
    import dashboard_progress
    import collection_progress
    from selection_automation import network_readiness, rotation_queue, stage3_progress

    console = web.Console(tmp_path)
    base = tmp_path / 'runs/fixture'
    base.mkdir(parents=True)
    web.selection.save(base / 'category.json', {'category_key': 'fixture', 'execution_mode': 'full'})
    console.owner = lambda: None
    console.current_run_key = lambda: ('fixture', False)
    console.generation_progress = lambda: {}
    console.stage3_snapshot = lambda key: {'total': 1986, 'completed': 491, 'pending': 1495}
    console.excel_files = lambda key=None: {}
    console.frontend_market_snapshot = lambda key: {}
    monkeypatch.setattr(web, 'worker_supervisor_paths', lambda base: [])
    monkeypatch.setattr(dashboard_progress, 'enrich_progress', lambda base, details, **kwargs: details)
    monkeypatch.setattr(collection_progress, 'collection_total', lambda *args, **kwargs: {})
    monkeypatch.setattr(rotation_queue, 'worker_requests', lambda base: {})
    monkeypatch.setattr(stage3_progress, 'controller_alive', lambda base: False)
    monkeypatch.setattr(network_readiness, 'describe', lambda settings: {})

    started = threading.Event()
    release = threading.Event()
    calls = []

    def slow_review(path):
        calls.append(path)
        started.set()
        assert release.wait(3)
        return {'available': True, 'running': True, 'phase': '并行看图', 'target': 10, 'imported': 2}

    monkeypatch.setattr(web, 'codex_review_control', slow_review)
    try:
        started_at = time.monotonic()
        first = console.status()
        assert time.monotonic() - started_at < .5
        assert first['stage3_control']['codex_review']['counting']
        assert started.wait(1)
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=5) as executor:
            rows = list(executor.map(lambda _: console.status(), range(5)))
        assert len(calls) == 1
        assert all(row['stage3_control']['codex_review']['refreshing'] for row in rows)
    finally:
        release.set()
    deadline = time.monotonic() + 2
    while console._codex_review_refreshing and time.monotonic() < deadline:
        time.sleep(.01)
    assert console.status()['stage3_control']['codex_review']['imported'] == 2


def test_codex_snapshot_retains_last_value_and_exposes_refresh_error(tmp_path, monkeypatch):
    console = web.Console(tmp_path)
    monkeypatch.setattr(web, 'codex_review_control', lambda base: {
        'available': True, 'running': True, 'phase': '并行看图', 'imported': 4,
    })
    assert console.codex_review_snapshot('fixture')['counting']
    deadline = time.monotonic() + 2
    while console._codex_review_refreshing and time.monotonic() < deadline:
        time.sleep(.01)
    assert console.codex_review_snapshot('fixture')['imported'] == 4

    monkeypatch.setattr(web, 'codex_review_control', lambda base: (_ for _ in ()).throw(OSError('暂时不可读')))
    console._codex_review_cache['fixture'] = (time.monotonic() - 5, console._codex_review_cache['fixture'][1])
    assert console.codex_review_snapshot('fixture')['refreshing']
    deadline = time.monotonic() + 2
    while console._codex_review_refreshing and time.monotonic() < deadline:
        time.sleep(.01)
    state = console.codex_review_snapshot('fixture')
    assert state['imported'] == 4 and '暂时不可读' in state['refresh_error']
