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
        'search_query': q, 'unique_goods': n, 'status': status,
        'sales_sort': {'requested': order},
        'started_at': '2026-10-01T00:00:00Z', 'updated_at': '2026-10-01T01:00:00Z'})


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
