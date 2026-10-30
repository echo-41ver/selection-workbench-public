"""Per-logical-call rule model metadata. Never stores content or credentials."""
from __future__ import annotations
import json
import math
import os
from pathlib import Path
import re
import sys
import time
import uuid

TOKEN_KEYS = frozenset(('input_tokens', 'output_tokens', 'total_tokens', 'prompt_tokens', 'completion_tokens',
    'prompt_cache_hit_tokens', 'prompt_cache_miss_tokens', 'cache_creation_input_tokens', 'cache_read_input_tokens',
    'reasoning_tokens', 'cached_tokens', 'audio_tokens', 'text_tokens', 'image_tokens',
    'accepted_prediction_tokens', 'rejected_prediction_tokens'))
DETAIL_KEYS = frozenset(('input_tokens_details', 'output_tokens_details', 'prompt_tokens_details', 'completion_tokens_details'))
_FAILED_ROOTS = set()


def numeric_usage(value):
    if not isinstance(value, dict):
        return {}
    result = {}
    for key in TOKEN_KEYS:
        item = value.get(key)
        if type(item) in (int, float) and 0 <= item <= 10**15 and math.isfinite(item):
            result[key] = item
    for key in DETAIL_KEYS:
        item = value.get(key)
        if isinstance(item, dict):
            details = {name: item[name] for name in TOKEN_KEYS if type(item.get(name)) in (int, float)
                       and 0 <= item[name] <= 10**15 and math.isfinite(item[name])}
            if details:
                result[key] = details
    return result


def safe_identifier(value, secret=''):
    if (isinstance(value, str) and re.fullmatch(r'[A-Za-z0-9_.:-]{1,200}', value)
            and not re.search(r'(?i)bearer|sk[-_]', value) and not (secret and secret in value)):
        return value
    return None


def provider_metadata(packet):
    if not isinstance(packet, dict):
        return {}
    result = {}
    identifier = safe_identifier(packet.get('id'), os.getenv('DEEPSEEK_API_KEY', '').strip())
    if identifier:
        result['response_id'] = identifier
    usage = numeric_usage(packet.get('usage'))
    if usage:
        result['model_usage'] = usage
    return result


class RuleModelEvidenceError(RuntimeError):
    code = 'MODEL_EVIDENCE_WRITE_FAILED'
    retryable = False

    def __init__(self):
        super().__init__('模型调用记录无法保存；已停止自动付费请求。请检查程序目录可写及磁盘空间后重新启动本次操作')


class RuleModelEvidence:
    def __init__(self, root, secret=''):
        root = Path(root).resolve()
        self.folder = root / 'runtime/model-call-evidence'
        self.secret = secret
        self.storage_state = 'pending'
        self._attempts = set()
        self._usage = set()
        call_id = uuid.uuid4().hex
        context = os.getenv('SELECTION_MODEL_DRAFT_CONTEXT', '')
        context = context if re.fullmatch(r'category-[a-f0-9]{32}', context) else None
        entry = Path(sys.argv[0]).name
        entry = entry if re.fullmatch(r'[A-Za-z0-9_-]{1,80}\.py', entry) else 'other_python_entry'
        self.path = self.folder / (call_id + '.json')
        self.record = {'schema': 1, 'logical_call_id': call_id, 'entry': entry, 'draft_context': context,
            'started_at': time.time(), 'finished_at': None, 'outcome': 'running',
            'transport_attempt_count': 0, 'transport_attempts_state': 'unavailable', 'events': [],
            'http_statuses': [], 'http_status_state': 'unavailable', 'provider_response_ids': [],
            'response_id_state': 'unavailable', 'usage': {}, 'usage_observations': [],
            'usage_state': 'unavailable', 'billing_state': 'UNKNOWN', 'amount': None,
            'amount_state': 'PROVIDER_BILLING_NOT_OBSERVED'}
        self._root = root

    def _save(self):
        temporary = None
        try:
            if not self.folder.resolve().is_relative_to(self._root):
                raise OSError('EVIDENCE_PATH_OUTSIDE_ROOT')
            self.folder.mkdir(parents=True, exist_ok=True)
            temporary = self.folder / ('.' + self.path.stem + '.' + uuid.uuid4().hex + '.tmp')
            with temporary.open('x', encoding='utf-8') as stream:
                json.dump(self.record, stream, ensure_ascii=False, indent=2)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
            self.storage_state = 'recorded'
        except OSError:
            # Never throw inside a live transport notification; fail closed at entry/exit.
            self.storage_state = 'unavailable'
        finally:
            if temporary is not None:
                try:
                    temporary.unlink(missing_ok=True)
                except OSError:
                    pass

    def __enter__(self):
        if self._root in _FAILED_ROOTS:
            self.storage_state = 'unavailable'
            raise RuleModelEvidenceError()
        self._save()
        if self.storage_state != 'recorded':
            _FAILED_ROOTS.add(self._root)
            raise RuleModelEvidenceError()
        return self

    def observe(self, **fields):
        changed = False
        network = fields.get('model_network')
        if isinstance(network, dict):
            attempt, phase, route = network.get('attempt'), network.get('phase'), network.get('route')
            if type(attempt) is int and 1 <= attempt <= 16 and phase in ('connecting', 'response_received', 'connect_failed'):
                event = {'time': time.time(), 'attempt': attempt, 'phase': phase,
                         'route': route if route in ('direct', 'system_proxy', 'default_transport') else 'unreported'}
                if network.get('request_sent') is False:
                    event['request_sent'] = False
                self.record['events'].append(event)
                if phase == 'connecting':
                    self._attempts.add(attempt)
                    self.record['transport_attempt_count'] = len(self._attempts)
                    self.record['transport_attempts_state'] = 'observed'
                changed = True
        identifier = safe_identifier(fields.get('response_id'), self.secret)
        if identifier and identifier not in self.record['provider_response_ids']:
            self.record['provider_response_ids'].append(identifier)
            self.record['response_id_state'] = 'available'
            changed = True
        usage = numeric_usage(fields.get('model_usage'))
        if usage:
            signature = json.dumps(usage, sort_keys=True)
            if signature not in self._usage:
                self._usage.add(signature)
                self.record['usage_observations'].append(usage)
                self.record['usage'] = usage
                self.record['usage_state'] = 'available' if len(self._usage) == 1 else 'conflict'
                changed = True
        if changed:
            self._save()

    def http_status(self, code):
        if type(code) is int and 100 <= code <= 599:
            self.record['http_status'] = code
            self.record['http_status_state'] = 'available'
            if code not in self.record['http_statuses']:
                self.record['http_statuses'].append(code)
            self._save()

    def bind_images(self, bindings):
        # Only provenance and digests: never image base64, prompts or credentials.
        self.record['image_inputs'] = bindings
        self._save()
        if self.storage_state != 'recorded':
            raise RuleModelEvidenceError()

    def image_input_failed(self, error):
        self.record.update(request_sent=False,billing_state='NO_CHARGE',
                           amount_state='LOCAL_INPUT_REJECTED_BEFORE_MODEL_REQUEST',
                           image_input_error_type=type(error).__name__)
        response=getattr(error,'response',None)
        if response is not None:self.record['image_input_http_status']=response.status_code
        self._save()
        if self.storage_state != 'recorded':
            raise RuleModelEvidenceError()

    def finish(self, outcome, error_type=None):
        self.record['outcome'] = outcome if outcome in ('success', 'failed') else 'failed'
        self.record['finished_at'] = time.time()
        if isinstance(error_type, str) and re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]{0,99}', error_type):
            self.record['error_type'] = error_type
        self._save()

    def __exit__(self, kind, error, traceback):
        self.finish('failed' if kind else 'success', kind.__name__ if kind else None)
        if self.storage_state != 'recorded':
            _FAILED_ROOTS.add(self._root)
            if error is not None:
                error.add_note('MODEL_EVIDENCE_WRITE_FAILED')
            else:
                raise RuleModelEvidenceError()
        return False
