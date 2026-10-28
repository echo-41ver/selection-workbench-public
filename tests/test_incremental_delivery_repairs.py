import json
import os
from pathlib import Path
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import export_category_workbooks as exporter
import publish_all as publisher
import run_category_layer3 as consumer
from selection_automation.process_lock import ProcessLock
from selection_automation.vision import compatible_vision_keys
from selection_workflow import excel_delivery


def test_publisher_waits_for_other_export_without_false_failure(tmp_path, monkeypatch):
    lock_path = tmp_path / 'publish.lock'
    first = ProcessLock(lock_path)
    first.__enter__()
    waits = []
    def release_first(seconds):
        waits.append(seconds)
        first.__exit__(None, None, None)
    monkeypatch.setattr(publisher.time, 'sleep', release_first)
    with publisher.publish_lock(lock_path):
        assert waits == [.5]
        with pytest.raises(RuntimeError):
            with ProcessLock(lock_path):
                pass
    with ProcessLock(lock_path):
        pass


def test_frozen_candidates_allow_windows_publication_during_network_wait(tmp_path):
    source = tmp_path / 'candidates.jsonl'
    source.write_text('{"goods_id":"old"}\n{"goods_id":"second"}\n', encoding='utf-8')
    replacement = tmp_path / 'next.jsonl'
    replacement.write_text('{"goods_id":"new"}\n', encoding='utf-8')
    with consumer.frozen_candidates(source) as snapshot:
        pending = consumer.rows(snapshot)
        try:
            assert next(pending)['goods_id'] == 'old'
            # Actual os.replace, not mocked: the old implementation fails here
            # on Windows because next(rows(source)) retains the input handle.
            os.replace(replacement, source)
            assert next(pending)['goods_id'] == 'second'
            assert list(consumer.rows(source))[0]['goods_id'] == 'new'
        finally:
            pending.close()
    assert not snapshot.exists()


def candidate_fixture(tmp_path):
    category = tmp_path / 'category.json'
    cat = json.loads((ROOT / 'config/categories/power_drill.json').read_text(encoding='utf-8'))
    category.write_text(json.dumps(cat), encoding='utf-8')
    image = tmp_path / 'image.bin'
    image.write_bytes(b'fixture image identity; not sent to any provider')
    row = {'asin': 'A', 'goods_id': 'T', 'pair_revision': 'fixture',
           'amazon_image_path': str(image), 'temu_image_path': str(image)}
    source = tmp_path / 'candidates.jsonl'
    source.write_text(json.dumps(row) + '\n', encoding='utf-8')
    return cat, category, source, row


def authorize_consumer(tmp_path,monkeypatch,category):
    monkeypatch.setattr(consumer,'ROOT',tmp_path)
    key=json.loads(category.read_text(encoding='utf-8'))['category_key']
    marker=tmp_path/'runs'/key/'stage123/paid_enabled.json'
    marker.parent.mkdir(parents=True,exist_ok=True)
    marker.write_text('{}',encoding='utf-8')


def test_candidate_snapshot_is_cleaned_when_credentials_missing(tmp_path, monkeypatch):
    _, category, source, _ = candidate_fixture(tmp_path)
    args = type('Args', (), dict(category=category, results=tmp_path / 'results.jsonl',
                                 validate_only=False, max_pairs=0, max_requests=0))()
    authorize_consumer(tmp_path,monkeypatch,category)
    monkeypatch.delenv('DEEPSEEK_API_KEY', raising=False)
    with pytest.raises(SystemExit, match='DEEPSEEK_API_KEY'):
        with consumer.frozen_candidates(source) as snapshot:
            consumer.run(args, snapshot)
    assert not snapshot.exists()


@pytest.mark.parametrize('row', [
    {'failure_status': 402}, {'failure_reason': 'APIStatusError: HTTP 402'},
    {'failure_status': 401}, {'failure_status': 403},
])
def test_provider_account_failure_does_not_exhaust_pair_retry_budget(tmp_path, row):
    path = tmp_path / 'results.jsonl'
    path.write_text(json.dumps({'vision_revision_key': 'k', 'api_status': 'failed',
                                'attempt': 2, **row}) + '\n', encoding='utf-8')
    assert consumer.result_index(path)['k']['attempt'] == 1


def test_direct_consumer_refuses_duplicate_paid_owner(tmp_path, monkeypatch):
    _, category, source, _ = candidate_fixture(tmp_path)
    authorize_consumer(tmp_path,monkeypatch,category)
    results = tmp_path / 'results.jsonl'
    monkeypatch.setattr(sys, 'argv', ['consumer', '--category', str(category),
                                    '--candidates', str(source), '--results', str(results)])
    with ProcessLock(tmp_path / 'consumer.lock'):
        with pytest.raises(RuntimeError, match='owns'):
            consumer.main()
    assert not (tmp_path / 'requests.jsonl').exists()


def test_legacy_success_is_reused_without_api_key(tmp_path, monkeypatch):
    cat, category, source, row = candidate_fixture(tmp_path)
    authorize_consumer(tmp_path,monkeypatch,category)
    results = tmp_path / 'results.jsonl'
    legacy = compatible_vision_keys(cat, row)[-1]
    results.write_text(json.dumps({'vision_revision_key': legacy, 'api_status': 'success',
                                   'attempt': 1}) + '\n', encoding='utf-8')
    monkeypatch.delenv('DEEPSEEK_API_KEY', raising=False)
    monkeypatch.setattr(sys, 'argv', ['consumer', '--category', str(category),
                                    '--candidates', str(source), '--results', str(results)])
    consumer.main()
    assert not (tmp_path / 'requests.jsonl').exists()


def test_no_pending_rows_ignore_historical_provider_block(tmp_path, monkeypatch):
    import hashlib
    _, category, source, _ = candidate_fixture(tmp_path)
    authorize_consumer(tmp_path,monkeypatch,category)
    source.write_text('', encoding='utf-8')
    blocked = tmp_path / 'provider_blocked.json'
    blocked.write_text(json.dumps({'status': 402, 'key_identity': hashlib.sha256(b'').hexdigest()}), encoding='utf-8')
    monkeypatch.delenv('DEEPSEEK_API_KEY', raising=False)
    monkeypatch.setattr(sys, 'argv', ['consumer', '--category', str(category),
                                    '--candidates', str(source), '--results', str(tmp_path / 'results.jsonl')])
    consumer.main()
    assert not (tmp_path / 'requests.jsonl').exists()
    assert blocked.exists()


def publisher_fixture(tmp_path, monkeypatch):
    cat, category, _, _ = candidate_fixture(tmp_path)
    base = tmp_path / 'runs' / cat['category_key']
    source = base / 'amazon' / 'amazon_frontend_latest.json'
    source.parent.mkdir(parents=True)
    source.write_text(json.dumps({'records': [{'asin': 'A', 'title': 'fixture'}]}), encoding='utf-8')
    monkeypatch.setattr(publisher, 'ROOT', tmp_path)
    monkeypatch.setattr(exporter, 'ROOT', tmp_path)
    return category, base


def test_open_result_refresh_does_not_export_other_workbooks_or_clear_their_errors(tmp_path, monkeypatch):
    category, base = publisher_fixture(tmp_path, monkeypatch)
    database = base / 'stage12/stage12.sqlite3'
    database.parent.mkdir()
    database.write_bytes(b'offline database fixture')
    (base / 'stage12_config.json').write_text(json.dumps({'output_dir': str(database.parent)}))
    (base / 'temu/union').mkdir(parents=True)
    (base / 'temu/union/products.jsonl').write_text('{}\n')
    publisher.save(base / 'publish_state.json', {'deliveries': {'amazon': {'kept': True}},
                                               'errors': {'temu': 'TEST_ONLY_EXISTING_FAILURE'}})
    from selection_automation import commercial
    monkeypatch.setattr(commercial, 'build_report', lambda *args: {})
    monkeypatch.setattr(publisher.subprocess, 'run', lambda *a, **kw: None)
    exported = []
    monkeypatch.setattr(exporter, 'export', lambda path, kind: exported.append(kind) or {'固定查看副本已更新': True})
    state = publisher.publish(category, kind='layer123')
    assert exported == ['layer123']
    assert state['deliveries']['amazon'] == {'kept': True}
    assert state['errors'] == {'temu': 'TEST_ONLY_EXISTING_FAILURE'}


def test_all_delivery_copies_fail_without_committing_success_and_retry(tmp_path, monkeypatch):
    category, base = publisher_fixture(tmp_path, monkeypatch)
    original = excel_delivery._atomic_copy

    def fail(*args):
        raise PermissionError('fixture all viewing targets unavailable')

    monkeypatch.setattr(excel_delivery, '_atomic_copy', fail)
    state = publisher.publish(category)
    assert state['errors']['amazon']
    assert 'amazon' not in state
    assert not state['deliveries']['amazon']['当前查看产物已交付']
    assert list((base / 'excel').glob('*_data.xlsx'))
    monkeypatch.setattr(excel_delivery, '_atomic_copy', original)
    state = publisher.publish(category)
    assert state['errors'] == {}
    assert state['amazon']
    assert state['deliveries']['amazon']['固定查看副本已更新']


def test_locked_fixed_view_is_retried_after_unlock_without_source_change(tmp_path, monkeypatch):
    category, base = publisher_fixture(tmp_path, monkeypatch)
    original = excel_delivery._atomic_copy

    def locked(source, destination):
        if '_查看快照_' not in destination.name:
            raise PermissionError('fixture Excel has the fixed workbook open')
        original(source, destination)

    monkeypatch.setattr(excel_delivery, '_atomic_copy', locked)
    first = publisher.publish(category)
    assert first['errors'] == {}
    assert 'amazon' in first['pending_views']
    assert not first['deliveries']['amazon']['固定查看副本已更新']
    snapshots = list((base / 'excel').glob('*_查看快照_*.xlsx'))
    assert len(snapshots) == 1
    monkeypatch.setattr(excel_delivery, '_atomic_copy', original)
    monkeypatch.setattr(exporter, 'export', lambda *args: pytest.fail('unchanged source must not be regenerated'))
    second = publisher.publish(category)
    assert second['amazon'] == first['amazon']
    assert second['pending_views'] == {}
    assert second['deliveries']['amazon']['固定查看副本已更新']
    assert list((base / 'excel').glob('*_查看快照_*.xlsx')) == snapshots
    result = second['deliveries']['amazon']
    assert Path(result['固定查看副本']).read_bytes() == Path(result['数据源工作簿']).read_bytes()


def test_snapshot_error_still_attempts_fixed_delivery(tmp_path, monkeypatch):
    source = tmp_path / 'data.xlsx'
    source.write_bytes(b'fixture')
    destination = tmp_path / 'view.xlsx'
    original = excel_delivery._atomic_copy

    def snapshot_failure(source, target):
        if '_查看快照_' in target.name:
            raise OSError('fixture snapshot cannot be created')
        original(source, target)

    monkeypatch.setattr(excel_delivery, '_atomic_copy', snapshot_failure)
    delivered = excel_delivery.publish_view_copies(source, destination)
    assert delivered['当前查看产物已交付']
    assert delivered['固定查看副本已更新']
    assert not delivered['时间戳查看快照已生成']


@pytest.mark.skipif(os.name != 'nt', reason='Actual Windows file-handle lock')
def test_real_windows_view_handle_unlock_republishes(tmp_path, monkeypatch):
    category, base = publisher_fixture(tmp_path, monkeypatch)
    cat = json.loads(category.read_text(encoding='utf-8'))
    view = base / 'excel' / f"Amazon_US_{cat['category_name_zh']}_Best_Sellers.xlsx"
    view.parent.mkdir()
    view.write_bytes(b'old fixed view')
    with view.open('rb'):
        state = publisher.publish(category)
        assert state['pending_views']['amazon']
        assert state['deliveries']['amazon']['时间戳查看快照已生成']
        assert view.read_bytes() == b'old fixed view'
    monkeypatch.setattr(exporter, 'export', lambda *args: pytest.fail('reuse data without regeneration'))
    state = publisher.publish(category)
    assert not state['pending_views']
    assert state['deliveries']['amazon']['固定查看副本已更新']
    assert view.read_bytes() != b'old fixed view'


def test_export_rescores_visual_facts_instead_of_persisted_old_score(tmp_path, monkeypatch):
    from identity_fixtures import identity
    from openpyxl import load_workbook
    cat, category, _, row = candidate_fixture(tmp_path)
    monkeypatch.setattr(exporter, 'ROOT', tmp_path)
    base = tmp_path / 'runs' / cat['category_key']
    frozen = base / 'stage123' / 'export_frozen'
    frozen.mkdir(parents=True)
    key = cat['category_key']
    (frozen / f'layer12_summary_{key}.json').write_text(json.dumps({
        'actual_pairs': 1, 'amazon_products': 1, 'temu_products': 1}), encoding='utf-8')
    (frozen / f'layer3_candidates_{key}.jsonl').write_text(json.dumps(row) + '\n', encoding='utf-8')
    (frozen / f'layer12_rows_{key}.jsonl').write_text(json.dumps({
        **row, 'pair_status': 'CANDIDATE'}) + '\n', encoding='utf-8')
    facts = {'identity': identity(), 'modules': {name: {'relation': 'SAME', 'amazon_evidence': 'same visible body',
                              'temu_evidence': 'same visible body', 'hard_conflict_code': 'NONE'}
                         for name in cat['stage3']['modules']}, 'overall_confidence': .9}
    (base / 'stage123/results.jsonl').write_text(json.dumps({
        **row, 'vision_revision_key': compatible_vision_keys(cat, row)[0],
        'api_status': 'success', 'visual_facts': facts,
        'module_result': {'state': 'different stale result', 'total_score': 0}}) + '\n', encoding='utf-8')
    delivered = exporter.export_snapshot(category, 'layer123')
    book = load_workbook(delivered['固定查看副本'], read_only=True)
    try:
        values = list(book['全量综合结果'].values)
        assert values[4][values[3].index('最终判断')] == '确认同款'
        assert values[4][values[3].index('第三层总分')] == 100
    finally:
        book.close()


@pytest.mark.parametrize('changed', ['category', 'rules', 'ledger', 'intent'])
def test_analysis_republishes_on_rule_configuration_or_usage_change(tmp_path, monkeypatch, changed):
    category, base = publisher_fixture(tmp_path, monkeypatch)
    (base / 'amazon' / 'amazon_frontend_latest.json').unlink()
    db = base / 'stage12' / 'stage12.sqlite3'
    db.parent.mkdir()
    db.write_bytes(b'offline snapshot test: subprocess is mocked')
    (base / 'stage12_config.json').write_text(json.dumps({'output_dir': str(db.parent)}), encoding='utf-8')
    (base/'stage123').mkdir(exist_ok=True)
    (base/'stage123/paid_enabled.json').write_text('{}',encoding='utf-8')
    (base/'stage123/results.jsonl').write_text('{"api_status":"success"}\n',encoding='utf-8')
    calls = []
    monkeypatch.setattr(publisher.subprocess, 'run', lambda *a, **kw: None)
    monkeypatch.setattr(exporter, 'export', lambda category, kind: calls.append(kind) or {'固定查看副本已更新': True})
    first = publisher.publish(category)
    assert first['errors'] == {}
    publisher.publish(category)
    assert calls == ['layer123']
    if changed == 'category':
        cat = json.loads(category.read_text(encoding='utf-8'))
        cat['stage3']['rule_version'] = 'fixture-revised-local-scoring'
        category.write_text(json.dumps(cat), encoding='utf-8')
    else:
        path = (tmp_path / 'src/selection_automation/rules.py' if changed == 'rules'
                else base / ('stage123/request_intents.jsonl' if changed == 'intent' else 'stage123/requests.jsonl'))
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('changed offline fixture', encoding='utf-8')
    publisher.publish(category)
    assert calls == ['layer123', 'layer123']


def test_unpaid_analysis_publishes_current_focused_workbook_without_inventing_verdicts(tmp_path,monkeypatch):
    category,base=publisher_fixture(tmp_path,monkeypatch)
    (base/'amazon/amazon_frontend_latest.json').unlink()
    db=base/'stage12/stage12.sqlite3';db.parent.mkdir();db.write_bytes(b'offline database fixture')
    (base/'stage12_config.json').write_text(json.dumps({'output_dir':str(db.parent)}),encoding='utf-8')
    calls=[]
    monkeypatch.setattr(publisher.subprocess,'run',lambda *a,**kw:None)
    monkeypatch.setattr(exporter,'export',lambda category,kind:calls.append(kind) or {'固定查看副本已更新':True})
    publisher.publish(category)
    assert calls==['layer123']
    (base/'stage123').mkdir(exist_ok=True)
    (base/'stage123/paid_enabled.json').write_text('{}',encoding='utf-8')
    publisher.publish(category)
    assert calls==['layer123','layer123']
    (base/'stage123/results.jsonl').write_text('{"api_status":"success"}\n',encoding='utf-8')
    publisher.publish(category)
    assert calls==['layer123','layer123','layer123']
