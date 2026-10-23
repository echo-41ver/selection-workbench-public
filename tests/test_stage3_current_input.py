"""Current input, rather than a long-lived private iterator, authorizes dispatch."""
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import sys
import threading
from types import SimpleNamespace

import openai
import pytest

from selection_workflow.incremental_stage12 import normalize_record

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import run_category_layer3 as runner


def write(path, values):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(''.join(json.dumps(value) + '\n' for value in values), encoding='utf-8')


@pytest.fixture
def task(tmp_path, monkeypatch):
    base = tmp_path / 'runs/fixture'
    amazon = base / 'amazon.jsonl'
    temu = base / 'temu.jsonl'
    candidate = base / 'stage12/layer3_candidates_fixture.jsonl'
    config = {'sources': [
        {'key': 'a', 'platform': 'AMAZON', 'adapter': 'amazon_generic', 'snapshot_mode': 'replace', 'path': str(amazon)},
        {'key': 't', 'platform': 'TEMU', 'adapter': 'temu_frontend', 'snapshot_mode': 'replace', 'path': str(temu)}],
        'paths': [{'key': 'fixture', 'amazon_source': 'a', 'temu_source': 't'}]}
    raw_a = {'asin': 'A', 'title': 'curtain', 'main_image_url': 'https://example.invalid/a'}
    raw_t = [{'goods_id': str(i), 'title': 'curtain', 'main_image_url': 'https://example.invalid/' + str(i)} for i in range(3)]
    write(amazon, [raw_a]); write(temu, raw_t)
    (base / 'stage12_config.json').write_text(json.dumps(config), encoding='utf-8')
    facts_a = normalize_record(raw_a, platform='AMAZON', source_key='a', adapter='amazon_generic', source_path=amazon)
    pairs = []
    for raw in raw_t:
        facts_t = normalize_record(raw, platform='TEMU', source_key='t', adapter='temu_frontend', source_path=temu)
        pairs.append({'source_path': 'fixture', 'asin': 'A', 'goods_id': raw['goods_id'],
                      'pair_revision': 'p' + raw['goods_id'], 'vision_revision_key': 'v' + raw['goods_id'], 'attempt': 1,
                      'amazon_fact_version': facts_a['evidence_hash'], 'temu_fact_version': facts_t['evidence_hash'],
                      'amazon_image_path': 'a', 'temu_image_path': raw['goods_id']})
    write(candidate, pairs)
    results = base / 'stage123/results.jsonl'
    results.parent.mkdir()
    args = SimpleNamespace(results=results, candidates=candidate, validate_only=False, max_requests=0, max_pairs=0)
    cat = {'stage3': {'modules': {'one': 100}, 'batch_size': 1, 'max_concurrency': 1}}
    calls = []
    class Client:
        def __init__(self, **kwargs): self.responses = self
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def create(self, **kwargs):
            calls.append(kwargs)
            return SimpleNamespace(id='response-' + str(len(calls)), status='completed', usage=None, output_text='{}')
    monkeypatch.setenv('DEEPSEEK_API_KEY', 'offline-only')
    monkeypatch.setattr(openai, 'OpenAI', Client)
    monkeypatch.setattr(runner, '_data_url', lambda path: 'data:image/png;base64,fixture')
    monkeypatch.setattr(runner, 'protocol', lambda *args: ('fixture', {}))
    monkeypatch.setattr(runner, 'decode', lambda cat, data, ids: {i: {'overall_confidence': .9} for i in ids})
    monkeypatch.setattr(runner, 'score', lambda *args: {'hard_conflicts': []})
    monkeypatch.setattr(runner, 'load_category', lambda path: cat)
    monkeypatch.setattr(runner, 'compatible_vision_keys', lambda cat, row: [row['vision_revision_key']])
    args.category = base / 'category.json'
    return SimpleNamespace(base=base, amazon=amazon, temu=temu, candidate=candidate, config=config,
                           raw_a=raw_a, raw_t=raw_t, pairs=pairs, args=args, cat=cat, calls=calls)


def test_managed_run_rechecks_sources_while_frozen_candidate_publication_lags(task, monkeypatch):
    def protocol(*args):
        # Candidate publication deliberately lags the authoritative union.
        write(task.temu, task.raw_t[1:])
        return 'fixture', {}
    monkeypatch.setattr(runner, 'protocol', protocol)
    with runner.frozen_candidates(task.candidate) as frozen:
        runner.run(task.args, frozen)
    assert len(task.calls) == 2
    results = list(runner.rows(task.args.results))
    assert {row['goods_id'] for row in results} == {'1', '2'}
    assert all(row['api_status'] == 'success' for row in results)
    intents = list(runner.rows(task.args.results.with_name('request_intents.jsonl')))
    assert len([row for row in intents if row['status'] == 'started']) == 2


def guard(task):
    from selection_automation.stage3_current_input import CurrentInputGuard
    return CurrentInputGuard(task.base, task.candidate, root=task.base.parent.parent, required=True)


@pytest.mark.parametrize('change', ['remove', 'facts', 'pair_revision'])
def test_current_membership_fact_and_published_revision_each_block_stale_group(task, change):
    gate = guard(task)
    assert gate.allows([task.pairs[0]])
    if change == 'remove': write(task.temu, task.raw_t[1:])
    elif change == 'facts':
        task.raw_t[0]['title'] = 'different item'; write(task.temu, task.raw_t)
    else:
        published = [dict(row) for row in task.pairs]
        published[0]['pair_revision'] = 'new-rule-revision'; write(task.candidate, published)
    assert not gate.allows([task.pairs[0]])
    assert gate.allows([task.pairs[1]])
    # A new watch pass reading the still-old candidate cannot restore membership.
    assert not guard(task).allows([task.pairs[0]])


def test_identical_content_and_volatile_timestamp_republication_do_not_block(task):
    gate = guard(task)
    assert gate.allows(task.pairs)
    task.raw_t[0]['observed_at'] = '2026-10-23T13:00:00Z'
    write(task.temu, task.raw_t)
    os.utime(task.candidate, None)
    assert gate.allows(task.pairs)


def test_amazon_facts_local_image_discovery_invalidates_old_fact_version(task):
    task.config['sources'][0]['adapter'] = 'amazon_facts'
    (task.base / 'stage12_config.json').write_text(json.dumps(task.config), encoding='utf-8')
    facts = normalize_record(task.raw_a, platform='AMAZON', source_key='a', adapter='amazon_facts', source_path=task.amazon)
    task.pairs[0]['amazon_fact_version'] = facts['evidence_hash']
    gate = guard(task)
    assert gate.allows([task.pairs[0]])
    image = task.amazon.parent / 'image_cache/amazon_A.jpg'
    image.parent.mkdir(); image.write_bytes(b'local image presence changes normalized facts; no request')
    assert not gate.allows([task.pairs[0]])


@pytest.mark.parametrize('kind', ['missing_config', 'bad_source', 'ambiguous_config', 'missing_versions'])
def test_unavailable_managed_inputs_fail_closed_with_recovery_message(task, kind):
    from selection_automation.stage3_current_input import CurrentInputUnavailable
    if kind == 'missing_config': (task.base / 'stage12_config.json').unlink()
    elif kind == 'bad_source': task.temu.write_text('{broken', encoding='utf-8')
    elif kind == 'ambiguous_config':
        task.config['sources'].append(dict(task.config['sources'][0]))
        (task.base / 'stage12_config.json').write_text(json.dumps(task.config), encoding='utf-8')
    else: task.pairs[0].pop('temu_fact_version')
    with pytest.raises(CurrentInputUnavailable, match='继续'):
        guard(task).allows([task.pairs[0]])
    assert task.calls == []


def test_concurrent_cache_reads_remain_consistent_after_replace(task):
    gate = guard(task)
    with ThreadPoolExecutor(max_workers=8) as pool:
        assert all(pool.map(lambda _: gate.allows(task.pairs), range(16)))
        write(task.temu, task.raw_t[1:])
        assert not any(pool.map(lambda _: gate.allows([task.pairs[0]]), range(16)))
        assert all(pool.map(lambda _: gate.allows([task.pairs[1]]), range(16)))


def test_current_input_failure_preserves_inflight_success_and_creates_no_failed_pair(task, monkeypatch):
    original = openai.OpenAI
    class Client(original):
        def create(self, **kwargs):
            result = super().create(**kwargs)
            task.temu.write_text('{broken', encoding='utf-8')
            return result
    monkeypatch.setattr(openai, 'OpenAI', Client)
    with pytest.raises(SystemExit, match='当前输入.*继续'):
        with runner.frozen_candidates(task.candidate) as frozen:
            runner.run(task.args, frozen)
    assert len(task.calls) == 1
    results = list(runner.rows(task.args.results))
    assert len(results) == 1 and results[0]['api_status'] == 'success'
    intents = list(runner.rows(task.args.results.with_name('request_intents.jsonl')))
    assert [row['status'] for row in intents] == ['started', 'settled']


def test_removed_historical_success_is_retained_and_legal_success_is_not_resent(task):
    successful = [{'asin': row['asin'], 'goods_id': row['goods_id'], 'pair_revision': row['pair_revision'],
                   'vision_revision_key': row['vision_revision_key'], 'api_status': 'success', 'attempt': 1}
                  for row in task.pairs[:2]]
    write(task.args.results, successful)
    prefix = task.args.results.read_bytes()
    write(task.temu, task.raw_t[1:])
    with runner.frozen_candidates(task.candidate) as frozen:
        runner.run(task.args, frozen)
    assert len(task.calls) == 1
    assert task.args.results.read_bytes().startswith(prefix)
    assert [row['goods_id'] for row in runner.rows(task.args.results)] == ['0', '1', '2']


def test_parallel_inflight_results_survive_replacement_but_next_group_is_not_dispatched(task, monkeypatch):
    task.cat['stage3']['max_concurrency'] = 2
    second = threading.Event()
    mutex = threading.Lock()
    original = openai.OpenAI
    class Client(original):
        def create(self, **kwargs):
            with mutex:
                response = super().create(**kwargs)
                number = len(task.calls)
            if number == 1:
                assert second.wait(3)
            elif number == 2:
                write(task.temu, task.raw_t[1:2])
                second.set()
            return response
    monkeypatch.setattr(openai, 'OpenAI', Client)
    with runner.frozen_candidates(task.candidate) as frozen:
        runner.run(task.args, frozen)
    assert len(task.calls) == 2
    results = list(runner.rows(task.args.results))
    assert {row['goods_id'] for row in results} == {'0', '1'}
    assert all(row['api_status'] == 'success' for row in results)
    assert len(list(runner.rows(task.args.results.with_name('request_intents.jsonl')))) == 4


@pytest.mark.parametrize('directory', ['stage123', 'Stage123'])
def test_real_managed_entry_requires_source_configuration(task, directory):
    task.args.results = task.base / directory / 'results.jsonl'
    (task.base / 'stage12_config.json').unlink()
    with pytest.raises(SystemExit, match='当前输入.*继续'):
        with runner.frozen_candidates(task.candidate) as frozen:
            runner.run(task.args, frozen)
    assert task.calls == []
    assert not task.args.results.exists()
    assert not task.args.results.with_name('request_intents.jsonl').exists()


def test_managed_run_rejects_a_foreign_candidate_publication(task):
    foreign = task.base / 'other/candidates.jsonl'
    write(foreign, task.pairs)
    task.args.candidates = foreign
    with pytest.raises(SystemExit, match='当前输入.*继续'):
        with runner.frozen_candidates(foreign) as frozen:
            runner.run(task.args, frozen)
    assert task.calls == []
    assert not task.args.results.with_name('request_intents.jsonl').exists()


def test_standalone_cli_still_rechecks_its_current_published_revision(tmp_path):
    from selection_automation.stage3_current_input import CurrentInputGuard, CurrentInputUnavailable
    path = tmp_path / 'candidate.jsonl'
    row = {'asin': 'A', 'goods_id': 'T', 'pair_revision': 'old'}
    write(path, [row])
    gate = CurrentInputGuard(tmp_path, path, root=tmp_path)
    assert gate.allows([row])
    write(path, [{**row, 'pair_revision': 'new'}])
    assert not gate.allows([row])
    path.unlink()
    with pytest.raises(CurrentInputUnavailable): gate.allows([row])


pytestmark = pytest.mark.usefixtures('offline_api_transport')
