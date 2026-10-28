"""Normal resume preserves market inputs; explicit refresh and auth recovery remain live."""
from copy import deepcopy
import json
from pathlib import Path
import sys
from types import SimpleNamespace
import urllib.request

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import collect_market_evidence as market
import watch_temu_union


def snapshot(status='PARTIAL'):
    return {'status': status, 'message': '提供方部分取得，不是全站', 'source': 'GEEKBI',
            'site_id': 48, 'currency': 'USD', 'category_key': 'fixture',
            'queried_at': '2026-10-23T12:11:33+00:00',
            'records': [{'goodsId': '101', 'catIds': [7]}],
            'categories': [{'catId': 7, 'expected': 2, 'retrieved': 1, 'complete': False}],
            'pages': [{'query': {'page': 1}, 'returned': 1, 'total': 2}]}


@pytest.fixture
def task(tmp_path, monkeypatch):
    monkeypatch.setattr(market, 'ROOT', tmp_path)
    monkeypatch.delenv('DEEPSEEK_API_KEY', raising=False)
    monkeypatch.setattr(urllib.request, 'getproxies', lambda: {})
    category = tmp_path / 'runs/fixture/category.json'
    category.parent.mkdir(parents=True)
    category.write_text(json.dumps({'category_key': 'fixture', 'category_name_zh': '测试品类'}), encoding='utf-8')
    folder = category.parent / 'market'
    folder.mkdir()
    calls = []
    auth_state = {'servers': {}}
    def provider(**options):
        calls.append('provider')
        assert options['auth_state_path'] == tmp_path / 'runtime/market-auth/agent-auth.json'
        auth = SimpleNamespace(state_path=tmp_path / 'fixture-auth.json', server_key='fixture-server')
        auth._mutate = lambda mutation: mutation(auth_state)[1]
        return SimpleNamespace(client=SimpleNamespace(auth=auth))
    fresh = snapshot()
    fresh.update(queried_at='2026-10-23T13:13:10+00:00', records=[{'goodsId': '202', 'catIds': [7]}])
    def collect(category, source, progress, **options):
        calls.append('collect')
        assert options['choose_categories'] is None
        return deepcopy(fresh)
    monkeypatch.setattr(market, 'Provider', provider)
    monkeypatch.setattr(market, 'collect', collect)
    monkeypatch.setattr(watch_temu_union, 'publish_once', lambda *args: calls.append('publish'))
    return SimpleNamespace(category=category, folder=folder, calls=calls, fresh=fresh)


def seed(task, cached, state=None):
    path = task.folder / 'geekbi.json'
    path.write_text(json.dumps(cached), encoding='utf-8')
    if state is not None:
        (task.folder / 'status.json').write_text(json.dumps(state), encoding='utf-8')
    return path


@pytest.mark.parametrize('status', ['PARTIAL', 'COMPLETE'])
@pytest.mark.parametrize('missing_status', [False, True])
def test_normal_resume_reuses_nonempty_snapshot_without_requests_or_timestamp_changes(task, status, missing_status):
    cached = snapshot(status)
    summary = {k: v for k, v in cached.items() if k not in ('records', 'pages')}
    path = seed(task, cached, None if missing_status else summary)
    before = path.read_bytes(), path.stat().st_mtime_ns
    assert market.run(task.category) == cached
    assert task.calls == []
    assert (path.read_bytes(), path.stat().st_mtime_ns) == before
    assert json.loads((task.folder / 'status.json').read_text('utf-8')) == summary
    assert not (task.folder / 'auth_context.json').exists()


@pytest.mark.parametrize('state', ['ACTION_REQUIRED', 'AUTH_REQUIRED', 'FAILED', 'ERROR', 'RUNNING', 'CATEGORY_UNRESOLVED'])
def test_pending_gate_or_failed_refresh_is_retried_instead_of_hidden_by_partial_cache(task, state):
    seed(task, snapshot(), {'status': state, 'message': '已有门禁或失败待恢复'})
    assert market.run(task.category) == task.fresh
    assert task.calls == ['provider', 'collect', 'publish']


@pytest.mark.parametrize('change', ['empty', 'bad_record', 'foreign_task', 'foreign_source', 'wrong_site', 'bad_time'])
def test_invalid_or_empty_snapshot_does_not_stop_normal_recovery(task, change):
    cached = snapshot()
    if change == 'empty': cached['records'] = []
    if change == 'bad_record': cached['records'] = [{'goodsId': None}]
    if change == 'foreign_task': cached['category_key'] = 'another-task'
    if change == 'foreign_source': cached['source'] = 'UNVERIFIED'
    if change == 'wrong_site': cached['site_id'] = 1
    if change == 'bad_time': cached['queried_at'] = 'invalid'
    seed(task, cached, {'status': 'PARTIAL'})
    assert market.run(task.category) == task.fresh
    assert task.calls == ['provider', 'collect', 'publish']


def test_explicit_force_refresh_replaces_valid_partial_snapshot(task):
    seed(task, snapshot(), {'status': 'PARTIAL'})
    assert market.run(task.category, force=True) == task.fresh
    assert task.calls == ['provider', 'collect', 'publish']
    assert json.loads((task.folder / 'geekbi.json').read_text('utf-8')) == task.fresh


def test_complete_empty_result_keeps_original_resume_cache_semantics(task):
    cached = snapshot('COMPLETE')
    cached.update(records=[], categories=[{'catId': 7, 'expected': 0, 'retrieved': 0, 'complete': True}],
                  pages=[{'query': {'page': 1}, 'returned': 0, 'total': 0}])
    path = seed(task, cached, {'status': 'COMPLETE'})
    before = path.read_bytes(), path.stat().st_mtime_ns
    assert market.run(task.category) == cached
    assert task.calls == []
    assert (path.read_bytes(), path.stat().st_mtime_ns) == before


def test_force_auth_failure_preserves_snapshot_and_gate_until_real_recovery(task, monkeypatch):
    cached = snapshot()
    path = seed(task, cached, {'status': 'PARTIAL'})
    before = path.read_bytes()
    gate = type('ActionRequired', (Exception,), {'message': '请先完成极鲸云登录',
                'public_payload': lambda self: {'authorization_url': 'https://example.invalid/authorize'}})
    attempts = []
    def collect(*args, **kwargs):
        attempts.append(1)
        if len(attempts) < 3: raise gate()
        return deepcopy(task.fresh)
    monkeypatch.setattr(market, 'collect', collect)
    for force in (True, False):
        assert market.run(task.category, force=force)['status'] == 'ACTION_REQUIRED'
        assert path.read_bytes() == before
        assert json.loads((task.folder / 'status.json').read_text('utf-8'))['status'] == 'ACTION_REQUIRED'
    assert len(attempts) == 2
    assert market.run(task.category) == task.fresh
    assert len(attempts) == 3 and task.calls == ['provider', 'provider', 'provider', 'publish']
