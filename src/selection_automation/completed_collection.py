"""Verify retired collection data before analysis-only resume."""
import hashlib
import json
from pathlib import Path
import re

from .amazon_listing_url import listing_identity
from .workers import category_worker_count


def _read(path):
    try:
        return json.loads(path.read_text('utf-8-sig'))
    except (OSError, ValueError):
        return {}


def _digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def completed_checkpoint(root, run, category):
    """Return evidence only when no original collector or browser is needed.

    Browser cleanup deliberately removes sessions.json. Its retirement receipt
    is evidence, never a manifest to restore or bind. Explicitly restored history
    may use its completed task-local retirement records after a reinstall.
    Any incomplete/changed checkpoint continues through normal strict recovery.
    """
    try:
        return _completed_checkpoint(root, run, category)
    except (OSError, ValueError, TypeError, KeyError, AttributeError):
        return None


def _collection_config(category):
    return {key: category.get(key) for key in ('category_key', 'amazon', 'temu')}


def _completed_checkpoint(root, run, category):
    root, run = Path(root).resolve(), Path(run).resolve()
    key = category.get('category_key', '')
    if not re.fullmatch(r'[a-z][a-z0-9_]{0,63}', key) or run != root/'runs'/key:
        return None
    if _read(run/'category.json') != category:
        return None
    machine = _read(root/'runtime/machine.json').get('id')
    if not machine:
        return None
    plan_path = run/'temu/keyword_plan.json'
    dispatch_path = run/'temu/supervisors/assignments.json'
    cleanup_path = run/'cleanup_status.json'
    amazon_path = run/'amazon/amazon_frontend_latest.json'
    plan, dispatch = _read(plan_path), _read(dispatch_path)
    keywords, jobs = plan.get('keywords'), dispatch.get('jobs')
    if plan.get('finalized') is not True or not keywords or not isinstance(jobs, list) or not jobs:
        return None
    queries = [row.get('query') for row in keywords]
    if (not all(isinstance(query, str) and query.strip() for query in queries)
            or len(set(queries)) != len(queries)
            or len(jobs) != len(queries)
            or {row.get('query') for row in jobs} != set(queries)):
        return None
    for job in jobs:
        passes = job.get('sort_passes')
        if (job.get('complete') is not True or not isinstance(passes, dict)
                or set(passes) != {'sales', 'recent'}
                or any(state.get('complete') is not True for state in passes.values())):
            return None
        requested = next(row for row in keywords if row['query'] == job['query'])
        if int(job.get('max_products') or 0) < int(requested.get('max_products') or 0):
            return None
    cleanup = _read(cleanup_path)
    if (cleanup.get('status') != 'COMPLETE' or cleanup.get('pending_workers')
            or cleanup.get('waiting_workers')):
        return None
    amazon = _read(amazon_path)
    records, target = amazon.get('records'), category['amazon']['target_count']
    expected_listing = listing_identity(category['amazon'].get('bestsellers_url'))
    if (amazon.get('status') != 'COMPLETE' or amazon.get('details_complete') is not True
            or amazon.get('missing_details') or not isinstance(records, list)
            or len(records) != target or len({row.get('asin') for row in records}) != target
            or any(not row.get('asin') for row in records)
            or sorted(row.get('rank', 0) for row in records) != list(range(1, target+1))
            or not expected_listing or listing_identity(amazon.get('source_url')) != expected_listing):
        return None
    # Pending rotation is a browser command and cannot be silently consumed here.
    from .rotation_queue import PENDING, read_requests
    if any(row.get('status') in PENDING for row in read_requests(run)):
        return None
    manifest_path = root/'config/sessions.json'
    candidates = [(manifest_path, _read(manifest_path))]
    for receipt in sorted((root/'runtime/profile_retirement').glob('*.json'), reverse=True):
        archived = _read(receipt)
        if archived.get('machine_id') != machine:
            continue
        for record in archived.get('records', []):
            if Path(record.get('path', '')).resolve() == manifest_path:
                candidates.append((receipt, record.get('manifest', {})))
    retired_workers = []
    for identity_path, manifest in candidates:
        workers = manifest.get('workers')
        if (manifest.get('machine_id') != machine or manifest.get('task_key') != key
                or not isinstance(workers, list) or len(workers) != category_worker_count(category)):
            continue
        retired_workers.append((identity_path, workers))
    restore_path = root/'runtime/web-console/state.json'
    restore = _read(restore_path)
    if restore.get('category_key') == key and restore.get('explicitly_restored') is True:
        names = {state.get('owner') for job in jobs for state in job['sort_passes'].values()}
        if (len(names) == category_worker_count(category)
                and all(isinstance(name, str) and re.fullmatch(r'worker-\d{2}(?:-new)?', name) for name in names)):
            # These are task data, not authorization to start or modify a browser.
            retired_workers.append((restore_path, [
                {'name':name, 'profile_id':_read(run/'temu/supervisors'/f'{name}.json').get('deleted_profile_id')}
                for name in sorted(names)]))
    for identity_path, workers in retired_workers:
        names = [worker.get('name', '') for worker in workers]
        if (len(set(names)) != len(names)
                or any(not re.fullmatch(r'worker-\d{2}(?:-new)?', name) for name in names)
                or any(state.get('owner') not in names for job in jobs for state in job['sort_passes'].values())):
            continue
        worker_paths = [run/'temu/supervisors'/f'{name}.json' for name in names]
        states = [_read(path) for path in worker_paths]
        if any(state.get('status') != 'WORKER_COMPLETE'
               or state.get('environment_cleanup') != 'COMPLETE'
               or not worker.get('profile_id')
               or str(state.get('deleted_profile_id')) != str(worker['profile_id'])
               for worker, state in zip(workers, states)):
            continue
        # A completed nonempty lane must still have its original source snapshots;
        # verified empty passes are the collector's explicit alternative evidence.
        from .empty_results import verified_empty_evidence
        if any(not any(path.stat().st_size for path in (run/'temu/workers'/name).glob('*/products.jsonl'))
               and any(not verified_empty_evidence(job.get('no_results_evidence'), job['query']) for job in jobs
                       if any(state.get('owner') == name for state in job['sort_passes'].values()))
               for name in names):
            continue
        paths = [plan_path, dispatch_path, cleanup_path, amazon_path, identity_path,
                 root/'runtime/machine.json', *worker_paths]
        try:
            evidence = {str(path): _digest(path) for path in paths}
        except OSError:
            continue
        sources = {str(path): [path.stat().st_size, path.stat().st_mtime_ns]
                   for name in names for path in (run/'temu/workers'/name).glob('*/products.jsonl')}
        return {'mode': 'completed_collection_analysis_resume', 'category_key': key,
                'machine_id': machine, 'keyword_count': len(queries), 'amazon_count': len(records),
                'worker_count': len(names), 'identity_evidence': str(identity_path), 'files': evidence,
                'source_files': sources, 'category_path': str(run/'category.json'),
                'collection_config': _collection_config(category)}
    return None


def unchanged(checkpoint):
    """A later checkpoint change requires a fresh, explicit recovery decision."""
    try:
        return (all(_digest(Path(path)) == digest for path, digest in checkpoint['files'].items())
                and all([Path(path).stat().st_size, Path(path).stat().st_mtime_ns] == stamp
                        for path, stamp in checkpoint['source_files'].items())
                and _collection_config(_read(Path(checkpoint['category_path']))) == checkpoint['collection_config'])
    except (OSError, ValueError, KeyError):
        return False
