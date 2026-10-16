import importlib.util
import json
import os
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))


def test_commercial_rules_version_is_in_export_source_signature():
    import watch_stage3_excel as publisher
    assert ROOT/'src/selection_automation/commercial.py' in publisher.export_code_paths()


@pytest.fixture
def publisher(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location('stage3_excel_test', ROOT / 'scripts/watch_stage3_excel.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, 'ROOT', tmp_path)
    category = tmp_path / 'category.json'
    category.write_text('{"category_key":"knife_fixture"}', encoding='utf-8')
    base = tmp_path / 'runs/knife_fixture'
    frozen = base / 'stage123/export_frozen'
    frozen.mkdir(parents=True)
    for name in ('layer12_summary_knife_fixture.json', 'layer12_rows_knife_fixture.jsonl',
                 'layer3_candidates_knife_fixture.jsonl'):
        (frozen / name).write_text('{}', encoding='utf-8')
    (base / 'stage123/results.jsonl').write_text('{}\n', encoding='utf-8')
    monkeypatch.setattr(module, 'external_export_running', lambda _: False)
    import export_category_workbooks
    monkeypatch.setattr(module, 'fresh_export', lambda: export_category_workbooks.export)
    return module, category, base


def test_only_layer123_and_preserves_other_deliveries(publisher, monkeypatch):
    module, category, base = publisher
    import export_category_workbooks
    calls = []
    delivery = {'固定查看副本已更新': True, '数据源工作簿': 'data.xlsx', '固定查看副本': 'view.xlsx'}
    monkeypatch.setattr(export_category_workbooks, 'export', lambda path, kind: calls.append(kind) or delivery)
    module.save(base / 'publish_state.json', {'amazon': 'unchanged', 'analysis': 'keep',
        'deliveries': {'temu': {'old': True}}, 'errors': {'commercial': 'existing'}})
    assert module.publish(category)
    assert module.publish(category)
    state = module.read(base / 'publish_state.json')
    assert calls == ['layer123']
    assert state['deliveries']['temu'] == {'old': True}
    assert state['deliveries']['layer123'] == delivery
    assert state['amazon'] == 'unchanged' and state['analysis'] == 'keep'
    assert state['errors']['commercial'] == 'existing'


def test_changes_during_export_remain_dirty(publisher, monkeypatch):
    module, category, base = publisher
    import export_category_workbooks
    calls = []
    def export(path, kind):
        calls.append(kind)
        if len(calls) == 1:
            with (base / 'stage123/results.jsonl').open('a', encoding='utf-8') as stream:
                stream.write('{"new":true}\n')
        return {'固定查看副本已更新': True}
    monkeypatch.setattr(export_category_workbooks, 'export', export)
    module.publish(category)
    assert module.read(base / 'publish_state.json')['stage3_excel'] != module.signature(module.source_paths(category, base))
    module.publish(category)
    assert calls == ['layer123', 'layer123']


def test_existing_manual_export_and_publish_lock_are_respected(publisher, monkeypatch):
    module, category, base = publisher
    monkeypatch.setattr(module, 'external_export_running', lambda _: True)
    assert module.publish(category) is False
    assert not (base / 'publish_state.json').exists()
    monkeypatch.setattr(module, 'external_export_running', lambda _: False)
    with module.ProcessLock(base / 'publish.lock'):
        with pytest.raises(RuntimeError, match='Another supervisor'):
            module.publish(category)


def test_existing_export_matches_exact_category_and_kind(publisher, monkeypatch):
    module, category, _ = publisher
    import psutil
    def fake_process(kind, path=category):
        return SimpleNamespace(info={'pid': 987654,'name':'python.exe'},cmdline=lambda:[
            'python','export_category_workbooks.py','--category',str(path),'--kind',kind])
    monkeypatch.undo()
    monkeypatch.setattr(psutil, 'process_iter', lambda _: [fake_process('layer12')])
    assert not module.external_export_running(category)
    monkeypatch.setattr(psutil, 'process_iter', lambda _: [fake_process('layer123', category.parent / 'other.json')])
    assert not module.external_export_running(category)
    monkeypatch.setattr(psutil, 'process_iter', lambda _: [fake_process('layer123')])
    assert module.external_export_running(category)


def test_external_export_never_reads_browser_command_lines(publisher,monkeypatch):
    module,category,_=publisher
    import psutil
    browser=SimpleNamespace(info={'pid':321,'name':'flowerbrowser.exe'},
        cmdline=lambda:pytest.fail('Do not query unrelated browser processes'))
    monkeypatch.undo()
    monkeypatch.setattr(psutil,'process_iter',lambda fields:[browser] if fields==['pid','name'] else
        pytest.fail('Do not request every process command line'))
    assert not module.external_export_running(category)


def test_publisher_checkpoint_keeps_exact_process_identity(publisher):
    module,_,base=publisher
    path=base/'stage123/excel_publisher.json'
    module.save(path,{'pid':os.getpid(),'created':123,'phase':'old'})
    module.publisher_phase(base,'冻结最新第一二层数据')
    result=module.read(path)
    assert result['pid']==os.getpid() and result['created']==123
    assert result['phase']=='冻结最新第一二层数据' and result['updated_at']>0
    module.save(path,{'pid':os.getpid()+1,'created':456})
    module.publisher_phase(base,'must not replace another owner')
    assert module.read(path)=={'pid':os.getpid()+1,'created':456}


def test_stop_flushes_changes_that_arrive_during_export(publisher, monkeypatch):
    module, category, base = publisher
    import export_category_workbooks
    calls = []
    def export(path, kind):
        calls.append(kind)
        if len(calls) == 1:
            (base / 'stage123/results.jsonl').write_text('{"final":true}\n', encoding='utf-8')
        return {'固定查看副本已更新': True}
    monkeypatch.setattr(export_category_workbooks, 'export', export)
    monkeypatch.setattr(module, 'controller_active', lambda _: False)
    monkeypatch.setattr(module.time, 'sleep', lambda _: None)
    module.watch(category, interval=30)
    assert calls == ['layer123', 'layer123']


def test_collection_keeps_nonpaid_publisher_alive_until_task_finishes(publisher, monkeypatch):
    module, category, base = publisher
    monkeypatch.setattr(module, 'publish', lambda _: True)
    monkeypatch.setattr(module, 'signature', lambda _: 'clean')
    module.save(base/'publish_state.json', {'stage3_excel':'clean'})
    monkeypatch.setattr(module, 'controller_active', lambda _: False)
    checks=iter([True,False]);sleeps=[]
    monkeypatch.setattr(module, 'collection_owner_active', lambda _: next(checks))
    monkeypatch.setattr(module.time, 'sleep', lambda _: sleeps.append(True))
    module.watch(category)
    assert sleeps == [True]


@pytest.mark.parametrize('wrong', [None,'created','category','cwd','command'])
def test_collection_owner_requires_task_and_process_identity(publisher, monkeypatch, wrong):
    import psutil
    module, category, base = publisher
    (module.ROOT/'runtime').mkdir()
    module.save(module.ROOT/'runtime/active.json', {'pid':123,'created':10,
        'category_key':'foreign' if wrong=='category' else base.name})
    process=SimpleNamespace(is_running=lambda:True,status=lambda:'running',
        create_time=lambda:11 if wrong=='created' else 10,
        cwd=lambda:str(module.ROOT/'foreign' if wrong=='cwd' else module.ROOT),
        cmdline=lambda:['python',str(module.ROOT/'scripts/other.py' if wrong=='command'
            else module.ROOT/'scripts/selection.py'),'run','--category',str(base/'category.json')])
    monkeypatch.setattr(psutil,'Process',lambda _:process)
    assert module.collection_owner_active(base) is (wrong is None)


def test_database_only_changes_refresh_freeze(publisher, monkeypatch):
    module, category, base = publisher
    import export_category_workbooks
    output = base / 'stage12'
    output.mkdir()
    db = output / 'stage12.sqlite3'
    db.write_bytes(b'first')
    os.utime(db, ns=(1, 1))
    module.save(base / 'stage12_config.json', {'output_dir': str(output)})
    snapshots = []
    monkeypatch.setattr(module.subprocess, 'run', lambda *a, **kw: snapshots.append(a[0]))
    monkeypatch.setattr(export_category_workbooks, 'export', lambda *a: {'固定查看副本已更新': True})
    module.publish(category)
    assert not snapshots
    (base / 'stage123/results.jsonl').write_text('{"new":true}\n', encoding='utf-8')
    module.publish(category)
    assert not snapshots
    db.write_bytes(b'second')
    module.publish(category)
    assert len(snapshots) == 1
    assert snapshots[0][2] == 'snapshot'


def test_locked_view_is_retried_without_reexport(publisher, monkeypatch):
    module, category, base = publisher
    import export_category_workbooks
    import selection_workflow.excel_delivery
    calls = []
    delivered = {'固定查看副本已更新': False, '时间戳查看快照已生成': True,
                 '数据源工作簿': 'data.xlsx', '固定查看副本': 'view.xlsx'}
    monkeypatch.setattr(export_category_workbooks, 'export', lambda *a: calls.append(a) or delivered)
    monkeypatch.setattr(selection_workflow.excel_delivery, 'retry_fixed_view',
                        lambda *a: {'固定查看副本已更新': True})
    module.publish(category)
    assert 'layer123' in module.read(base / 'publish_state.json')['pending_views']
    module.publish(category)
    assert len(calls) == 1
    assert not module.read(base / 'publish_state.json')['pending_views']


def test_changed_exporter_reloads_cached_module(tmp_path, monkeypatch):
    import importlib
    spec = importlib.util.spec_from_file_location('stage3_reload_test', ROOT / 'scripts/watch_stage3_excel.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    script = tmp_path / 'export_category_workbooks.py'
    script.write_text('def export(*args): return "old"\n', encoding='utf-8')
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.delitem(sys.modules, 'export_category_workbooks', raising=False)
    monkeypatch.setattr(module, 'export_code_paths', lambda: [script])
    cached = importlib.import_module('export_category_workbooks')
    assert module.fresh_export()() == 'old'
    script.write_text('def export(*args): return "new threshold 65"\n', encoding='utf-8')
    assert cached.export() == 'old'
    assert module.fresh_export()() == 'new threshold 65'
    assert module.fresh_export()() == 'new threshold 65'
