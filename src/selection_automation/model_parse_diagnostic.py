"""Bounded private diagnostics for strict rule-response parsing, never prompts or headers."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import time
import uuid

from .rule_model_evidence import safe_identifier

MAX_SOURCE_CHARACTERS = 2 * 1024 * 1024
MAX_EXCERPT_CHARACTERS = 160
_SECRET_NAMES = r'(?:api[_-]?key|access[_-]?token|refresh[_-]?token|token|authorization|password|passwd|secret|credential|cookie|device[_-]?code|login[_-]?code|ticket|signature)'
_PRIVATE_SPANS = (
    re.compile(r'(?i)(?<![A-Za-z0-9_])(?:Bearer|Basic)\s+[^\s"\'<>\\,;}\]]+'),
    re.compile(r'(?<![A-Za-z0-9_-])[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+(?![A-Za-z0-9_-])'),
    re.compile(r'(?i)\b(?:sk|rk|pk|api|key|token)[-_][A-Za-z0-9_.+/=-]{4,}'),
    re.compile(r'(?i)["\']?' + _SECRET_NAMES + r'["\']?\s*[:=]\s*(?:"(?:\\.|[^"\\])*(?:"|$)|\'[^\']*(?:\'|$)|[^\s,;}\]]+)'),
    re.compile(r'(?i)https?:(?:\\?/){2}[^/\s"<>]*@'),
    re.compile(r'[?&][A-Za-z0-9_.%~-]+=[^&\s"\'<>\\]*'),
    re.compile(r'[A-Za-z0-9_+/=-]{20,}'),
)
_JSON_REASONS = frozenset(('Expecting value', 'Extra data', 'Unterminated string starting at',
    'Expecting property name enclosed in double quotes', "Expecting ',' delimiter", "Expecting ':' delimiter",
    'Invalid control character at', 'Invalid \\escape', 'Invalid \\uXXXX escape'))


def type_name(value):
    return {dict: 'object', list: 'array', str: 'string', int: 'integer', float: 'number',
            bool: 'boolean', type(None): 'null', bytes: 'bytes', bytearray: 'bytes'}.get(type(value), 'other')


class ModelParseFailure(ValueError):
    def __init__(self, branch, *, source=None, error=None, field=None, actual_type=None, finish_reason=None):
        super().__init__(branch)
        self.branch, self.source, self.error = branch, source, error
        self.field, self.actual_type, self.finish_reason = field, actual_type, finish_reason


def parse_json(source, branch, *, finish_reason=None):
    try:
        return json.loads(source)
    except json.JSONDecodeError as error:
        raise ModelParseFailure(branch, source=error.doc, error=error, finish_reason=finish_reason) from None


def read_response_json(response):
    try:
        return json.load(response)
    except json.JSONDecodeError as error:
        raise ModelParseFailure('response_envelope_json', source=error.doc, error=error) from None


def response_choice(result):
    if not isinstance(result, dict):
        raise ModelParseFailure('result_envelope', field='result', actual_type=type_name(result))
    choices = result.get('choices')
    if not isinstance(choices, list) or not choices:
        raise ModelParseFailure('result_envelope', field='choices', actual_type=type_name(choices))
    if not isinstance(choices[0], dict):
        raise ModelParseFailure('result_envelope', field='choices[0]', actual_type=type_name(choices[0]))
    return choices[0]


def rule_object(choice):
    finish = choice.get('finish_reason')
    message = choice.get('message')
    if not isinstance(message, dict):
        raise ModelParseFailure('result_envelope', field='message', actual_type=type_name(message), finish_reason=finish)
    if 'content' not in message:
        raise ModelParseFailure('result_envelope', field='content', actual_type='missing', finish_reason=finish)
    content = message['content']
    if not isinstance(content, (str, bytes, bytearray)):
        raise ModelParseFailure('content_type', actual_type=type_name(content), finish_reason=finish)
    value = parse_json(content, 'content_json', finish_reason=finish)
    if not isinstance(value, dict):
        raise ModelParseFailure('parsed_non_object', source=content, actual_type=type_name(value), finish_reason=finish)
    return value


def _excerpt(text, position, secret):
    start = max(0, min(position, len(text)) - MAX_EXCERPT_CHARACTERS // 2)
    end = min(len(text), start + MAX_EXCERPT_CHARACTERS)
    spans = []
    for pattern in _PRIVATE_SPANS:
        spans.extend((m.start(), m.end()) for m in pattern.finditer(text) if m.end() > start and m.start() < end)
    if secret:
        for value in {secret, json.dumps(secret, ensure_ascii=False)[1:-1], json.dumps(secret)[1:-1]}:
            spans.extend((m.start(), m.end()) for m in re.finditer(re.escape(value), text)
                         if m.end() > start and m.start() < end)
    cursor, pieces = start, []
    for left, right in sorted(spans):
        if right <= cursor:
            continue
        if left > cursor:
            pieces.append(text[cursor:min(left, end)])
        pieces.append('[已隐藏]')
        cursor = min(right, end)
    if cursor < end:
        pieces.append(text[cursor:end])
    return start, ''.join(pieces)[:MAX_EXCERPT_CHARACTERS]


def save_parse_diagnostic(root, failure, evidence, *, secret='', protocol='unknown'):
    """Separate private artifact; the existing billing/evidence ledger stays metadata-only."""
    root = Path(root).resolve()
    folder = root / 'runtime/model-parse-diagnostics'
    if not folder.resolve().is_relative_to(root):
        raise OSError('DIAGNOSTIC_PATH_OUTSIDE_ROOT')
    call_id = evidence.get('logical_call_id')
    if not isinstance(call_id, str) or not re.fullmatch(r'[a-f0-9]{32}', call_id):
        raise ValueError('INVALID_DIAGNOSTIC_ID')
    record = {'schema': 1, 'diagnostic_id': call_id, 'logical_call_id': call_id,
              'created_at': time.time(), 'branch': failure.branch,
              'protocol': protocol if protocol in ('chat_completions', 'responses') else 'unknown',
              'provider_response_ids': [value for value in evidence.get('provider_response_ids', [])
                  if safe_identifier(value, secret)][:8]}
    context = evidence.get('draft_context')
    if isinstance(context, str) and re.fullmatch(r'category-[a-f0-9]{32}', context):
        record['draft_context'] = context
    if failure.field:
        record['field'] = failure.field
    if failure.actual_type:
        record['actual_type'] = failure.actual_type
    finish = failure.finish_reason
    record['finish_reason'] = finish if finish in ('stop', 'length', 'content_filter', 'tool_calls', 'function_call') else 'unavailable' if finish is None else 'other'
    if failure.error is not None:
        error = failure.error
        record['json_error'] = {'line': error.lineno, 'column': error.colno, 'position': error.pos,
                               'reason': error.msg if error.msg in _JSON_REASONS else 'JSON decode error'}
    text = failure.source
    if isinstance(text, (bytes, bytearray)):
        text = bytes(text).decode('utf-8', 'replace')
    if isinstance(text, str):
        fingerprint = hashlib.sha256()
        for offset in range(0, len(text), 65536):
            fingerprint.update(text[offset:offset + 65536].encode('utf-8', 'surrogatepass'))
        source = {'characters': len(text), 'sha256': fingerprint.hexdigest(),
                  'sha256_encoding': 'utf-8-surrogatepass'}
        if len(text) <= MAX_SOURCE_CHARACTERS:
            source['excerpt_start_position'], source['excerpt'] = _excerpt(text,
                failure.error.pos if failure.error is not None else 0, secret)
        else:
            source['omitted'] = 'diagnostic_size_limit'
        record['source'] = source
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / (call_id + '.json')
    temporary = folder / ('.' + call_id + '.' + uuid.uuid4().hex + '.tmp')
    try:
        with temporary.open('x', encoding='utf-8') as stream:
            json.dump(record, stream, ensure_ascii=True, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()
    return call_id
