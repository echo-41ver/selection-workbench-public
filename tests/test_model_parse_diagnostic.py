"""Fake responses only: strict parse failures remain failures with bounded evidence."""
import importlib.util
import hashlib
import io
import json
from pathlib import Path
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))


class Response(io.BytesIO):
    status = 200

    def __init__(self, body, content_type='application/json'):
        super().__init__(body)
        self.headers = {'Content-Type': content_type}


def generator_module():
    spec = importlib.util.spec_from_file_location('parse_diagnostic_generate', ROOT / 'scripts/generate_category.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def invoke(monkeypatch, tmp_path, body, *, content_type='application/json', protocol='chat_completions'):
    generator = generator_module()
    monkeypatch.setattr(generator, 'ROOT', tmp_path)
    monkeypatch.setenv('DEEPSEEK_API_KEY', 'TEST_ONLY_CURRENT_PRIVATE_KEY')
    monkeypatch.setenv('SELECTION_MODEL_DRAFT_CONTEXT', 'category-' + 'f' * 32)
    if protocol == 'responses':
        monkeypatch.setenv('SELECTION_RULES_BASE_URL', 'https://api.openai.com/v1/responses')
        monkeypatch.setenv('SELECTION_RULES_MODEL', 'gpt-test')
    else:
        monkeypatch.delenv('SELECTION_RULES_BASE_URL', raising=False)
        monkeypatch.delenv('SELECTION_RULES_MODEL', raising=False)
    calls = []

    def fake_open(*args, **kwargs):
        calls.append(True)
        return Response(body, content_type)

    monkeypatch.setattr(generator, 'open_model_url', fake_open)
    monkeypatch.setattr('socket.create_connection', lambda *a, **k: pytest.fail('network forbidden'))
    with pytest.raises(generator.CategoryGenerationError) as caught:
        generator.model_json('TEST_ONLY_PROMPT_NEVER_SAVE')
    assert caught.value.code == 'MODEL_INVALID_JSON'
    assert len(calls) == 1
    evidence = list((tmp_path / 'runtime/model-call-evidence').glob('*.json'))
    assert len(evidence) == 1
    ledger = json.loads(evidence[0].read_text('utf-8'))
    assert ledger['outcome'] == 'failed'
    assert ledger['billing_state'] == 'UNKNOWN'
    return caught.value, ledger


def packet(content):
    return json.dumps({'id': 'fixture-response', 'choices': [
        {'finish_reason': 'stop', 'message': {'content': content}}]}).encode()


def diagnostic(tmp_path):
    files = list((tmp_path / 'runtime/model-parse-diagnostics').glob('*.json'))
    assert len(files) == 1
    return files[0], json.loads(files[0].read_text('utf-8'))


def test_content_json_failure_records_private_bounded_diagnostic(monkeypatch, tmp_path):
    error, ledger = invoke(monkeypatch, tmp_path, packet('{"rule":,}'))
    path, record = diagnostic(tmp_path)
    assert record['branch'] == 'content_json'
    assert record['logical_call_id'] == ledger['logical_call_id'] == path.stem
    assert record['finish_reason'] == 'stop'
    assert record['json_error'] == {'line': 1, 'column': 9, 'position': 8, 'reason': 'Expecting value'}
    assert record['source']['characters'] == 10
    assert len(record['source']['sha256']) == 64
    assert len(record['source']['excerpt']) <= 160
    assert path.stem in str(error)
    assert 'source' not in ledger and 'diagnostic' not in ledger
    assert 'TEST_ONLY_PROMPT_NEVER_SAVE' not in path.read_text('utf-8')


@pytest.mark.parametrize('result,field,actual_type', [
    ([], 'result', 'array'), ({}, 'choices', 'null'), ({'choices': []}, 'choices', 'array'),
    ({'choices': [None]}, 'choices[0]', 'null'),
    ({'choices': [{'finish_reason': 'stop'}]}, 'message', 'null'),
    ({'choices': [{'finish_reason': 'stop', 'message': {}}]}, 'content', 'missing'),
])
def test_result_envelope_failure_names_only_the_structural_field(monkeypatch, tmp_path, result, field, actual_type):
    invoke(monkeypatch, tmp_path, json.dumps(result).encode())
    _, record = diagnostic(tmp_path)
    assert record['branch'] == 'result_envelope'
    assert (record['field'], record['actual_type']) == (field, actual_type)
    assert 'source' not in record


@pytest.mark.parametrize('content,actual_type', [(None, 'null'), ([], 'array'), ({'private': 'value'}, 'object'), (42, 'integer')])
def test_non_text_content_remains_a_failure(monkeypatch, tmp_path, content, actual_type):
    invoke(monkeypatch, tmp_path, packet(content))
    _, record = diagnostic(tmp_path)
    assert record['branch'] == 'content_type'
    assert record['actual_type'] == actual_type
    assert record['finish_reason'] == 'stop'
    assert 'source' not in record and 'private' not in json.dumps(record)


@pytest.mark.parametrize('content,actual_type', [('[]', 'array'), ('null', 'null'), ('true', 'boolean'), ('42', 'integer'), ('"text"', 'string')])
def test_valid_json_without_an_object_still_fails(monkeypatch, tmp_path, content, actual_type):
    invoke(monkeypatch, tmp_path, packet(content))
    _, record = diagnostic(tmp_path)
    assert record['branch'] == 'parsed_non_object'
    assert record['actual_type'] == actual_type
    assert record['source']['sha256'] == hashlib.sha256(content.encode()).hexdigest()


@pytest.mark.parametrize('protocol', ['chat_completions', 'responses'])
def test_stream_packet_json_is_distinct_from_model_content(monkeypatch, tmp_path, protocol):
    invoke(monkeypatch, tmp_path, b'data: {"broken":,}\n\n', content_type='text/event-stream', protocol=protocol)
    _, record = diagnostic(tmp_path)
    assert record['branch'] == 'stream_packet_json'
    assert record['protocol'] == protocol
    assert record['json_error']['position'] == 10


def test_invalid_outer_json_has_a_separate_branch(monkeypatch, tmp_path):
    invoke(monkeypatch, tmp_path, b'{"broken":,}')
    _, record = diagnostic(tmp_path)
    assert record['branch'] == 'response_envelope_json'


def test_stream_diagnostic_keeps_already_observed_finish_and_safe_response_id(monkeypatch, tmp_path):
    first = {'id': 'fixture-stream-safe', 'choices': [
        {'index': 0, 'delta': {'content': '{}'}, 'finish_reason': 'stop'}]}
    body = ('data: ' + json.dumps(first) + '\n\ndata: {"broken":,}\n\n').encode()
    invoke(monkeypatch, tmp_path, body, content_type='text/event-stream')
    _, record = diagnostic(tmp_path)
    assert record['branch'] == 'stream_packet_json'
    assert record['finish_reason'] == 'stop'
    assert record['provider_response_ids'] == ['fixture-stream-safe']


def test_fragment_redacts_credentials_before_using_original_position(monkeypatch, tmp_path):
    key = 'TEST_ONLY_CURRENT_PRIVATE_KEY'
    short_bearer = 'sampleBearer'
    query_secret = 'shortQuery'
    userinfo = 'user:pass'
    value = 'Bearer ' + short_bearer + ' https://' + userinfo + '@fixture.invalid/?auth=' + query_secret + ' ' + key
    content = '{"data":' + value + '}'
    error, ledger = invoke(monkeypatch, tmp_path, packet(content))
    path, record = diagnostic(tmp_path)
    serialized = path.read_text('utf-8')
    for private in (key, short_bearer, query_secret, userinfo, 'TEST_ONLY_PROMPT_NEVER_SAVE'):
        assert private not in serialized
        assert private not in str(error)
        assert private not in json.dumps(ledger)
    assert len(record['source']['excerpt']) <= 160
    assert record['source']['sha256'] == hashlib.sha256(content.encode()).hexdigest()
    assert '[已隐藏]' in record['source']['excerpt']


@pytest.mark.parametrize('private', ['TEST_ONLY_CURRENT_PRIVATE_KEY', 'x' * 32])
def test_redaction_masks_tokens_crossing_the_excerpt_boundary(private):
    from selection_automation.model_parse_diagnostic import _excerpt
    source = '填' * 100 + private + '填' * 300
    start, excerpt = _excerpt(source, 200, 'TEST_ONLY_CURRENT_PRIVATE_KEY')
    assert start == 120
    assert private not in excerpt and private[20:] not in excerpt
    assert '[已隐藏]' in excerpt
    assert len(excerpt) <= 160


def test_escaped_exact_key_and_generic_json_secret_are_redacted():
    from selection_automation.model_parse_diagnostic import _excerpt
    key = 'short"key\\value'
    private = 'short-value'
    source = json.dumps({'data': key, 'api_key': private})
    _, excerpt = _excerpt(source, 0, key)
    assert json.dumps(key)[1:-1] not in excerpt
    assert private not in excerpt


@pytest.mark.parametrize('authorization,private', [
    ('Basic ' + 'dTpw', 'dTpw'),
    ('Bearer ' + 'tinyValue', 'tinyValue'),
    ('eyJx' + '.e30.sig', 'eyJx' + '.e30.sig'),
])
def test_explicit_short_basic_bearer_and_jwt_are_redacted(authorization, private):
    from selection_automation.model_parse_diagnostic import _excerpt
    source = '填' * 100 + authorization + '填' * 200
    _, excerpt = _excerpt(source, 180, '')
    assert private not in excerpt
    assert '[已隐藏]' in excerpt


def test_diagnostic_write_failure_is_visible_without_replacing_business_error(monkeypatch, tmp_path):
    (tmp_path / 'runtime').mkdir()
    (tmp_path / 'runtime/model-parse-diagnostics').write_text('blocked', encoding='utf-8')
    error, ledger = invoke(monkeypatch, tmp_path, packet('{"rule":,}'))
    assert '诊断保存失败' in str(error)
    assert error.code == 'MODEL_INVALID_JSON' and ledger['outcome'] == 'failed'
    assert 'TEST_ONLY_CURRENT_PRIVATE_KEY' not in str(error)


def test_diagnostic_outside_root_link_is_refused(monkeypatch, tmp_path):
    from selection_automation.model_parse_diagnostic import ModelParseFailure, save_parse_diagnostic
    outside = tmp_path / 'outside'
    outside.mkdir()
    root = tmp_path / 'root'
    (root / 'runtime').mkdir(parents=True)
    link = root / 'runtime/model-parse-diagnostics'
    try:
        link.symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip('Directory symlink permission unavailable')
    with pytest.raises(OSError, match='OUTSIDE_ROOT'):
        save_parse_diagnostic(root, ModelParseFailure('content_json'), {'logical_call_id': 'a' * 32})
    assert list(outside.iterdir()) == []


def test_large_invalid_output_diagnostic_is_bounded(monkeypatch, tmp_path):
    from selection_automation.model_parse_diagnostic import MAX_SOURCE_CHARACTERS
    invoke(monkeypatch, tmp_path, packet('x' * (MAX_SOURCE_CHARACTERS + 1)))
    path, record = diagnostic(tmp_path)
    assert record['source'] == {'characters': MAX_SOURCE_CHARACTERS + 1, 'omitted': 'diagnostic_size_limit',
                               'sha256': hashlib.sha256(('x' * (MAX_SOURCE_CHARACTERS + 1)).encode()).hexdigest(),
                               'sha256_encoding': 'utf-8-surrogatepass'}
    assert path.stat().st_size < 4096


def test_valid_json_object_is_unchanged_and_creates_no_parse_artifact(monkeypatch, tmp_path):
    generator = generator_module()
    monkeypatch.setattr(generator, 'ROOT', tmp_path)
    monkeypatch.setenv('DEEPSEEK_API_KEY', 'TEST_ONLY_CURRENT_PRIVATE_KEY')
    monkeypatch.setattr(generator, 'open_model_url', lambda *a, **k: Response(packet('{"ok":true}')))
    assert generator.model_json('TEST_ONLY_PROMPT_NEVER_SAVE') == {'ok': True}
    assert not (tmp_path / 'runtime/model-parse-diagnostics').exists()
