"""Mock-only rule-model evidence tests: temporary files, no network or credentials."""
from __future__ import annotations

import ast
from contextlib import ExitStack
import importlib
import importlib.util
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
import uuid


# Synthetic credential-shaped fixtures use split literals below. Their runtime
# values still exercise strict redaction without shipping token-shaped source.
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))


class FakeModelResponse(io.BytesIO):
    def __init__(self, content, content_type='application/json'):
        super().__init__(content)
        self.status = 200
        self.headers = {'Content-Type': content_type,
                        'x-request-id': 'TEST_ONLY_HEADER_NOT_FOR_EVIDENCE'}


def sse_content(packets, *, done=True):
    body = ''.join('data: ' + json.dumps(packet) + '\n\n' for packet in packets)
    return (body + ('data: [DONE]\n\n' if done else '')).encode('utf-8')


class RuleModelEvidenceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.module = importlib.import_module('selection_automation.rule_model_evidence')

    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        directory = self.stack.enter_context(tempfile.TemporaryDirectory(prefix='mock-rule-evidence-only-'))
        # Windows CI may expose TEMP through an 8.3 alias (RUNNER~1). The
        # production recorder resolves its existing root before publishing.
        self.root = Path(directory).resolve()
        self.stack.enter_context(patch.dict(os.environ, {}, clear=True))
        for target in ('socket.create_connection', 'socket.socket', 'urllib.request.urlopen'):
            self.stack.enter_context(patch(target, side_effect=AssertionError('NETWORK_FORBIDDEN_IN_EVIDENCE_TEST')))

    def records(self):
        folder = self.root / 'runtime' / 'model-call-evidence'
        paths = sorted(folder.glob('*.json'))
        return [(path, json.loads(path.read_text(encoding='utf-8'))) for path in paths]

    def assert_record_contract(self, path, record):
        expected = {'schema', 'logical_call_id', 'entry', 'draft_context', 'started_at',
                    'finished_at', 'outcome', 'transport_attempt_count', 'events',
                    'provider_response_ids', 'usage', 'usage_state', 'billing_state',
                    'transport_attempts_state'}
        self.assertTrue(expected <= record.keys())
        uuid.UUID(record['logical_call_id'])
        self.assertEqual(path.stem, record['logical_call_id'])
        self.assertIsInstance(record['events'], list)
        self.assertIsInstance(record['provider_response_ids'], list)
        self.assertIsInstance(record['transport_attempts_state'], str)
        self.assertEqual(record['billing_state'], 'UNKNOWN')
        self.assertIsNotNone(record['started_at'])
        self.assertIsNotNone(record['finished_at'])
        json.dumps(record, allow_nan=False)

    def generation_module(self):
        spec = importlib.util.spec_from_file_location('mock_rule_model_generate_category', ROOT / 'scripts/generate_category.py')
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def run_model_wrapper(self, response=None, *, failure=None):
        """Exercise the real wrapper with a fake transport and an isolated root."""
        generator = self.generation_module()
        prompt, secret = 'TEST_ONLY_PRIVATE_PROMPT', ('sk-' + 'test-only-wrapper-private-key')
        payloads = []

        def open_fake(request, *, timeout, notify):
            self.assertEqual(timeout, 600)
            payloads.append(json.loads(request.data.decode('utf-8')))
            notify(model_network={'attempt': 1, 'route': 'direct', 'phase': 'connecting', 'request_sent': False})
            if failure is not None:
                raise failure
            notify(model_network={'attempt': 1, 'route': 'direct', 'phase': 'response_received'})
            return response

        before = {path for path, _ in self.records()}
        environment = {'DEEPSEEK_API_KEY': secret,
                       'SELECTION_MODEL_DRAFT_CONTEXT': 'category-' + 'c' * 32}
        with patch.object(generator, 'ROOT', self.root), \
                patch.object(generator, 'MODEL_PROGRESS_REPORTER', None), \
                patch.object(generator, 'open_model_url', side_effect=open_fake), \
                patch.dict(os.environ, environment):
            try:
                value, error = generator.model_json(prompt, max_tokens=41), None
            except generator.CategoryGenerationError as caught:
                value, error = None, caught
        added = [(path, record) for path, record in self.records() if path not in before]
        self.assertEqual(len(added), 1)
        path, record = added[0]
        self.assert_record_contract(path, record)
        self.assertEqual(record['transport_attempt_count'], 1)
        self.assertEqual(record['draft_context'], environment['SELECTION_MODEL_DRAFT_CONTEXT'])
        self.assertEqual(len(payloads), 1)
        payload = payloads[0]
        self.assertEqual(set(payload), {'model', 'messages', 'response_format', 'max_tokens', 'stream', 'thinking'})
        self.assertEqual(payload['messages'][-1], {'role': 'user', 'content': prompt})
        self.assertEqual(payload['response_format'], {'type': 'json_object'})
        self.assertEqual(payload['max_tokens'], 41)
        self.assertIs(payload['stream'], True)
        self.assertEqual(payload['thinking'], {'type': 'disabled'})
        self.assertNotIn('stream_options', payload)
        serialized = json.dumps(record)
        for private in (prompt, secret, 'TEST_ONLY_RESPONSE_BODY', 'TEST_ONLY_HEADER_NOT_FOR_EVIDENCE'):
            self.assertNotIn(private, serialized)
        return value, error, record

    def test_two_logical_calls_publish_distinct_complete_records_atomically(self):
        self.assert_two_calls_published_atomically(self.root)

    def test_windows_short_root_publishes_into_the_same_resolved_fixture(self):
        if os.name != 'nt':
            self.skipTest('Windows GetShortPathNameW requires Windows')
        import ctypes
        from ctypes import wintypes

        get_short_path = ctypes.WinDLL('kernel32', use_last_error=True).GetShortPathNameW
        get_short_path.argtypes = (wintypes.LPCWSTR, wintypes.LPWSTR, wintypes.DWORD)
        get_short_path.restype = wintypes.DWORD
        size = get_short_path(str(self.root), None, 0)
        if not size:
            self.skipTest(f'GetShortPathNameW unavailable for fixture: Windows error {ctypes.get_last_error()}')
        buffer = ctypes.create_unicode_buffer(size)
        written = get_short_path(str(self.root), buffer, size)
        self.assertGreater(written, 0)
        self.assertLess(written, size)
        short_root = Path(buffer.value)
        if str(short_root).casefold() == str(self.root).casefold():
            self.skipTest('Fixture volume does not provide a distinct Windows 8.3 alias')
        self.assertTrue(short_root.samefile(self.root))
        self.assertEqual(short_root.resolve(), self.root)
        # Exercise the actual recorder with the short spelling, while fixture
        # enumeration keeps the resolved spelling and all atomic assertions.
        self.assert_two_calls_published_atomically(short_root)

    def assert_two_calls_published_atomically(self, input_root):
        publications = []
        real_replace, real_rename = os.replace, os.rename

        def replacing(original):
            def replace(source, destination, *args, **kwargs):
                destination = Path(destination)
                if destination.parent.name == 'model-call-evidence' and destination.suffix == '.json':
                    payload = json.loads(Path(source).read_text(encoding='utf-8'))
                    self.assertIsInstance(payload, dict)
                    self.assertNotEqual(Path(source), destination)
                    publications.append(destination)
                return original(source, destination, *args, **kwargs)
            return replace

        with patch('os.replace', side_effect=replacing(real_replace)), \
                patch('os.rename', side_effect=replacing(real_rename)):
            for number in range(2):
                with self.module.RuleModelEvidence(input_root) as evidence:
                    evidence.observe(response_id='chatcmpl-test-' + str(number),
                                     model_usage={'prompt_tokens': 3, 'completion_tokens': 2, 'total_tokens': 5})

        records = self.records()
        self.assertEqual(len(records), 2)
        self.assertEqual(len({record['logical_call_id'] for _, record in records}), 2)
        self.assertTrue({path for path, _ in records} <= set(publications))
        self.assertEqual({path for path in (self.root / 'runtime' / 'model-call-evidence').iterdir()},
                         {path for path, _ in records})
        for path, record in records:
            self.assert_record_contract(path, record)
            self.assertEqual(record['outcome'], 'success')
            self.assertEqual(record['usage_state'], 'available')

    def test_failure_is_recorded_without_swallowing_or_exposing_exception(self):
        secret = 'TEST_ONLY_SECRET_EXCEPTION_TOKEN'
        with self.assertRaisesRegex(ValueError, secret):
            with self.module.RuleModelEvidence(self.root, secret=secret) as evidence:
                evidence.observe(model_network={'attempt': 1, 'route': 'direct',
                                                'phase': 'connecting', 'request_sent': False})
                raise ValueError('https://private.invalid/?key=' + secret)
        [(path, record)] = self.records()
        self.assert_record_contract(path, record)
        self.assertEqual(record['outcome'], 'failed')
        self.assertEqual(record['billing_state'], 'UNKNOWN')
        self.assertEqual(record['usage_state'], 'unavailable')
        serialized = json.dumps(record)
        self.assertNotIn(secret, serialized)
        self.assertNotIn('private.invalid', serialized)
        self.assertNotIn('https://', serialized)

    def test_network_attempts_are_counted_once_per_attempt_not_per_notification(self):
        with self.module.RuleModelEvidence(self.root) as evidence:
            for attempt, route, phase in ((1, 'direct', 'connecting'),
                                          (1, 'direct', 'connect_failed'),
                                          (2, 'system_proxy', 'connecting'),
                                          (2, 'system_proxy', 'response_received'),
                                          (2, 'system_proxy', 'response_received')):
                evidence.observe(model_network={'attempt': attempt, 'route': route,
                                                'phase': phase, 'request_sent': False})
            evidence.http_status(200)
        [(path, record)] = self.records()
        self.assert_record_contract(path, record)
        self.assertEqual(record['transport_attempt_count'], 2)
        serialized = json.dumps(record['events'])
        for phase in ('connecting', 'connect_failed', 'response_received'):
            self.assertIn(phase, serialized)

    def test_valid_draft_context_is_retained_and_traversal_context_is_rejected(self):
        context = 'category-' + 'a' * 32
        with patch.dict(os.environ, {'SELECTION_MODEL_DRAFT_CONTEXT': context}):
            with self.module.RuleModelEvidence(self.root):
                pass
        self.assertEqual(self.records()[0][1]['draft_context'], context)
        before = {path for path, _ in self.records()}
        for context in ('../../outside', 'category-' + '../' * 12,
                        'category-' + 'b' * 31, 'category-' + 'z' * 32,
                        'https://private.invalid/TEST_ONLY_CONTEXT_SECRET'):
            with self.subTest(context=context):
                with patch.dict(os.environ, {'SELECTION_MODEL_DRAFT_CONTEXT': context}):
                    with self.module.RuleModelEvidence(self.root):
                        pass
                added = [(path, record) for path, record in self.records() if path not in before]
                self.assertEqual(len(added), 1)
                path, record = added[0]
                self.assertIsNone(record['draft_context'])
                self.assertNotIn(context, json.dumps(record))
                self.assertEqual(path.parent, self.root / 'runtime' / 'model-call-evidence')
                before.add(path)

    def test_provider_metadata_keeps_only_safe_identifier_and_numeric_usage(self):
        packet = {'id': 'chatcmpl-test-safe',
                  'usage': {'prompt_tokens': 7, 'completion_tokens': 2, 'total_tokens': 9,
                            'Authorization': 'Bearer TEST_ONLY_HEADER_SECRET',
                            'prompt': 'TEST_ONLY_PROMPT_SECRET'},
                  'url': 'https://private.invalid/?key=TEST_ONLY_URL_SECRET',
                  'api_key': 'TEST_ONLY_API_SECRET',
                  'choices': [{'message': {'content': 'TEST_ONLY_RESPONSE_SECRET'}}]}
        metadata = self.module.provider_metadata(packet)
        self.assertEqual(metadata.get('response_id'), 'chatcmpl-test-safe')
        self.assertEqual(metadata.get('model_usage'), {'prompt_tokens': 7, 'completion_tokens': 2, 'total_tokens': 9})
        self.assertNotIn('TEST_ONLY_', json.dumps(metadata))
        invalid = self.module.provider_metadata({
            'id': 'https://private.invalid/?key=TEST_ONLY_ID_SECRET',
            'usage': {'prompt_tokens': True, 'completion_tokens': -1,
                      'total_tokens': float('inf'), 'input_tokens': 3, 'output_tokens': float('nan')}})
        self.assertEqual(invalid.get('model_usage'), {'input_tokens': 3})
        self.assertNotIn('TEST_ONLY_', json.dumps(invalid, allow_nan=False))
        self.assertNotIn('https://', json.dumps(invalid, allow_nan=False))

    def test_unknown_observer_fields_and_secret_identifier_are_not_persisted(self):
        secret = 'TEST_ONLY_OBSERVER_SECRET'
        with self.module.RuleModelEvidence(self.root, secret=secret) as evidence:
            evidence.observe(response_id=secret,
                             model_usage={'prompt_tokens': 4, 'nested': {'secret': secret}},
                             prompt=secret, content=secret, api_key=secret,
                             headers={'Authorization': 'Bearer ' + secret},
                             request_url='https://private.invalid/?token=' + secret,
                             model_network={'attempt': 1, 'route': 'direct', 'phase': 'connecting',
                                            'request_sent': False, 'url': secret})
        [(_, record)] = self.records()
        serialized = json.dumps(record)
        self.assertNotIn(secret, serialized)
        self.assertNotIn('private.invalid', serialized)
        self.assertNotIn('Authorization', serialized)

    def test_repeated_usage_is_not_summed_and_conflicting_usage_is_flagged(self):
        with self.module.RuleModelEvidence(self.root) as evidence:
            for _ in range(2):
                evidence.observe(response_id='chatcmpl-test-once',
                                 model_usage={'prompt_tokens': 5, 'completion_tokens': 2, 'total_tokens': 7})
        [(_, record)] = self.records()
        self.assertEqual(record['provider_response_ids'], ['chatcmpl-test-once'])
        self.assertEqual(record['usage'], {'prompt_tokens': 5, 'completion_tokens': 2, 'total_tokens': 7})
        self.assertEqual(record['usage_state'], 'available')
        old_paths = {path for path, _ in self.records()}
        with self.module.RuleModelEvidence(self.root) as evidence:
            evidence.observe(response_id='chatcmpl-test-conflict', model_usage={'total_tokens': 7})
            evidence.observe(response_id='chatcmpl-test-conflict', model_usage={'total_tokens': 8})
        [(_, conflict)] = [(path, row) for path, row in self.records() if path not in old_paths]
        self.assertEqual(conflict['usage_state'], 'conflict')
        self.assertEqual(conflict['billing_state'], 'UNKNOWN')

    def test_sse_preserves_text_and_notifies_response_id_and_usage(self):
        stream = importlib.import_module('selection_automation.model_stream')
        packets = [{'id': 'chatcmpl-test-sse', 'choices': [{'index': 0, 'delta': {'content': '{"ok":'}, 'finish_reason': None}]},
                   {'id': 'chatcmpl-test-sse', 'choices': [{'index': 0, 'delta': {'content': 'true}'}, 'finish_reason': 'stop'}]},
                   {'id': 'chatcmpl-test-sse', 'choices': [],
                    'usage': {'prompt_tokens': 6, 'completion_tokens': 3, 'total_tokens': 9}}]
        payload = ''.join('data: ' + json.dumps(packet) + '\n\n' for packet in packets) + 'data: [DONE]\n\n'
        response = io.BytesIO(payload.encode('utf-8'))
        response.headers = {'Content-Type': 'text/event-stream'}
        notifications = []
        result = stream.read_chat_completion(response, notify=lambda **fields: notifications.append(fields))
        self.assertEqual(result['choices'][0]['message']['content'], '{"ok":true}')
        self.assertEqual(result['choices'][0]['finish_reason'], 'stop')
        self.assertTrue(any(row.get('response_id') == 'chatcmpl-test-sse' for row in notifications))
        self.assertTrue(any(row.get('model_usage') == {'prompt_tokens': 6, 'completion_tokens': 3, 'total_tokens': 9}
                            for row in notifications))
        self.assertTrue(any(row.get('events') for row in notifications))

    def test_model_json_wrapper_records_actual_metadata_for_json_and_sse(self):
        usage = {'prompt_tokens': 8, 'completion_tokens': 4, 'total_tokens': 12}
        packet = {'id': 'chatcmpl-wrapper-json', 'usage': {**usage, 'prompt': 'TEST_ONLY_RESPONSE_BODY'},
                  'choices': [{'finish_reason': 'stop',
                               'message': {'content': '{"answer":"TEST_ONLY_RESPONSE_BODY"}'}}]}
        json_response = FakeModelResponse(json.dumps(packet).encode('utf-8'))
        packets = [{'id': 'chatcmpl-wrapper-sse', 'choices': [{'index': 0,
                    'delta': {'content': '{"answer":"TEST_ONLY_RESPONSE_BODY"}'}, 'finish_reason': 'stop'}]},
                   {'id': 'chatcmpl-wrapper-sse', 'choices': [], 'usage': usage}]
        sse_response = FakeModelResponse(sse_content(packets), 'text/event-stream')
        for response, response_id in ((json_response, 'chatcmpl-wrapper-json'),
                                      (sse_response, 'chatcmpl-wrapper-sse')):
            with self.subTest(response_id=response_id):
                value, error, record = self.run_model_wrapper(response)
                self.assertIsNone(error)
                self.assertEqual(value, {'answer': 'TEST_ONLY_RESPONSE_BODY'})
                self.assertEqual(record['outcome'], 'success')
                self.assertEqual(record['http_status'], 200)
                self.assertEqual(record['provider_response_ids'], [response_id])
                self.assertEqual(record['usage'], usage)
                self.assertEqual(record['usage_state'], 'available')
                self.assertEqual(record['billing_state'], 'UNKNOWN')
                self.assertTrue(response.closed)
        self.assertEqual(len(self.records()), 2)

    def test_model_json_http_failure_remains_failed_with_unknown_billing(self):
        failure = HTTPError(('https://private.invalid/?key=sk-' + 'test-only-wrapper-private-key'), 429,
                            'TEST_ONLY_PROVIDER_MESSAGE', {},
                            io.BytesIO(json.dumps({'error': {'message': ('sk-' + 'test-only-wrapper-private-key')}}).encode()))
        value, error, record = self.run_model_wrapper(failure=failure)
        self.assertIsNone(value)
        self.assertIsNotNone(error)
        self.assertEqual(error.code, 'MODEL_HTTP_429')
        self.assertEqual(record['outcome'], 'failed')
        self.assertEqual(record['http_status'], 429)
        self.assertEqual(record['billing_state'], 'UNKNOWN')
        self.assertEqual(record['usage_state'], 'unavailable')
        self.assertNotIn('private.invalid', json.dumps(record))

    def test_model_json_interrupted_sse_remains_failed_and_keeps_observed_id(self):
        response = FakeModelResponse(sse_content([{'id': 'chatcmpl-wrapper-interrupted',
            'choices': [{'index': 0, 'delta': {'content': '{"answer":"TEST_ONLY_RESPONSE_BODY"'},
                         'finish_reason': None}]}], done=False), 'text/event-stream')
        value, error, record = self.run_model_wrapper(response)
        self.assertIsNone(value)
        self.assertIsNotNone(error)
        self.assertEqual(error.code, 'MODEL_STREAM_INTERRUPTED')
        self.assertEqual(record['outcome'], 'failed')
        self.assertEqual(record['http_status'], 200)
        self.assertEqual(record['provider_response_ids'], ['chatcmpl-wrapper-interrupted'])
        self.assertEqual(record['billing_state'], 'UNKNOWN')
        self.assertEqual(record['usage_state'], 'unavailable')
        self.assertTrue(response.closed)

    def test_model_json_output_limit_remains_failed_even_with_usage_and_valid_json(self):
        response = FakeModelResponse(json.dumps({'id': 'chatcmpl-wrapper-length', 'usage': {'total_tokens': 41},
            'choices': [{'finish_reason': 'length', 'message': {'content': '{"answer":"TEST_ONLY_RESPONSE_BODY"}'}}]}).encode())
        value, error, record = self.run_model_wrapper(response)
        self.assertIsNone(value)
        self.assertIsNotNone(error)
        self.assertEqual(error.code, 'MODEL_OUTPUT_TRUNCATED')
        self.assertEqual(record['outcome'], 'failed')
        self.assertEqual(record['provider_response_ids'], ['chatcmpl-wrapper-length'])
        self.assertEqual(record['usage'], {'total_tokens': 41})
        self.assertEqual(record['usage_state'], 'available')
        self.assertEqual(record['billing_state'], 'UNKNOWN')

    def test_model_json_progress_filters_known_key_in_provider_ids_and_header(self):
        generator = self.generation_module()
        secret, prompt, body = 'TEST_ONLY_NONSK_PROVIDER_KEY', 'TEST_ONLY_PRIVATE_PROMPT', 'TEST_ONLY_BODY_NOT_METADATA'
        response = FakeModelResponse(json.dumps({'id': secret,
            'usage': {'prompt_tokens': 2, 'arbitrary': secret},
            'choices': [{'finish_reason': 'stop', 'message': {'content': json.dumps({'ok': True, 'text': body})}}]}).encode())
        response.headers['x-request-id'] = secret
        progress = []
        private_identifiers = (secret, 'BearerTEST_ONLY_PRIVATE_HEADER', ('sk-' + 'TEST_ONLY_PRIVATE_TOKEN'))

        def open_fake(request, *, timeout, notify):
            notify(model_network={'attempt': 1, 'route': 'direct', 'phase': 'connecting'})
            for identifier in private_identifiers:
                notify(response_id=identifier, model_request_id=identifier,
                       model_usage={'prompt_tokens': 2, 'arbitrary': secret,
                                    'request_url': 'https://private.invalid/TEST_ONLY_USAGE_URL'})
            notify(model_network={'attempt': 1, 'route': 'direct', 'phase': 'response_received'})
            return response

        with patch.object(generator, 'ROOT', self.root), \
                patch.object(generator, 'MODEL_PROGRESS_REPORTER', side_effect=lambda **fields: progress.append(fields)), \
                patch.object(generator, 'open_model_url', side_effect=open_fake), \
                patch.dict(os.environ, {'DEEPSEEK_API_KEY': secret}):
            result = generator.model_json(prompt, max_tokens=41)
        self.assertEqual(result, {'ok': True, 'text': body})
        [(_, record)] = self.records()
        for output in (progress, record):
            serialized = json.dumps(output)
            for private in (*private_identifiers, prompt, body, 'TEST_ONLY_USAGE_URL'):
                self.assertNotIn(private, serialized)
        self.assertEqual(record['provider_response_ids'], [])
        self.assertEqual(record['usage'], {'prompt_tokens': 2})
        self.assertTrue(any(row.get('model_usage') == {'prompt_tokens': 2} for row in progress))
        self.assertTrue(all(set(row['model_usage']) <= {'prompt_tokens'}
                            for row in progress if 'model_usage' in row))

    def test_web_console_start_passes_generated_output_stem_as_draft_context(self):
        """Inspect syntax only; never import web_console or launch its service."""
        tree = ast.parse((ROOT / 'scripts/web_console.py').read_text(encoding='utf-8-sig'))
        starts = [node for node in ast.walk(tree) if isinstance(node, ast.FunctionDef) and node.name == '_start']
        self.assertEqual(len(starts), 1)
        method = starts[0]
        assignments = [node for node in ast.walk(method) if isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Subscript) and isinstance(target.value, ast.Name) and target.value.id == 'env'
            and isinstance(target.slice, ast.Constant) and target.slice.value == 'SELECTION_MODEL_DRAFT_CONTEXT'
            for target in node.targets)]
        self.assertEqual(len(assignments), 1)
        assignment = assignments[0]
        self.assertIsInstance(assignment.value, ast.Attribute)
        self.assertEqual(assignment.value.attr, 'stem')
        self.assertIsInstance(assignment.value.value, ast.Name)
        self.assertEqual(assignment.value.value.id, 'output')
        output_definitions = [node for node in ast.walk(method) if isinstance(node, ast.Assign)
                              and any(isinstance(target, ast.Name) and target.id == 'output' for target in node.targets)
                              and node.lineno < assignment.lineno]
        self.assertEqual(len(output_definitions), 1)
        output_syntax = ast.unparse(output_definitions[0].value)
        self.assertIn('category-', output_syntax)
        self.assertIn('uuid.uuid4().hex', output_syntax)
        invokes = [node for node in ast.walk(method) if isinstance(node, ast.Call)
                   and isinstance(node.func, ast.Attribute) and node.func.attr == 'invoke'
                   and node.lineno > assignment.lineno]
        first_invoke = min(invokes, key=lambda node: node.lineno)
        self.assertIn('scripts/generate_category.py', ast.unparse(first_invoke))
        self.assertTrue(any(keyword.arg == 'env' and isinstance(keyword.value, ast.Name)
                            and keyword.value.id == 'env' for keyword in first_invoke.keywords))

    def test_initial_evidence_write_failure_prevents_model_transport(self):
        generator = self.generation_module()
        with patch.object(generator, 'ROOT', self.root), \
                patch.object(generator, 'MODEL_PROGRESS_REPORTER', None), \
                patch.object(generator, 'open_model_url') as transport, \
                patch.dict(os.environ, {'DEEPSEEK_API_KEY': ('sk-' + 'test-only-storage-key')}), \
                patch('os.replace', side_effect=OSError('TEST_ONLY_WRITE_FAILURE')):
            with self.assertRaises(self.module.RuleModelEvidenceError) as raised:
                generator.model_json('TEST_ONLY_PRIVATE_PROMPT', max_tokens=41)
        self.assertEqual(raised.exception.code, 'MODEL_EVIDENCE_WRITE_FAILED')
        self.assertIs(raised.exception.retryable, False)
        transport.assert_not_called()
        self.assertEqual(self.records(), [])

    def run_storage_failure_after_started(self, *, http_failure=False):
        generator = self.generation_module()
        replacements, transport_calls = [], []
        real_replace = os.replace

        def replace_once(source, destination, *args, **kwargs):
            replacements.append(destination)
            if len(replacements) > 1:
                raise OSError('TEST_ONLY_POST_REQUEST_WRITE_FAILURE')
            return real_replace(source, destination, *args, **kwargs)

        def open_fake(request, *, timeout, notify):
            transport_calls.append(request)
            notify(model_network={'attempt': 1, 'route': 'direct', 'phase': 'connecting'})
            if http_failure:
                raise HTTPError('https://private.invalid', 429, 'TEST_ONLY_HTTP_ERROR', {},
                                io.BytesIO(b'{"error":{"message":"rate limited"}}'))
            notify(model_network={'attempt': 1, 'route': 'direct', 'phase': 'response_received'})
            return FakeModelResponse(json.dumps({'id': 'chatcmpl-wrapper-storage', 'usage': {'total_tokens': 7},
                'choices': [{'finish_reason': 'stop', 'message': {'content': '{"answer":true}'}}]}).encode())

        with patch.object(generator, 'ROOT', self.root), \
                patch.object(generator, 'MODEL_PROGRESS_REPORTER', None), \
                patch.object(generator, 'open_model_url', side_effect=open_fake), \
                patch.dict(os.environ, {'DEEPSEEK_API_KEY': ('sk-' + 'test-only-storage-key')}), \
                patch('os.replace', side_effect=replace_once):
            with self.assertRaises(Exception) as raised:
                generator.model_json('TEST_ONLY_PRIVATE_PROMPT', max_tokens=41)
        self.assertEqual(len(transport_calls), 1)
        self.assertGreater(len(replacements), 1)
        [(_, record)] = self.records()
        self.assertEqual(record['outcome'], 'running')
        self.assertIsNone(record['finished_at'])
        self.assertEqual(record['billing_state'], 'UNKNOWN')
        self.assertNotIn('TEST_ONLY_PRIVATE_PROMPT', json.dumps(record))
        self.assertNotIn(('sk-' + 'test-only-storage-key'), json.dumps(record))
        return generator, raised.exception

    def test_post_request_write_failure_keeps_initial_unknown_intent_and_never_replays(self):
        _, error = self.run_storage_failure_after_started()
        self.assertIsInstance(error, self.module.RuleModelEvidenceError)
        self.assertEqual(error.code, 'MODEL_EVIDENCE_WRITE_FAILED')
        self.assertIs(error.retryable, False)

    def test_post_request_write_failure_preserves_original_http_error_with_fixed_note(self):
        generator, error = self.run_storage_failure_after_started(http_failure=True)
        self.assertIsInstance(error, generator.CategoryGenerationError)
        self.assertEqual(error.code, 'MODEL_HTTP_429')
        self.assertIn('MODEL_EVIDENCE_WRITE_FAILED', getattr(error, '__notes__', ()))

    def test_failed_evidence_latches_same_root_before_next_transport_even_after_disk_recovers(self):
        generator, first_error = self.run_storage_failure_after_started()
        self.assertIsInstance(first_error, self.module.RuleModelEvidenceError)
        # The helper already proved exactly one first transport. Atomic writes
        # are restored here; the second logical call must still stop at entry.
        with patch.object(generator, 'ROOT', self.root), \
                patch.object(generator, 'MODEL_PROGRESS_REPORTER', None), \
                patch.object(generator, 'open_model_url') as second_transport, \
                patch.dict(os.environ, {'DEEPSEEK_API_KEY': ('sk-' + 'test-only-storage-key')}):
            with self.assertRaises(self.module.RuleModelEvidenceError) as raised:
                generator.model_json('TEST_ONLY_SECOND_PROMPT', max_tokens=41)
        second_transport.assert_not_called()
        self.assertEqual(raised.exception.code, 'MODEL_EVIDENCE_WRITE_FAILED')
        self.assertIs(raised.exception.retryable, False)
        [(_, record)] = self.records()
        self.assertEqual(record['outcome'], 'running')
        self.assertEqual(record['billing_state'], 'UNKNOWN')


if __name__ == '__main__':
    unittest.main()
