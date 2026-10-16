"""Exercise only simulated local publication entry points, never real workers."""
import os
from pathlib import Path
import sys
from types import SimpleNamespace

import psutil

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import publish_all
import run_incremental_stage12 as incremental
import watch_stage3_excel as watcher


def test_publication_entry_points_use_normal_windows_priority(tmp_path, monkeypatch):
    priorities = []
    monkeypatch.setattr(psutil, 'Process', lambda: SimpleNamespace(
        nice=priorities.append, create_time=lambda: 123))
    category = tmp_path / 'category.json'
    monkeypatch.setattr(sys, 'argv', ['publish_all', '--category', str(category)])
    monkeypatch.setattr(publish_all, 'publish', lambda _: {'errors': {}})
    publish_all.main()

    monkeypatch.setattr(watcher, 'ROOT', tmp_path)
    monkeypatch.setattr(watcher, 'read', lambda *args: {
        'category_key': 'fixture', 'stage3_excel': 'same'})
    monkeypatch.setattr(watcher, 'save', lambda *args: None)
    monkeypatch.setattr(watcher, 'publish', lambda _: True)
    monkeypatch.setattr(watcher, 'signature', lambda _: 'same')
    monkeypatch.setattr(watcher, 'source_paths', lambda *args: [])
    monkeypatch.setattr(watcher, 'controller_active', lambda _: False)
    monkeypatch.setattr(watcher, 'collection_owner_active', lambda _: False)
    folder = tmp_path / 'runs/fixture/stage123'
    folder.mkdir(parents=True)
    watcher.watch(category)
    expected = psutil.NORMAL_PRIORITY_CLASS if os.name == 'nt' else 10
    assert priorities == [expected, expected]


def test_incremental_background_candidate_and_excel_start_with_normal_flags(tmp_path, monkeypatch):
    config = {'output_dir': str(tmp_path), 'sources': [],
              'candidate_materialize_background': True,
              'poll_seconds': 0,
              'excel': {'auto_update': True, 'background': True, 'output': 'view.xlsx'}}
    monkeypatch.setattr(sys, 'argv', ['incremental', 'watch', '--config', str(tmp_path/'config.json'),
                                     '--max-cycles', '1'])
    monkeypatch.setattr(incremental, 'load_config', lambda _: config)
    monkeypatch.setattr(incremental, 'validate_category_handoff', lambda _: None)
    monkeypatch.setattr(incremental, 'WatchInstance', lambda _: SimpleNamespace(
        acquire=lambda: None, release=lambda: None))
    pipeline = SimpleNamespace(con=SimpleNamespace(execute=lambda _: SimpleNamespace(
        fetchone=lambda: [1])), migrate_image_retries=lambda _: None, close=lambda: None)
    monkeypatch.setattr(incremental, 'open_pipeline', lambda *args, **kwargs: pipeline)
    monkeypatch.setattr(incremental, 'run_cycle', lambda *args, **kwargs: {'处理': {'selected': 0}})
    monkeypatch.setattr(incremental, 'active_candidate_export_pid', lambda _: None)
    monkeypatch.setattr(incremental, 'active_excel_export_pid', lambda _: None)
    calls = []
    def launch(args, **kwargs):
        calls.append((args[2], kwargs['creationflags']))
        return SimpleNamespace(pid=123, poll=lambda: 0)
    monkeypatch.setattr(incremental.subprocess, 'Popen', launch)
    incremental.main()
    expected = incremental.subprocess.NORMAL_PRIORITY_CLASS if os.name == 'nt' else 0
    assert calls == [('candidates', expected), ('excel', expected)]
