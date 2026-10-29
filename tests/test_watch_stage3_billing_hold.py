import importlib.util
import json
from pathlib import Path

import pytest


@pytest.mark.parametrize('fail_closed', [False, True])
def test_billing_hold_does_not_launch_paid_consumer(tmp_path, monkeypatch, fail_closed):
    spec = importlib.util.spec_from_file_location('watch_billing_test', Path(__file__).parents[1] / 'scripts/watch_stage3.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, 'ROOT', tmp_path)
    monkeypatch.setattr(module, 'ensure_publisher', lambda *a: None)
    folder = tmp_path / 'runs' / 'knife_test' / 'stage123'
    folder.mkdir(parents=True)
    (folder / 'paid_enabled.json').write_text('{}', encoding='utf-8')
    category = tmp_path / 'category.json'
    category.write_text(json.dumps({'category_key': 'knife_test'}), encoding='utf-8')
    monkeypatch.setattr(module, 'snapshot', lambda *a, **kw: {
        'total': 396, 'pending': 396, 'runnable_pending': 0,
        'billing_unknown_pairs': 396, 'billing_hold_fail_closed': fail_closed,
    })
    def reject_paid_launch(*a, **kw):
        raise AssertionError('held-only queue must not launch paid consumer')
    monkeypatch.setattr(module.subprocess, 'run', reject_paid_launch)
    monkeypatch.setattr(module.subprocess, 'Popen', reject_paid_launch)
    module.watch(category)
    state = json.loads((folder / 'controller.json').read_text(encoding='utf-8'))
    assert '核对' in state['phase']
    assert bool(state.get('error')) is fail_closed


def test_web_continue_grants_local_retry_only_on_first_consumer_pass(tmp_path, monkeypatch):
    spec=importlib.util.spec_from_file_location('watch_local_retry_test',Path(__file__).parents[1]/'scripts/watch_stage3.py')
    module=importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
    monkeypatch.setattr(module,'ROOT',tmp_path)
    monkeypatch.setattr(module,'ensure_publisher',lambda *a:None)
    from selection_automation import stage3_provider
    monkeypatch.setattr(stage3_provider,'from_category',lambda category:{'mode':'multimodal_api'})
    folder=tmp_path/'runs'/'knife_test'/'stage123'; folder.mkdir(parents=True)
    (folder/'paid_enabled.json').write_text('{}')
    category=tmp_path/'category.json'; category.write_text(json.dumps({'category_key':'knife_test'}))
    passes=[]
    monkeypatch.setattr(module,'snapshot',lambda *a,**kw:{'total':3,'pending':0 if not passes else 1,'failed':2})
    class Child:
        def wait(self,**kwargs): return 0
    def launch(command,**kwargs):
        passes.append(command)
        return Child()
    monkeypatch.setattr(module.subprocess,'Popen',launch)
    def sleep(seconds):
        if len(passes)==2: (folder/'STOP').touch()
    monkeypatch.setattr(module.time,'sleep',sleep)
    module.watch(category)
    assert len(passes)==2
    assert '--retry-local-errors' in passes[0] and '--retry-local-errors' not in passes[1]
