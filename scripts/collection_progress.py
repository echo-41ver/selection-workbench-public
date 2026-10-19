"""Read-only collection totals and cached historical per-lane throughput."""
from datetime import datetime
from pathlib import Path
import json
import math
import re
import time
from empty_results_checkpoint import confirmed_empty_checkpoint

_HISTORY = {}
DONE = {'KEYWORD_COMPLETE', 'KEYWORD_LIMIT_REACHED'}

def collected_count(row):
    return row.get('raw_collected_count',row.get('unique_goods',0))


def blocked_observation(row):
    status = str(row.get('status') or '')
    return (any(row.get(flag) is True for flag in
                ('manual_action_required', 'human_action_required', 'human_verification_required'))
            or status.startswith(('STOPPED_', 'WAITING_HUMAN', 'WAITING_BROWSER_CONTROL'))
            or status in {'HUMAN_ACTION_REQUIRED', 'FAILED_AFTER_3_ATTEMPTS', 'BINDING_FAILED'})


def read(path):
    try:
        if path.stat().st_size > 8 * 1024 * 1024:
            return {}
        value = json.loads(path.read_text(encoding='utf-8'))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def stamp(value):
    try:
        return datetime.fromisoformat(value.replace('Z', '+00:00')).timestamp()
    except (AttributeError, ValueError, TypeError):
        return None


def snapshots(base):
    """One observation per query/sort, including retries or migrated ownership."""
    result = {}
    for path in (base / 'temu/workers').glob('*/*/progress.json'):
        row = read(path)
        query = row.get('search_query', row.get('query'))
        order = (row.get('sales_sort') or {}).get('requested', 'sales')
        count = collected_count(row)
        if not query or type(count) is not int or count < 0:
            continue
        key = (query, order)
        previous = result.get(key)
        if previous is None or (stamp(row.get('updated_at')) or 0) > (stamp(previous.get('updated_at')) or 0):
            result[key] = row
    return result


def sample(rows):
    count = seconds = batches = 0
    for row in rows:
        if 'raw_collected_count' not in row:continue
        if row.get('status') not in DONE:
            continue
        start, end = stamp(row.get('started_at')), stamp(row.get('updated_at'))
        goods = collected_count(row)
        if start is None or end is None or end - start < 10 or type(goods) is not int or goods <= 0:
            continue
        count += goods
        seconds += end - start
        batches += 1
    return count, seconds, batches


def historical_rate(base):
    key = str(base.resolve())
    now = time.monotonic()
    cached = _HISTORY.get(key)
    if cached and now - cached[0] < 60:
        return cached[1]
    count = seconds = batches = tasks = 0
    candidates = sorted(base.parent.glob('*/category.json'), key=lambda p: p.stat().st_mtime, reverse=True)
    for category in candidates[:30]:
        if category.parent.resolve() == base.resolve():
            continue
        n, duration, size = sample(snapshots(category.parent).values())
        if size:
            count += n; seconds += duration; batches += size; tasks += 1
    result = {'per_lane_per_second': count / seconds if seconds else None,
              'history_tasks': tasks, 'sample_batches': batches, 'source': 'history'}
    _HISTORY[key] = (now, result)
    return result


def collection_total(base, category, *, rate_snapshot=None):
    assignments = read(base / 'temu/supervisors/assignments.json')
    planned = {}
    jobs = assignments.get('jobs', [])
    for job in jobs:
        target = job.get('max_products')
        if type(target) is int and target > 0 and job.get('query'):
            for order in ('sales', 'recent'):
                planned[(job['query'], order)] = target
    # Older assignment manifests have no per-query caps; retain their own passes.
    if not planned:
        target = category.get('temu', {}).get('max_products_per_keyword')
        if type(target) is int and target > 0:
            for worker in assignments.get('workers', []):
                for item in worker.get('passes', worker.get('queries', [])):
                    query, order = (item.get('query'), item.get('sort', 'sales')) if isinstance(item, dict) else (item, 'sales')
                    if query:
                        planned[(query, order)] = target
    if not planned:
        return None
    observed = snapshots(base)
    # The collector skips both sorts only after an evidence-backed empty query.
    empty_queries = {query for (query, _), row in observed.items()
                     if confirmed_empty_checkpoint(row, query)}
    completed_passes = set()
    states = [read(p) for p in (base / 'temu/supervisors').glob('worker-*.json')
              if re.fullmatch(r'worker-\d+(?:-new)?',p.stem)]
    for worker in states:
        completed_passes.update((p.get('query'), p.get('sort', 'sales')) for p in worker.get('completed_passes', []) if isinstance(p, dict))
    actual = completed = shortage = finished = 0
    blocked_passes = set()
    for key, target in planned.items():
        row = observed.get(key, {})
        goods = collected_count(row)
        actual += goods
        empty_done = key[0] in empty_queries and goods == 0 and not blocked_observation(row)
        done = row.get('status') in DONE or key in completed_passes
        # A completed pass without its count is not evidence of zero goods.
        done = (done and bool(row)) or empty_done
        if done:
            finished += 1
            shortage += max(0, target - goods)
        elif blocked_observation(row):
            blocked_passes.add(key)
        completed += min(goods, target)
    original = sum(planned.values())
    total = original - shortage
    remaining = max(0, total - completed)
    lanes = len(assignments.get('lanes') or assignments.get('workers') or states)
    rate = historical_rate(base).copy() if rate_snapshot is None else {
        'per_lane_per_second':rate_snapshot.get('per_lane_per_second'),
        'history_tasks':rate_snapshot.get('history_tasks',0),
        'sample_batches':rate_snapshot.get('sample_batches',0),
        'source':rate_snapshot.get('source','current')}
    if not rate['per_lane_per_second']:
        count, seconds, batches = sample(observed.values())
        rate.update(per_lane_per_second=count / seconds if seconds else None, sample_batches=batches, source='current')
    blocked_states = 0
    for worker in states:
        key = (worker.get('current_query'), worker.get('current_sort', 'sales'))
        # The collector's live state supersedes a stale supervisor warning.
        current = observed.get(key, worker)
        if current.get('status') not in DONE and worker.get('status') != 'WORKER_COMPLETE':
            blocked_states += int(blocked_observation(current))
    blocked = min(lanes, max(len(blocked_passes), blocked_states))
    capacity = min(max(0, lanes - blocked), max(0, len(planned) - finished - len(blocked_passes)))
    speed = (rate['per_lane_per_second'] or 0) * capacity
    basis='LEGACY_UNIQUE' if any('raw_collected_count' not in row for row in observed.values()) else 'RAW_FRONTEND_CARDS'
    return {'completed': completed, 'total': total, 'planned_total': original, 'actual_count': actual,'count_basis':basis,
            'remaining': remaining,
            'all_passes_complete':finished==len(planned),
            'percent':100 if finished==len(planned) else min(99.9,round(100 * completed / total, 1)) if total else 0,
            'keyword_count': len({q for q, _ in planned}), 'finished_passes': finished, 'total_passes': len(planned),
            'shortfall': shortage, 'empty_keyword_count': len(empty_queries), 'blocked_lanes': blocked, 'lane_count': lanes,
            'eta_seconds': math.ceil(remaining / speed) if remaining and speed else (0 if not remaining else None),
            'rate_per_hour': round(speed * 3600) if speed else None, **rate}
