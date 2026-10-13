"""Local operator decisions, separate from evidence and recommendation history."""
from datetime import datetime, timezone
import json
from pathlib import Path
import re

from .commercial import read_json
from .merge_temu_snapshots import atomic_write
from .operations import operator_id
from .process_lock import ProcessLock

STATES = {'NEW': '未处理', 'INQUIRY': '询价中', 'SAMPLE': '打样中', 'HOLD': '暂缓', 'REJECT': '不做'}


def feedback_path(root, label):
    return Path(root) / 'runtime/operations/feedback' / (operator_id(label) + '.json')


def read_feedback(root, label):
    return read_json(feedback_path(root, label), {'items': {}}).get('items', {})


def save_feedback(root, label, body, *, now=None):
    asin = body.get('asin')
    state = body.get('state')
    reason = body.get('reason', '')
    source_run_key = body.get('source_run_key')
    if not isinstance(asin, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,40}', asin):
        raise ValueError('商品标识无效')
    if not isinstance(state, str) or state not in STATES:
        raise ValueError('请选择有效跟进状态')
    if not isinstance(reason, str) or len(reason) > 1000:
        raise ValueError('备注请控制在1000字以内')
    if state in {'HOLD', 'REJECT'} and not reason.strip():
        raise ValueError('请记录暂缓或不做的原因，便于后续选品复盘')
    if not isinstance(source_run_key, str) or not re.fullmatch(r'[a-z][a-z0-9_]{0,95}', source_run_key):
        raise ValueError('商品来源任务无效')
    now = now or datetime.now(timezone.utc)
    path = feedback_path(root, label)
    path.parent.mkdir(parents=True, exist_ok=True)
    with ProcessLock(path.with_suffix('.lock')):
        data = read_json(path, {'items': {}})
        old = data['items'].get(asin, {})
        # Retried requests do not create duplicate transitions.
        if all(old.get(k) == v for k, v in {'state': state, 'reason': reason.strip(), 'source_run_key': source_run_key}.items()):
            return old
        change = {'state': state, 'label': STATES[state], 'reason': reason.strip(), 'source_run_key': source_run_key,
                  'updated_at': now.isoformat()}
        row = {'asin': asin, **change, 'history': [*old.get('history', []), change]}
        data['items'][asin] = row
        atomic_write(path, json.dumps(data, ensure_ascii=False, indent=2))
        return row


def excluded_from_new_recommendations(row):
    return row.get('state') in {'INQUIRY', 'SAMPLE', 'HOLD', 'REJECT'}
