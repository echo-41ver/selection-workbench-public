import json
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from collection_progress import collection_total


def put(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding='utf-8')


def fixture(base):
    put(base / 'temu/supervisors/assignments.json', {
        'lanes': ['w1', 'w2', 'w3', 'w4'],
        'jobs': [{'query': q, 'max_products': n} for q, n in [('general', 1200), ('a', 400), ('b', 400), ('c', 400)]]})


def snapshot(base, folder, q, n, status='RUNNING', order='sales'):
    put(base / f'temu/workers/w1/{folder}/progress.json', {
        'search_query': q, 'unique_goods': n, 'raw_collected_count':n, 'status': status,
        'sales_sort': {'requested': order},
        'started_at': '2026-10-01T00:00:00Z', 'updated_at': '2026-10-01T01:00:00Z'})


def test_quantity_target_is_not_completion_until_both_passes_finish(tmp_path):
    put(tmp_path/'temu/supervisors/assignments.json',{'lanes':['w1'],'jobs':[{'query':'one','max_products':1000}]})
    for order in ('sales','recent'):snapshot(tmp_path,order,'one',1000,'RUNNING',order)
    result=collection_total(tmp_path,{})
    assert result['remaining']==0 and result['percent']==99.9
    assert result['all_passes_complete'] is False
    for order in ('sales','recent'):snapshot(tmp_path,order,'one',1000,'KEYWORD_LIMIT_REACHED',order)
    result=collection_total(tmp_path,{})
    assert result['percent']==100 and result['all_passes_complete'] is True


def test_raw_counts_are_independent_of_unique_goods_and_keep_triple_category_budget(tmp_path):
    put(tmp_path/'temu/supervisors/assignments.json',{'lanes':['w1'],'jobs':[
        {'query':'ordinary','max_products':400},{'query':'category','max_products':1200}]})
    for q,n in [('ordinary',400),('category',1200)]:
        for mode in ('sales','recent'):
            snapshot(tmp_path,q+mode,q,n,'KEYWORD_LIMIT_REACHED',mode)
            path=tmp_path/f'temu/workers/w1/{q+mode}/progress.json'
            row=json.loads(path.read_text());row['unique_goods']=17;put(path,row)
    result=collection_total(tmp_path,{})
    assert result['completed']==result['actual_count']==result['planned_total']==3200
    assert result['all_passes_complete'] and result['count_basis']=='RAW_FRONTEND_CARDS'


def test_weighted_total_counts_each_sort_and_does_not_add_retry_duplicates(tmp_path):
    base = tmp_path / 'runs/current'; fixture(base)
    snapshot(base, 'general', 'general', 600)
    snapshot(base, 'retry', 'general', 600)
    snapshot(base, 'general-recent', 'general', 300, order='recent')
    row = collection_total(base, {})
    assert row['planned_total'] == row['total'] == 4800
    assert row['completed'] == 900
    assert row['remaining'] == 3900
    assert row['keyword_count'] == 4 and row['total_passes'] == 8


def test_short_result_adjusts_target_only_after_confirmed_completion(tmp_path):
    base = tmp_path / 'runs/current'; fixture(base)
    snapshot(base, 'a', 'a', 5)
    assert collection_total(base, {})['shortfall'] == 0
    snapshot(base, 'a', 'a', 5, 'KEYWORD_COMPLETE')
    row = collection_total(base, {})
    assert row['total'] == 4405 and row['shortfall'] == 395
    assert row['completed'] == 5


def test_history_throughput_uses_parallel_lanes_and_preserves_pause_information(tmp_path):
    base = tmp_path / 'runs/current'; fixture(base)
    history = tmp_path / 'runs/past'
    put(history / 'category.json', {})
    snapshot(history, 'old', 'old', 400, 'KEYWORD_LIMIT_REACHED')
    put(base / 'temu/supervisors/worker-01-new.json', {'manual_action_required': True})
    row = collection_total(base, {})
    assert row['source'] == 'history' and row['history_tasks'] == 1
    assert row['rate_per_hour'] == 1200
    assert row['eta_seconds'] == 14400
    assert row['blocked_lanes'] == 1


def test_missing_history_and_all_passes_complete(tmp_path):
    base = tmp_path / 'runs/current'; fixture(base)
    assert collection_total(base, {})['eta_seconds'] is None
    for q, n in [('general', 1200), ('a', 400), ('b', 400), ('c', 400)]:
        for order in ('sales', 'recent'):
            snapshot(base, q + order, q, n, 'KEYWORD_LIMIT_REACHED', order)
    row = collection_total(base, {})
    assert row['percent'] == 100 and row['eta_seconds'] == 0
    assert row['completed'] == 4800


def test_last_pass_live_verification_overrides_running_supervisor(tmp_path):
    base = tmp_path / 'runs/current'
    put(base / 'temu/supervisors/assignments.json', {
        'lanes': ['w1', 'w2', 'w3', 'w4'],
        'jobs': [{'query': 'pump', 'max_products': 1200}]})
    snapshot(base, 'sales', 'pump', 400, 'KEYWORD_COMPLETE')
    snapshot(base, 'recent', 'pump', 545, 'WAITING_HUMAN_VERIFICATION', 'recent')
    put(base / 'temu/supervisors/worker-02-new.json', {
        'status': 'RUNNING', 'current_query': 'pump', 'current_sort': 'recent'})
    rate = {'per_lane_per_second': 1}
    row = collection_total(base, {}, rate_snapshot=rate)
    assert row['blocked_lanes'] == 1
    # Idle lanes cannot work on the already-started final pass.
    assert row['eta_seconds'] is None and row['rate_per_hour'] is None
    snapshot(base, 'recent', 'pump', 545, 'RUNNING', 'recent')
    put(base / 'temu/supervisors/worker-02-new.json', {
        'status': 'HUMAN_ACTION_REQUIRED', 'manual_action_required': True,
        'current_query': 'pump', 'current_sort': 'recent'})
    row = collection_total(base, {}, rate_snapshot=rate)
    assert row['blocked_lanes'] == 0 and row['eta_seconds'] == 655


def empty_snapshot(base, query='empty', **changes):
    from urllib.parse import quote
    row = {'search_query': query, 'unique_goods': 0, 'status': 'KEYWORD_NO_RESULTS',
           'sales_sort': {'requested': 'sales'}, 'completion_basis': 'EXPLICIT_EMPTY_QUERY_RESULTS',
           'no_results_evidence': {'confirmed': True, 'protocol': 'VISIBLE_EMPTY_RESULTS_STABLE_V2',
               'query': query, 'loading': False, 'stable_ms': 31000, 'samples': 3,
               'url': 'https://www.temu.com/search_result.html?search_key=' + quote(query)}}
    row.update(changes)
    put(base / 'temu/workers/w1/empty/progress.json', row)
    return row


def test_confirmed_empty_query_completes_both_sorts_without_inventing_goods(tmp_path):
    base = tmp_path / 'runs/current'
    put(base / 'temu/supervisors/assignments.json', {
        'lanes': ['w1'], 'jobs': [{'query': 'empty', 'max_products': 200}, {'query': 'full', 'max_products': 200}]})
    empty_snapshot(base)
    for order in ('sales', 'recent'):
        snapshot(base, 'full-' + order, 'full', 200, 'KEYWORD_LIMIT_REACHED', order)
    row = collection_total(base, {})
    assert row['finished_passes'] == row['total_passes'] == 4
    assert row['percent'] == 100 and row['remaining'] == row['eta_seconds'] == 0
    assert row['actual_count'] == row['completed'] == row['total'] == 400
    assert row['planned_total'] == 800 and row['shortfall'] == 400
    assert row['empty_keyword_count'] == 1


def test_missing_counts_are_not_completed_by_worker_complete_alone(tmp_path):
    base = tmp_path / 'runs/current'
    put(base / 'temu/supervisors/assignments.json', {'lanes': ['w1'], 'jobs': [{'query': 'empty', 'max_products': 200}]})
    put(base / 'temu/supervisors/worker-01-new.json', {'status': 'WORKER_COMPLETE',
        'completed_passes': [{'query': 'empty', 'sort': order} for order in ('sales', 'recent')]})
    row = collection_total(base, {})
    assert row['finished_passes'] == 0 and row['remaining'] == 400
    empty_snapshot(base, no_results_evidence={'confirmed': True})
    assert collection_total(base, {})['finished_passes'] == 1


def test_loading_unrelated_or_manual_empty_evidence_cannot_complete_skipped_sort(tmp_path):
    base = tmp_path / 'runs/current'
    put(base / 'temu/supervisors/assignments.json', {'lanes': ['w1'], 'jobs': [{'query': 'empty', 'max_products': 200}]})
    for change in ({'loading': True}, {'query': 'other'}, {'samples': 1}, {'stable_ms': 1000}):
        row = empty_snapshot(base)
        row['no_results_evidence'].update(change)
        put(base / 'temu/workers/w1/empty/progress.json', row)
        assert collection_total(base, {})['finished_passes'] == 0
    empty_snapshot(base, manual_action_required=True)
    assert collection_total(base, {})['finished_passes'] == 0
