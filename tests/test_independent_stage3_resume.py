"""Independent Agent continuation preserves collection stops and paid history."""
from copy import deepcopy
import json
import os
from pathlib import Path
from types import SimpleNamespace
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import web_console as web
import run_codex_visual_pool as pool
from selection_automation import agent_models, local_agents, stage3_credentials
from selection_automation.stage3_progress import snapshot
from selection_automation.stage3_provider import apply, settings
from selection_automation.stage3_runtime import read_control, update_control
from selection_automation.vision import VisionRevisionContext, compatible_vision_keys
from prepare_codex_visual_batch import prepare_batch
from test_prepare_codex_visual_batch import setup
from test_set_inventory_identity import facts
from test_schema_billing_retry import failed
from test_match_criteria import products


class EndWatch(Exception):
    pass


def saved_success(tmp_path, monkeypatch, *, complete=False, mode='local_agent'):
    cat, base, path, pairs = setup(tmp_path, monkeypatch, [{'index': 1, 'asin': 'A1'}])
    apply(cat, settings({'mode': mode, 'workers': 1, 'concurrency_mode': 'manual',
                         **({'agent_ids': ['codex']} if mode == 'local_agent' else {})}))
    path.write_text(json.dumps(cat), encoding='utf-8')
    pair = pairs[0]
    (base / 'stage12' / f'layer3_candidates_{base.name}.jsonl').write_text(
        ''.join(json.dumps(row) + '\n' for row in pairs), encoding='utf-8')
    result = {**pair, 'api_status': 'success', 'state': '待补充证据',
              'vision_revision_key': compatible_vision_keys(cat, pair, context=VisionRevisionContext(cat))[0],
              'visual_facts': facts(cat, 8, 6, complete=False) if complete else {'decision_api': {'same': .62, 'similar': .18, 'different': .20}}}
    folder = base / 'stage123'
    folder.mkdir()
    (folder / 'results.jsonl').write_text(json.dumps(result) + '\n', encoding='utf-8')
    return cat, base, path, pair, result


def agent_console(tmp_path, monkeypatch, base):
    console = web.Console(tmp_path)
    monkeypatch.setattr(console, 'owner', lambda: None)
    monkeypatch.setattr(web, 'check_stage3_local_service', lambda *_: None)
    monkeypatch.setattr(local_agents, 'discover', lambda *_: [{'id': 'codex', 'installed': True}])
    monkeypatch.setattr(local_agents, 'ready_agents', lambda *a, **kw: [{'id': 'codex', 'max_group_size': 12}])
    monkeypatch.setattr(agent_models, 'validate_selection', lambda *_: None)
    monkeypatch.setattr(agent_models, 'validate_efforts', lambda *_: None)
    monkeypatch.setattr(stage3_credentials, 'saved_key', lambda *_: '')
    starts = []
    monkeypatch.setattr(pool, 'ensure_pool', lambda *a, **kw: starts.append(a))
    monkeypatch.setattr(web.subprocess, 'Popen', lambda *a, **kw: pytest.fail('NO_NATIVE_OR_MODEL_LAUNCH'))
    monkeypatch.setattr(console, 'stage3_snapshot', lambda _: snapshot(base, web.selection.read(base / 'category.json'), history=False))
    console.restore_history({'category_key': base.name})
    return console, starts


@pytest.mark.parametrize('mode,complete,expected', [('local_agent', False, 1), ('local_agent', True, 0), ('liquid_d1', False, 0)])
def test_agent_completion_matches_batch_facts_without_reclassifying_api_success(tmp_path, monkeypatch, mode, complete, expected):
    cat, base, path, pair, result = saved_success(tmp_path, monkeypatch, complete=complete, mode=mode)
    before = (base / 'stage123/results.jsonl').read_bytes()
    progress = snapshot(base, cat, history=False)
    assert progress['pending'] == expected
    assert progress['failed'] == 0
    if mode == 'local_agent':
        assert prepare_batch(path, skip_empty=True)['selected'] == expected
    assert (base / 'stage123/results.jsonl').read_bytes() == before
    assert json.loads(before)['api_status'] == 'success'


def test_qualified_saved_facts_win_over_earlier_decision_only_success(tmp_path, monkeypatch):
    cat, base, path, pair, result = saved_success(tmp_path, monkeypatch)
    qualified = {**result, 'visual_facts': facts(cat, 8, 6, complete=False)}
    with (base / 'stage123/results.jsonl').open('a', encoding='utf-8') as stream:
        stream.write(json.dumps(qualified) + '\n')
    assert snapshot(base, cat, history=False)['pending'] == 0
    assert prepare_batch(path, skip_empty=True)['selected'] == 0


@pytest.mark.parametrize('mode', ['multimodal_api', 'local_model'])
def test_decision_checkpoint_cannot_complete_a_requested_full_visual_analysis(tmp_path, monkeypatch, mode):
    cat, base, path, pair, result = saved_success(tmp_path, monkeypatch, mode='liquid_d1')
    before = (base / 'stage123/results.jsonl').read_bytes()
    apply(cat, settings({'mode': mode, 'protocol': 'responses', 'workers': 1,
                         **({'base_url': 'http://127.0.0.1:18181/v1'} if mode == 'local_model' else {})}))
    path.write_text(json.dumps(cat), encoding='utf-8')
    current = snapshot(base, cat, history=False)
    assert current['pending'] == 1 and current['completed'] == 0
    assert result['vision_revision_key'] not in compatible_vision_keys(cat, pair)
    assert (base / 'stage123/results.jsonl').read_bytes() == before
    full = {**result, 'vision_revision_key': compatible_vision_keys(cat, pair)[0],
            'visual_facts': facts(cat, 8, 6, complete=False)}
    with (base / 'stage123/results.jsonl').open('a', encoding='utf-8') as stream:
        stream.write(json.dumps(full) + '\n')
    assert snapshot(base, cat, history=False)['pending'] == 0


def test_valid_codex_unknown_is_already_complete_for_agent(tmp_path, monkeypatch):
    from selection_automation.codex_visual_review import import_reviews
    from test_codex_visual_review import fixture
    cat, base, path, pair, result = saved_success(tmp_path, monkeypatch)
    bound = {**pair, 'vision_revision_key': result['vision_revision_key']}
    _, review = fixture('UNKNOWN')
    review.update(asin=pair['asin'], goods_id=pair['goods_id'], vision_revision_key=bound['vision_revision_key'],
                  visual_facts=facts(cat, 8, 6, complete=False))
    before = (base / 'stage123/results.jsonl').read_bytes()
    import_reviews(cat, {'pairs': [bound]}, [review], base / 'stage123/codex_reviews/complete.jsonl', 'complete')
    current = snapshot(base, cat, history=False)
    assert current['codex_unknown'] == current['completed'] == 1
    assert current['pending'] == current['agent_pending_after_codex'] == 0
    assert prepare_batch(path, skip_empty=True)['selected'] == 0
    assert (base / 'stage123/results.jsonl').read_bytes() == before


@pytest.mark.parametrize('save_first', [False, True])
@pytest.mark.parametrize('complete', [False, True])
def test_save_then_start_and_direct_provider_start_use_same_agent_eligibility(tmp_path, monkeypatch, save_first, complete):
    cat, base, path, pair, result = saved_success(tmp_path, monkeypatch, complete=complete, mode='liquid_d1')
    web.selection.request_task_stop(base)
    console, starts = agent_console(tmp_path, monkeypatch, base)
    provider = settings({'mode': 'local_agent', 'agent_ids': ['codex'], 'workers': 1, 'concurrency_mode': 'manual'})
    if save_first:
        assert console.start_stage3({'provider': provider}, configure_only=True)['ok']
        body = {}
    else:
        body = {'provider': provider}
    if complete:
        with pytest.raises(web.ConsoleError) as error:
            console.start_stage3(body)
        assert error.value.code == 'NO_PENDING_CANDIDATES' and not starts
    else:
        assert console.start_stage3(body)['ok'] and len(starts) == 1
        current = web.selection.read(path)
        assert snapshot(base, current, history=False)['pending'] == 1
    assert (base / 'STOP').exists() and (base / 'stage12/STOP').exists()
    assert (base / 'stage123/results.jsonl').read_text('utf-8') == json.dumps(result) + '\n'


def test_rejected_resume_preserves_pause_and_saved_control_bytes(tmp_path, monkeypatch):
    cat, base, path, pair, result = saved_success(tmp_path, monkeypatch, complete=True)
    console, starts = agent_console(tmp_path, monkeypatch, base)
    update_control(base / 'stage123', paused=True, paused_agents=['claude'])
    before = (base / 'stage123/runtime_control.json').read_bytes()
    with pytest.raises(web.ConsoleError) as error:
        console.control_stage3({'action': 'resume'})
    assert error.value.code == 'NO_PENDING_CANDIDATES'
    assert (base / 'stage123/runtime_control.json').read_bytes() == before
    assert not starts


@pytest.mark.parametrize('blocker', ['unknown_without_authorization', 'unmapped_hold', 'consumed_authorized_success'])
def test_agent_supplement_does_not_bypass_billing_holds_or_consumed_authorization(tmp_path, monkeypatch, blocker):
    cat, base, path, pair, result = saved_success(tmp_path, monkeypatch)
    folder = base / 'stage123'
    key = result['vision_revision_key']
    started = {'request_id': 'TEST_ONLY_ORIGINAL', 'status': 'started', 'pairs': 1, 'vision_revision_keys': [key]}
    if blocker == 'unmapped_hold': started.pop('vision_revision_keys')
    (folder / 'request_intents.jsonl').write_text(json.dumps(started) + '\n', encoding='utf-8')
    if blocker == 'consumed_authorized_success':
        original = {**result, 'api_status': 'failed', 'attempt': 1, 'billing_state': 'UNKNOWN'}
        (folder / 'results.jsonl').write_text(json.dumps(original) + '\n' + json.dumps({**result, 'attempt': 2}) + '\n', encoding='utf-8')
        pool.save(folder / 'billing_retry_authorizations.json', {'version': 1, 'authorizations': [{
            'authorization_id': 'TEST_ONLY_EXPLICIT_ONCE', 'authorized_at': 1, 'max_extra_attempts': 1,
            'scope': [{'original_request_id': started['request_id'], 'asin': pair['asin'], 'goods_id': pair['goods_id'],
                       'vision_revision_key': key, 'baseline_attempt': 1}]}]})
    preserved = {p: p.read_bytes() for p in folder.iterdir() if p.is_file()}
    current = snapshot(base, cat, history=False)
    assert current['pending'] == 1 and current['runnable_pending'] == current['agent_runnable_pending'] == 0
    console, starts = agent_console(tmp_path, monkeypatch, base)
    with pytest.raises(web.ConsoleError) as error:
        console.start_stage3({})
    assert error.value.code == 'BILLING_REVIEW_REQUIRED' and not starts
    assert all(p.read_bytes() == data for p, data in preserved.items())


def test_explicit_continue_of_existing_manager_renews_only_independent_l3_authorization(tmp_path, monkeypatch):
    cat, base, path, pair, result = saved_success(tmp_path, monkeypatch)
    console, starts = agent_console(tmp_path, monkeypatch, base)
    web.selection.request_task_stop(base)
    os.utime(base / 'STOP', ns=(1_000_000_000, 1_000_000_000))
    pool.save(base / 'stage123/paid_enabled.json', {'category_key': base.name, 'enabled_at': .5, 'controller': 'independent'})
    monkeypatch.setattr(web, 'codex_review_control', lambda *_: {'running': True})
    before = (base / 'STOP').read_bytes()
    assert console.start_stage3({})['ok']
    marker = pool.read(base / 'stage123/paid_enabled.json')
    assert marker['enabled_at'] > (base / 'STOP').stat().st_mtime and marker['controller'] == 'independent'
    assert not (base / 'stage123/STOP').exists() and (base / 'STOP').read_bytes() == before
    assert not starts


def test_old_global_stop_allows_new_explicit_independent_agent_only(tmp_path, monkeypatch):
    cat, base, path, pair, result = saved_success(tmp_path, monkeypatch)
    web.selection.request_task_stop(base)
    os.utime(base / 'STOP', ns=(1_000_000_000, 1_000_000_000))
    before = {name: (base / name).read_bytes() for name in ['STOP', 'stage12/STOP']}
    console, starts = agent_console(tmp_path, monkeypatch, base)
    assert console.start_stage3({})['ok']
    ticks = []
    manager = SimpleNamespace(jobs={}, tick=lambda: ticks.append(manager.config['workers']) or {'running': 0})
    monkeypatch.setattr(pool, 'Pool', lambda *a: manager)
    monkeypatch.setattr(pool.time, 'sleep', lambda _: (_ for _ in ()).throw(EndWatch()))
    with pytest.raises(EndWatch):
        pool.watch(path)
    assert ticks == [1]
    assert all((base / name).read_bytes() == data for name, data in before.items())


@pytest.mark.parametrize('blocker', ['new_global_stop', 'stage_stop', 'stale_authorization', 'wrong_task', 'wrong_controller', 'invalid_time', 'missing_authorization'])
def test_stop_and_invalid_independent_authorization_only_drain_inflight(tmp_path, monkeypatch, blocker):
    category = tmp_path / 'category.json'
    category.write_text('{}')
    pool.save(tmp_path / 'stage123/codex_pool_config.json', {'enabled': True, 'workers': 1})
    (tmp_path / 'STOP').write_text('TEST_ONLY_OLD_STOP')
    os.utime(tmp_path / 'STOP', ns=(1_000_000_000, 1_000_000_000))
    marker = {'category_key': tmp_path.name, 'controller': 'independent', 'enabled_at': 2.0}
    if blocker == 'new_global_stop': os.utime(tmp_path / 'STOP', ns=(3_000_000_000, 3_000_000_000))
    elif blocker == 'stage_stop': pool.save(tmp_path / 'stage123/STOP', {'reason': 'TEST_ONLY_STAGE_STOP'})
    elif blocker == 'stale_authorization': marker['enabled_at'] = .5
    elif blocker == 'wrong_task': marker['category_key'] = 'another_task'
    elif blocker == 'wrong_controller': marker['controller'] = 'supervisor'
    elif blocker == 'invalid_time': marker['enabled_at'] = True
    if blocker != 'missing_authorization': pool.save(tmp_path / 'stage123/paid_enabled.json', marker)
    ticks = []
    manager = SimpleNamespace(jobs={'inflight': {'state': 'running'}})
    def settle():
        ticks.append(manager.config['workers'])
        assert manager.config['workers'] == 0
        manager.jobs.clear()
        return {'running': 0, 'saved_inflight': 1}
    manager.tick = settle
    monkeypatch.setattr(pool, 'Pool', lambda *a: manager)
    monkeypatch.setattr(pool.time, 'sleep', lambda _: None)
    pool.watch(category)
    assert ticks == [0]
    assert pool.read(tmp_path / 'stage123/codex_pool/pool.json')['phase'] == '已停止，结果保留'


@pytest.mark.parametrize('late_stop', ['whole_task', 'third_layer', 'pause'])
def test_launch_rechecks_stop_after_image_preparation_before_starting_agent(tmp_path, monkeypatch, late_stop):
    cat, base, path, pair, result = saved_success(tmp_path, monkeypatch)
    monkeypatch.setattr(pool, 'load_category', lambda _: cat)
    worker = pool.Pool(path, {'enabled': True, 'workers': 1, 'group_size': 12})
    original = pool._contact_sheets
    def prepare_then_stop(*args, **kwargs):
        original(*args, **kwargs)
        if late_stop == 'whole_task': web.selection.request_task_stop(base)
        elif late_stop == 'third_layer': pool.save(base / 'stage123/STOP', {'reason': 'TEST_ONLY_STOP'})
        else: update_control(base / 'stage123', paused=True)
    monkeypatch.setattr(pool, '_contact_sheets', prepare_then_stop)
    monkeypatch.setattr(pool, 'codex_executable', lambda *_: 'TEST_ONLY_NEVER_EXECUTED.exe')
    monkeypatch.setattr(pool.subprocess, 'Popen', lambda *a, **kw: pytest.fail('STOP_MUST_PREVENT_AGENT_LAUNCH'))
    with pytest.raises(RuntimeError, match='停止|暂停'):
        worker.launch({'task_id': 'TEST_ONLY_LATE_STOP'}, [{**pair, 'vision_revision_key': result['vision_revision_key']}])
    assert not worker.jobs and not worker.attempts


@pytest.mark.parametrize('action', ['start', 'resume'])
def test_stop_then_explicit_continue_reenables_existing_pool_without_resending_inflight(tmp_path, monkeypatch, action):
    cat, base, path, pair, result = saved_success(tmp_path, monkeypatch)
    web.selection.request_task_stop(base)
    os.utime(base / 'STOP', ns=(1_000_000_000, 1_000_000_000))
    console, starts = agent_console(tmp_path, monkeypatch, base)
    assert console.start_stage3({})['ok']
    manager = base / 'stage123/codex_pool/pool.json'
    pool.save(manager, {'running': 1, 'identities': [[pair['asin'], pair['goods_id'], result['vision_revision_key']]]})
    inflight_before = manager.read_bytes()
    monkeypatch.setattr(web, 'codex_review_control', lambda *a, **kw: {'running': True})
    assert console.stop_stage3({})['ok']
    assert not pool.read(base / 'stage123/codex_pool_config.json')['enabled']
    assert (base / 'stage123/STOP').exists()
    response = console.start_stage3({}) if action == 'start' else console.control_stage3({'action': 'resume'})
    assert response['ok']
    assert pool.read(base / 'stage123/codex_pool_config.json')['enabled']
    assert not (base / 'stage123/STOP').exists() and not read_control(base / 'stage123')['paused']
    assert manager.read_bytes() == inflight_before and len(starts) == 1
    assert (base / 'STOP').exists() and (base / 'stage12/STOP').exists()


def mixed_billing_queue(tmp_path, monkeypatch, blocker, mode='local_agent'):
    cat, base, path, pairs = setup(tmp_path, monkeypatch, [{'index': 1, 'asin': 'A'}, {'index': 2, 'asin': 'A'}])
    apply(cat, settings({'mode': mode, 'workers': 1,
                         **({'agent_ids': ['codex']} if mode == 'local_agent' else {})}))
    path.write_text(json.dumps(cat), encoding='utf-8')
    rows = [{**p, 'vision_revision_key': compatible_vision_keys(cat, p)[0]} for p in pairs]
    folder = base / 'stage123'
    folder.mkdir()
    original = {**rows[0], 'api_status': 'failed', 'attempt': 1, 'billing_state': 'UNKNOWN'}
    started = {'request_id': 'TEST_ONLY_HELD', 'status': 'started', 'pairs': 1,
               'vision_revision_keys': [rows[0]['vision_revision_key']]}
    if blocker == 'unmapped': started.pop('vision_revision_keys')
    (folder / 'request_intents.jsonl').write_text(json.dumps(started) + '\n', encoding='utf-8')
    results = [original]
    if blocker in {'consumed', 'authorized_once'}:
        pool.save(folder / 'billing_retry_authorizations.json', {'version': 1, 'authorizations': [{
            'authorization_id': 'TEST_ONLY_ONCE', 'authorized_at': 1, 'max_extra_attempts': 1,
            'scope': [{'original_request_id': started['request_id'], 'asin': rows[0]['asin'], 'goods_id': rows[0]['goods_id'],
                       'vision_revision_key': rows[0]['vision_revision_key'], 'baseline_attempt': 1}]}]})
        if blocker == 'consumed':results.append({**rows[0], 'api_status': 'success', 'attempt': 2, 'visual_facts': {'decision_api': {}}})
    (folder / 'results.jsonl').write_text(''.join(json.dumps(r) + '\n' for r in results), encoding='utf-8')
    return cat, base, path, rows


@pytest.mark.parametrize('blocker', ['unknown', 'consumed', 'authorized_once', 'unmapped'])
def test_mixed_queue_dispatches_only_nonisolated_pairs(tmp_path, monkeypatch, blocker):
    cat, base, path, rows = mixed_billing_queue(tmp_path, monkeypatch, blocker)
    preserved = {p: p.read_bytes() for p in (base / 'stage123').iterdir() if p.is_file()}
    monkeypatch.setattr(pool, 'load_category', lambda _: cat)
    worker = pool.Pool(path, {'enabled': True, 'workers': 1, 'group_size': 12})
    output = base / 'saved-reviews.jsonl'
    output.write_text('')
    task = {'task_id': 'TEST_ONLY_MIXED', 'rows': rows, 'output': str(output)}
    monkeypatch.setattr(pool, 'tasks', lambda _: iter([task]))
    monkeypatch.setattr(pool, 'prepare_batch', lambda *a, **kw: {'selected': 0})
    monkeypatch.setattr(pool, 'ensure_publisher', lambda *a, **kw: None)
    dispatched = []
    monkeypatch.setattr(worker, 'launch', lambda task, group: dispatched.extend(group) or len(group))
    state = worker.tick()
    assert [r['goods_id'] for r in dispatched] == ([] if blocker == 'unmapped' else [rows[1]['goods_id']])
    assert state['billing_held_pending'] == (2 if blocker == 'unmapped' else 1)
    assert all(p.read_bytes() == data for p, data in preserved.items())


def test_late_unknown_billing_is_rechecked_before_actual_agent_popen(tmp_path, monkeypatch):
    cat, base, path, pair, result = saved_success(tmp_path, monkeypatch)
    monkeypatch.setattr(pool, 'load_category', lambda _: cat)
    worker = pool.Pool(path, {'enabled': True, 'workers': 1, 'group_size': 12})
    original = pool._contact_sheets
    def prepare_then_hold(*args, **kwargs):
        original(*args, **kwargs)
        (base / 'stage123/request_intents.jsonl').write_text(json.dumps({'request_id': 'TEST_ONLY_LATE_HOLD',
            'status': 'started', 'pairs': 1, 'vision_revision_keys': [result['vision_revision_key']]}) + '\n', encoding='utf-8')
    monkeypatch.setattr(pool, '_contact_sheets', prepare_then_hold)
    monkeypatch.setattr(pool, 'codex_executable', lambda *_: 'TEST_ONLY_NEVER_EXECUTED.exe')
    monkeypatch.setattr(pool.subprocess, 'Popen', lambda *a, **kw: pytest.fail('BILLING_HOLD_MUST_PREVENT_LAUNCH'))
    with pytest.raises(RuntimeError, match='费用|授权'):
        worker.launch({'task_id': 'TEST_ONLY_LATE_HOLD'}, [{**pair, 'vision_revision_key': result['vision_revision_key']}])
    assert not worker.jobs and not worker.attempts


def test_exact_schema_api_retry_cannot_be_expanded_or_consumed_by_agent(failed):
    from selection_automation import schema_billing_retry as repair
    f = failed
    authorization = repair.create_authorization(f.folder, f.category, f.candidate)
    preserved = {p: p.read_bytes() for p in f.folder.iterdir() if p.is_file()}
    with pytest.raises(RuntimeError, match='复验授权'):
        pool.agent_billing_blocks(f.folder.parent)
    assert not repair.read_authorization(f.folder)['claimed']
    assert all(p.read_bytes() == data for p, data in preserved.items())


@pytest.mark.parametrize('save_first', [False, True])
def test_schema_retry_is_not_reported_or_started_as_runnable_agent_work(tmp_path, monkeypatch, failed, save_first):
    from selection_automation import schema_billing_retry as repair
    from selection_automation import stage3_progress as progress
    f = failed
    repair.create_authorization(f.folder, f.category, f.candidate)
    base = f.folder.parent
    pool.save(base / 'category.json', f.category)
    monkeypatch.setattr(progress, 'candidates', lambda _: [f.pair])
    before = snapshot(base, f.category, history=False)
    assert before['runnable_pending'] == 1  # The original API still owns its one-shot grant.
    assert before['agent_runnable_pending'] == 0
    preserved = {p: p.read_bytes() for p in f.folder.iterdir() if p.is_file()}
    console, starts = agent_console(tmp_path, monkeypatch, base)
    provider = settings({'mode': 'local_agent', 'agent_ids': ['codex'], 'workers': 1})
    body = {'provider': provider}
    if save_first:
        assert console.start_stage3(body, configure_only=True)['ok']
        assert snapshot(base, web.selection.read(base / 'category.json'), history=False)['agent_runnable_pending'] == 0
        body = {}
    with pytest.raises(web.ConsoleError) as error:
        console.start_stage3(body)
    assert error.value.code == 'BILLING_REVIEW_REQUIRED' and not starts
    assert not repair.read_authorization(f.folder)['claimed']
    assert all(p.read_bytes() == data for p, data in preserved.items())


@pytest.mark.parametrize('save_first', [False, True])
@pytest.mark.parametrize('include_normal', [False, True])
def test_api_retry_pair_is_excluded_from_prospective_agent_but_normal_pair_remains(tmp_path, monkeypatch, save_first, include_normal):
    cat, base, path, rows = mixed_billing_queue(tmp_path, monkeypatch, 'authorized_once', mode='liquid_d1')
    queue = rows if include_normal else rows[:1]
    (base / 'stage12' / f'layer3_candidates_{base.name}.jsonl').write_text(
        ''.join(json.dumps(r) + '\n' for r in queue), encoding='utf-8')
    api = snapshot(base, cat, history=False)
    assert api['runnable_pending'] == len(queue)
    assert api['agent_runnable_pending'] == int(include_normal)
    preserved = {p: p.read_bytes() for p in (base / 'stage123').iterdir() if p.is_file()}
    console, starts = agent_console(tmp_path, monkeypatch, base)
    provider = settings({'mode': 'local_agent', 'agent_ids': ['codex'], 'workers': 1})
    body = {'provider': provider}
    if save_first:
        assert console.start_stage3(body, configure_only=True)['ok']
        body = {}
        current = snapshot(base, web.selection.read(path), history=False)
        assert current['agent_runnable_pending'] == int(include_normal)
    if include_normal:
        assert console.start_stage3(body)['ok'] and len(starts) == 1
    else:
        with pytest.raises(web.ConsoleError) as error:
            console.start_stage3(body)
        assert error.value.code == 'BILLING_REVIEW_REQUIRED' and not starts
    assert all(p.read_bytes() == data for p, data in preserved.items())
