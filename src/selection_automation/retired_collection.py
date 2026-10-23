"""Authorize fresh browsers after an owned task's normal environment retirement."""
import json
from pathlib import Path
import re

from .workers import category_worker_count


def _read(path):
    try:
        value = json.loads(path.read_text('utf-8-sig'))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def retired_task_receipt(root, task_key, machine_id, worker_count):
    """Return ownership proof for reprovisioning, never a manifest to restore.

    An absent sessions file alone cannot authorize recovery. Require the frozen
    task and a complete local retirement receipt; any partial new bootstrap may
    contain only fresh profiles belonging to that same task and installation.
    """
    try:
        return _retired_task_receipt(Path(root).resolve(), task_key, machine_id, worker_count)
    except (OSError, ValueError, TypeError, KeyError, AttributeError):
        return None


def _retired_task_receipt(root, key, machine, count):
    if not re.fullmatch(r'[a-z][a-z0-9_]{0,63}', key) or not machine or type(count) is not int:
        return None
    manifest_path = root / 'config/sessions.json'
    run = root / 'runs' / key
    category = _read(run / 'category.json')
    if (manifest_path.exists() or _read(root / 'runtime/machine.json').get('id') != machine
            or category.get('category_key') != key or category_worker_count(category) != count):
        return None
    names = {f'worker-{number:02d}-new' for number in range(1, count + 1)}
    bootstrap_path = root / 'runtime/bootstrap.json'
    bootstrap = _read(bootstrap_path)
    if bootstrap_path.exists() and (bootstrap.get('machine_id') != machine
            or bootstrap.get('task_key') != key or bootstrap.get('used') is not False
            or bootstrap.get('requested_worker_count', count) != count):
        return None
    for path in sorted((root / 'runtime/profile_retirement').glob('*.json'), reverse=True):
        receipt = _read(path)
        if receipt.get('machine_id') != machine or receipt.get('new_task_key') != key:
            continue
        deleted, missing = receipt.get('deleted', []), receipt.get('already_missing', [])
        if (not isinstance(deleted, list) or not isinstance(missing, list)
                or any(not isinstance(profile, str) for profile in deleted + missing)):
            continue
        retired = set(deleted + missing)
        for record in receipt.get('records', []):
            if Path(record.get('path', '')).resolve() != manifest_path:
                continue
            manifest = record.get('manifest', {})
            workers = manifest.get('workers')
            if (manifest.get('schema_version') != 'temu-browser-sessions-v1'
                    or manifest.get('machine_id') != machine or manifest.get('task_key') != key
                    or manifest.get('used') is not True or type(manifest.get('worker_count')) is not int
                    or manifest.get('worker_count') != count
                    or not isinstance(workers, list) or len(workers) != count
                    or {worker.get('name') for worker in workers} != names):
                continue
            profiles = [worker.get('profile_id') for worker in workers]
            if (any(not isinstance(profile, str) or not re.fullmatch(r'[A-Za-z0-9_-]+', profile)
                    for profile in profiles) or len(set(profiles)) != count
                    or not set(profiles).issubset(retired)):
                continue
            if any(_read(run / f"temu/supervisors/{worker['name']}.json").get('deleted_profile_id')
                    not in (None, worker['profile_id']) for worker in workers):
                continue
            pending = bootstrap.get('workers', [])
            if (not isinstance(pending, list) or len(pending) > count
                    or len({worker.get('name') for worker in pending}) != len(pending)
                    or any(worker.get('name') not in names or worker.get('profile_id') in profiles
                           for worker in pending)):
                continue
            return path
    return None
