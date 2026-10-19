"""Offline operation-path regressions; no accounts, providers or live collection."""
import copy
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import mcp_server
import selection
from selection_automation.rules import validate, score
from selection_automation.vision import vision_revision, compatible_vision_keys
from identity_fixtures import identity

CAT = json.loads((ROOT / 'config/categories/power_drill.json').read_text(encoding='utf-8'))


@pytest.mark.parametrize('value', ['false', 'true', 0, 1, [], {}])
def test_mcp_rejects_nonboolean_paid_before_launch(monkeypatch, value):
    monkeypatch.setattr(mcp_server.subprocess, 'run', lambda *a, **k: pytest.fail('must reject before launch'))
    with pytest.raises(ValueError, match='paid must be boolean'):
        mcp_server.execute('selection_start', {'category_key': 'fixture', 'paid': value})


@pytest.mark.parametrize('workers', [[], [True], [1, 1], ['1'], [0], [33]])
def test_mcp_rejects_invalid_rotation_workers(workers):
    with pytest.raises(ValueError):
        mcp_server.execute('selection_rotate', {'category_key': 'fixture', 'workers': workers})


@pytest.mark.parametrize('path', ['C:fixture', r'\fixture', r'C:\fixture', '../outside', r'..\outside', r'\\server\share\outside'])
def test_windows_output_is_contained(path):
    cat = copy.deepcopy(CAT)
    cat['stage12_output_dir'] = path
    with pytest.raises(ValueError, match='inside this task'):
        validate(cat)


def visual_facts(cat, relation):
    return {'overall_confidence': .95, 'identity': identity(relation), 'modules': {
        name: {'relation': relation, 'amazon_evidence': 'clear identity', 'temu_evidence': 'clear identity'}
        for name in cat['stage3']['modules']}}


def test_similar_cannot_be_strict_exclusion_and_deleted_variant_is_ignored():
    towel = json.loads((ROOT / 'config/categories/bath_towel_set.json').read_text(encoding='utf-8'))
    assert towel['stage3']['different_score_ceiling'] == 69
    assert score(towel, visual_facts(towel, 'SIMILAR'))['state'] == '待复核'
    assert score(towel, visual_facts(towel, 'DIFFERENT'))['state'] == '不同款'
    facts = visual_facts(towel, 'SAME')
    facts['variant_differences'] = ['removed_gsm_module']
    assert score(towel, facts)['state'] == '确认同款'


def test_local_scoring_and_text_revision_do_not_rebill_images(tmp_path):
    image = tmp_path / 'image'; image.write_bytes(b'fixture-image')
    pair = {'asin': 'A', 'goods_id': 'T', 'pair_revision': 'v1',
            'amazon_image_path': str(image), 'temu_image_path': str(image)}
    before = vision_revision(CAT, pair)
    changed = copy.deepcopy(CAT)
    changed['stage3']['different_score_ceiling'] -= 1
    changed['stage3']['same_threshold'] += 1
    pair['pair_revision'] = 'updated-title-price-only'
    assert vision_revision(changed, pair) == before
    changed['stage3']['module_descriptions']['body'] += ' updated visual question'
    assert vision_revision(changed, pair) != before
    assert len(compatible_vision_keys(CAT, pair)) == 2


def test_verification_javascript_recognizes_real_temu_hostname(tmp_path, monkeypatch):
    captured = {}
    monkeypatch.setattr(selection, 'ROOT', tmp_path)
    (tmp_path / 'sid').write_text('fixture')
    class Reply:
        def __enter__(self): return self
        def __exit__(self, *a): pass
        def read(self): return b'{"value":true}'
    class Opener:
        def open(self, request, **kwargs):
            captured.update(json.loads(request.data)); return Reply()
    import urllib.request
    monkeypatch.setattr(urllib.request, 'build_opener', lambda *a: Opener())
    assert selection.ready_after_verification({'session_id_file':'sid','webdriver_url':'http://127.0.0.1:1'},'power drill')
    script = captured['script']
    stub = """global.location={href:'https://www.temu.com/search_result.html?search_key=power%20drill'};
global.document={title:'Search',body:{innerText:''},querySelectorAll:s=>s.includes('data-goods-id')?[{getBoundingClientRect:()=>({width:1,height:1})}]:[]};
global.getComputedStyle=()=>({display:'block',visibility:'visible'});
"""
    result = subprocess.run(['node','-e',stub+'console.log(new Function('+json.dumps(script)+')("power drill"))'], capture_output=True,text=True)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == 'true'


def test_provider_block_does_not_spend_retry_budget_and_recovers(tmp_path, monkeypatch):
    monkeypatch.setenv('DEEPSEEK_API_KEY', 'offline-fixture')
    marker = tmp_path / 'stage123/provider_blocked.json'
    selection.save(marker, {'status':402,'key_identity':hashlib.sha256(b'offline-fixture').hexdigest()})
    state = {}; children = {'stage3':SimpleNamespace(poll=lambda:1)}
    finished = {'stage3'}; attempts = {'stage3':3}; next_at = {}
    kwargs = dict(children=children,finished=finished,attempts=attempts,next_at=next_at,state=state)
    for _ in range(4):
        assert selection.hold_provider_block(tmp_path, **kwargs)
    assert not children and not finished and attempts['stage3'] == 0
    marker.unlink()
    assert not selection.hold_provider_block(tmp_path, **kwargs)
    assert next_at['stage3'] == 0


def test_start_reports_starting_and_stop_is_not_discarded(tmp_path, monkeypatch):
    monkeypatch.setattr(selection, 'ROOT', tmp_path)
    monkeypatch.setattr(selection, 'os', SimpleNamespace(name='posix'))
    monkeypatch.setattr(selection, 'spawn', lambda *a, **k: SimpleNamespace(pid=os.getpid(),poll=lambda:None))
    base = tmp_path/'runs'/CAT['category_key']; base.mkdir(parents=True)
    (base/'STOP').write_text('previous stop')
    result = selection.request_start(base/'category.json', CAT, base, False)
    assert result['状态'] == '启动中' and not (base/'STOP').exists()
    (base/'STOP').write_text('new stop after spawn')
    assert selection.request_start(base/'category.json', CAT, base, False)['状态'] == '停止中'
    assert (base/'STOP').read_text() == 'new stop after spawn'


def test_already_exited_start_reports_failure(tmp_path, monkeypatch):
    monkeypatch.setattr(selection, 'ROOT', tmp_path)
    monkeypatch.setattr(selection, 'os', SimpleNamespace(name='posix'))
    monkeypatch.setattr(selection, 'spawn', lambda *a, **k: SimpleNamespace(pid=999999,poll=lambda:1,returncode=1))
    result = selection.request_start(tmp_path/'category.json', CAT, tmp_path/'runs'/CAT['category_key'], False)
    assert result['状态'] == '启动失败'


@pytest.mark.parametrize('exit_point', ['process', 'create_time', 'after_registration'])
def test_start_exit_race_clears_starting_state(tmp_path, monkeypatch, exit_point):
    import psutil
    monkeypatch.setattr(selection, 'ROOT', tmp_path)
    monkeypatch.setattr(selection, 'os', SimpleNamespace(name='posix'))
    monkeypatch.setattr(selection, 'live_owner', lambda: None)
    checks = iter([None, 1]) if exit_point == 'after_registration' else None
    child = SimpleNamespace(pid=123456,returncode=1,poll=lambda:next(checks) if checks else None)
    monkeypatch.setattr(selection, 'spawn', lambda *a, **k: child)

    def disappeared():
        raise psutil.NoSuchProcess(child.pid)

    def process(pid):
        if exit_point == 'process':
            disappeared()
        return SimpleNamespace(pid=pid,create_time=disappeared if exit_point == 'create_time' else lambda:123.0)

    monkeypatch.setattr(psutil, 'Process', process)
    base = tmp_path / 'runs' / CAT['category_key']
    result = selection.request_start(base/'category.json', CAT, base, False)
    assert result['状态'] == '启动失败'
    assert selection.read(base/'status.json')['状态'] == '启动失败'
    assert not (tmp_path/'runtime/starting.json').exists()


def test_stop_before_supervisor_start_prevents_provisioning(tmp_path, monkeypatch):
    import company_setup
    monkeypatch.setattr(selection, 'ROOT', tmp_path)
    monkeypatch.setattr(company_setup, 'ensure_company', lambda *a, **k: pytest.fail('STOP must prevent provisioning'))
    base = tmp_path/'runs'/CAT['category_key']; base.mkdir(parents=True)
    path = base/'category.json'; path.write_text(json.dumps(CAT),encoding='utf-8')
    (base/'STOP').write_text('requested after spawn')
    selection.supervise(path, False)
    assert selection.read(base/'status.json')['状态'] == '已停止'
    assert not (tmp_path/'runtime/active.json').exists()


def test_rotation_uses_actual_capacity_not_hardcoded_four():
    manifest={'workers':[{}, {}, {}]}
    assert selection.selected_workers(manifest) == [1, 2, 3]
    assert selection.selected_workers(manifest,'2,3') == [2, 3]
    for value in ('4', '0', '2,2', ''):
        with pytest.raises(ValueError): selection.selected_workers(manifest,value)
    with pytest.raises(ValueError): selection.selected_workers({})
