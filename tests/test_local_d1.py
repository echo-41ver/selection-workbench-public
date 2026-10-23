"""Local native D1 tests: metadata GETs and mocked inference, no model load."""
import base64
from copy import deepcopy
import hashlib
import io
import json
from types import SimpleNamespace

from PIL import Image
import pytest

from selection_automation import local_d1 as d1, local_model_service as service
from selection_automation.stage3_provider import VisualClient, apply, decision_score, request_batch_size, settings


def metadata():
    return {'health': {'ready': True, 'model': d1.MODEL, 'revision': 'actual-revision'},
            'document': {'info': {'title': 'Local d1-3B Decision API'}, 'paths': {d1.PATH: {'post': {
                'requestBody': {'content': {'application/json': {'schema': {'$ref': '#/components/schemas/DecisionRequest'}}}}}}},
                'components': {'schemas': {'DecisionRequest': {'type': 'object', 'required': ['questions'], 'properties': {
                    'state': {'anyOf': [{'type': 'object'}, {'type': 'null'}]},
                    'questions': {'type': 'object', 'additionalProperties': True, 'minProperties': 1, 'maxProperties': 10},
                    'image_base64': {'anyOf': [{'type': 'string', 'maxLength': 8000000}, {'type': 'null'}]}}}}}}}


def provider():
    return settings({'mode': 'local_model', 'base_url': 'http://127.0.0.1:7863/v1/decide',
                     'model': d1.MODEL, 'protocol': 'decisions', 'workers': 1})


def image_url(color, size=(400, 240)):
    buffer = io.BytesIO()
    Image.new('RGB', size, color).save(buffer, format='PNG')
    return 'data:image/png;base64,' + base64.b64encode(buffer.getvalue()).decode('ascii')


def content():
    return [{'type': 'input_text', 'text': 'Amazon A1 / TEMU T1: actual source records'},
            {'type': 'input_image', 'image_url': image_url('red')},
            {'type': 'input_image', 'image_url': image_url('blue', (420, 300))}]


def criteria(count):
    return {'schema_version': 'amazon-match-criteria-v1', 'enabled': True, 'items': [
        {'id': f'condition_{i}', 'selected': True, 'kind': 'visual', 'label': f'条件{i}',
         'rule': f'完整视觉规则{i}不得删除'} for i in range(count)]}


def test_native_discovery_requires_health_and_exact_openapi_without_inference():
    data = metadata(); calls = []
    def get(base, path):
        calls.append((base, path))
        return data['health'] if path == '/health' else data['document']
    model = d1.discover('http://127.0.0.1:7863', get)
    assert calls == [('http://127.0.0.1:7863', '/health'), ('http://127.0.0.1:7863', '/openapi.json')]
    assert model['id'] == d1.MODEL and model['protocol'] == 'decisions'
    assert model['base_url'].endswith('/v1/decide') and model['vision'] is True
    assert model['inference_verified'] is False
    assert model['limits']['token_preflight'] == 'unavailable_server_enforced'


@pytest.mark.parametrize('change', ['not_ready', 'other_model', 'no_image', 'wrong_questions', 'wrong_title', 'remote_ref'])
def test_name_alone_or_other_contract_does_not_advertise_image_api(change):
    data = metadata(); request = data['document']['components']['schemas']['DecisionRequest']
    if change == 'not_ready': data['health']['ready'] = False
    if change == 'other_model': data['health']['model'] = 'some-d1-vision-name'
    if change == 'no_image': request['properties'].pop('image_base64')
    if change == 'wrong_questions': request['properties']['questions']['maxProperties'] = 20
    if change == 'wrong_title': data['document']['info']['title'] = 'Another API'
    if change == 'remote_ref':
        data['document']['paths'][d1.PATH]['post']['requestBody']['content']['application/json']['schema'] = {'$ref': 'https://example.invalid/schema'}
    assert d1.discover('http://127.0.0.1:7863', lambda base,path: data['health'] if path == '/health' else data['document']) is None


def test_discovery_catalog_and_custom_repair_never_start_or_query_inference(tmp_path, monkeypatch):
    data = metadata(); calls = []
    def get(base, path, payload=None):
        calls.append((path, payload))
        assert payload is None
        if path == '/api/tags': raise OSError('not Ollama')
        assert path in ('/health', '/openapi.json')
        return data['health'] if path == '/health' else data['document']
    monkeypatch.setattr(service, '_json', get)
    monkeypatch.setattr(service, '_endpoints', lambda: ['http://127.0.0.1:7863'])
    monkeypatch.setattr(service, '_start', lambda root: pytest.fail('D1 is already running'))
    original = provider(); before = deepcopy(original)
    result = service.repair_provider(tmp_path, original)
    assert original == before and result['provider'] == original
    assert '实际图片推理尚待验证' in result['message'] and result['service_connected']
    assert calls == [('/health', None), ('/openapi.json', None)]


def test_silent_non_http_port_is_not_probed_again_for_each_protocol(monkeypatch):
    calls = []
    def get(base, path):
        calls.append(path)
        raise service._UnavailableMetadata('silent TCP service')
    monkeypatch.setattr(service, '_json', get)
    assert service._probe('http://127.0.0.1:19999') is None
    assert calls == ['/api/tags']


def test_selected_local_endpoint_is_first_without_assuming_its_port(tmp_path, monkeypatch):
    seen = []; base = 'http://127.0.0.1:23456'
    model = d1.discover(base, lambda _,path: metadata()['health' if path == '/health' else 'document'])
    monkeypatch.setattr(service, '_endpoints', lambda: [f'http://127.0.0.1:{20000+i}' for i in range(80)])
    monkeypatch.setattr(service, '_probe_native', lambda base:None)
    def probe(address):
        seen.append(address)
        return {'base_url': address, 'source': d1.SOURCE, 'rows': [model]} if address == base else None
    monkeypatch.setattr(service, '_probe', probe)
    result = service.catalog(tmp_path, preferred=base + '/v1/decide')
    assert seen[0] == base and result['models'][0]['id'] == d1.MODEL
    assert not result['partial']


def test_legacy_sample_can_discover_native_but_custom_other_model_is_preserved(tmp_path, monkeypatch):
    model = d1.discover('http://127.0.0.1:7863', lambda base,path: metadata()['health' if path == '/health' else 'document'])
    monkeypatch.setattr(service, 'catalog', lambda *a,**k: {'models': [model], 'message': ''})
    original = settings({'mode': 'local_model'})
    result = service.repair_provider(tmp_path, original)
    assert result['provider']['model'] == d1.MODEL and result['provider']['protocol'] == 'decisions'
    custom = provider(); custom['model'] = 'custom-vision'
    assert service.repair_provider(tmp_path, custom)['provider'] is None


def test_two_images_are_full_resolution_in_labelled_png_and_recoverable():
    source = content(); before = deepcopy(source)
    client = VisualClient(provider(), None, '', 30)
    data = client.prepare(source, '全部身份规则\n不降低判断标准')
    payload = json.loads(data)
    assert set(payload) == {'state', 'questions', 'image_base64'}
    assert payload['state']['records'] == [source[0]['text']]
    assert 'LEFT / image 1 = Amazon' in payload['state']['image_layout']
    assert '全部身份规则\n不降低判断标准' in payload['questions']['same_product']['instructions']
    image = d1.prepared_image(data)
    evidence = client.last_prepare_metadata
    assert evidence['sha256'] == hashlib.sha256(image).hexdigest()
    with Image.open(io.BytesIO(image)) as picture:
        assert picture.format == 'PNG' and picture.size == (836, 348)
        for role, color, expected_size in [('amazon', (255, 0, 0), (400, 240)), ('temu', (0, 0, 255), (420, 300))]:
            item = next(item for item in evidence['sources'] if item['role'] == role)
            box = item['box']; pane = picture.crop(box)
            assert pane.size == expected_size and pane.getextrema() == tuple((x, x) for x in color)
        assert picture.crop((0, 0, picture.width, 48)).getbbox()  # visible header pixels remain
    assert source == before
    assert len(evidence['sources']) == 2


@pytest.mark.parametrize('count,limit', [(0, 10), (1, 5), (2, 3), (4, 2), (9, 1)])
def test_batch_limit_counts_whole_selected_visual_rules(count, limit):
    category = {'stage3': {'batch_size': 12}, 'match_criteria': criteria(count)}
    assert d1.question_batch_limit(category) == limit
    assert request_batch_size(provider(), category) == 1
    systemone = settings({'mode': 'local_model'})
    assert request_batch_size(systemone, category) == 12
    assert request_batch_size(settings({'mode': 'liquid_d1'}), category) == 1
    apply(category, provider())
    assert category['stage3']['batch_size'] == 1


def test_native_consumer_keeps_original_pairs_and_reuses_paid_checkpoint(tmp_path, monkeypatch):
    from contextlib import nullcontext
    from pathlib import Path
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
    import run_category_layer3 as runner
    from selection_automation import stage3_provider, stage3_transport, visual_examples, analysis_concurrency
    from selection_automation.vision import compatible_vision_keys

    amazon, temu = tmp_path / 'amazon.png', tmp_path / 'temu.png'
    amazon.write_bytes(base64.b64decode(content()[1]['image_url'].split(',', 1)[1]))
    temu.write_bytes(base64.b64decode(content()[2]['image_url'].split(',', 1)[1]))
    category = {'category_name_zh': '测试品类', 'stage3': {'modules': {'shape': 100}}, 'match_criteria': criteria(1)}
    category['match_criteria']['revision'] = 'fixture-complete-rule-revision'
    apply(category, settings({'mode': 'multimodal_api'}))
    pairs = [{'asin': 'A', 'goods_id': f'T{i:02}', 'pair_revision': f'p{i}',
              'amazon_title': 'Complete Amazon reference', 'temu_title': f'Complete TEMU candidate T{i:02}',
              'amazon_image_path': str(amazon), 'temu_image_path': str(temu)} for i in range(15)]
    prior = [{'asin': row['asin'], 'goods_id': row['goods_id'], 'pair_revision': row['pair_revision'],
              'vision_revision_key': compatible_vision_keys(category, row)[0], 'api_status': 'success',
              'attempt': 1, 'response_id': 'paid-api-response', 'usage': {'total_tokens': 100}}
             for row in pairs[:5]]
    results = tmp_path / 'results.jsonl'
    original = ''.join(json.dumps(row) + '\n' for row in prior).encode()
    results.write_bytes(original)
    ledger = tmp_path / 'requests.jsonl'
    paid_requests = ''.join(json.dumps({'request_id': f'paid-{i}', 'status': 'received', 'pairs': n}) + '\n'
                            for i, n in enumerate((3, 2))).encode()
    ledger.write_bytes(paid_requests)
    apply(category, provider())
    # An already saved native task can still have the old ten-pair batch value.
    category['stage3']['batch_size'] = 10
    failed = [{'asin': row['asin'], 'goods_id': row['goods_id'], 'pair_revision': row['pair_revision'],
               'vision_revision_key': compatible_vision_keys(category, row)[0], 'api_status': 'failed',
               'attempt': 1, 'failure_cause_types': ['LocalD1InputError'],
               'failure_reason': '双图 PNG 超过本机 D1 的 image_base64 长度限制；未发送请求',
               'billing_state': 'NO_CHARGE', 'response_id': None, 'usage': {}}
              for row in pairs[5:]]
    original += ''.join(json.dumps(row) + '\n' for row in failed).encode()
    results.write_bytes(original)
    paid_requests += (json.dumps({'request_id': 'local-input-error', 'status': 'local_error', 'pairs': 10,
                                 'billing_state': 'NO_CHARGE'}) + '\n').encode()
    ledger.write_bytes(paid_requests)
    candidate_file = tmp_path / 'candidates.jsonl'
    candidate_file.write_text(''.join(json.dumps(row) + '\n' for row in pairs), encoding='utf-8')
    calls = []
    class Transport:
        def post(self, address, **request):
            payload = json.loads(request['content'])
            assert address == provider()['base_url']
            assert set(payload) == {'state', 'questions', 'image_base64'}
            assert set(payload['questions']) == {'same_product', 'same_product__condition__condition_0'}
            assert '完整身份规则不得删除' in payload['questions']['same_product']['instructions']
            assert '完整视觉规则0不得删除' in payload['questions']['same_product__condition__condition_0']['instructions']
            assert len(payload['image_base64']) <= d1.MAX_IMAGE_BASE64
            with Image.open(io.BytesIO(base64.b64decode(payload['image_base64']))) as image:
                assert image.size == (836, 348) and image.format == 'PNG'
                assert image.crop((0, 48, 400, 288)).getextrema() == ((255, 255), (0, 0), (0, 0))
                assert image.crop((416, 48, 836, 348)).getextrema() == ((0, 0), (0, 0), (255, 255))
            records = payload['state']['records']
            record = json.loads(records[-1].split('\n', 1)[1])
            calls.append(record['product_id'])
            return SimpleNamespace(raise_for_status=lambda: None, json=lambda: {
                'answers': {key: {'choice': 'same', 'confidence': .95,
                                 'probabilities': {'same': .95, ('unknown' if '__condition__' in key else 'similar'): .03,
                                                   'different': .02}}
                            for key in payload['questions']}, 'usage': {'input_tokens': 50, 'output_tokens': 0}})
    monkeypatch.setattr(stage3_transport, 'create_transport', lambda *a, **k: nullcontext(Transport()))
    monkeypatch.setattr(stage3_provider, 'request_key', lambda *a: '')
    monkeypatch.setattr(analysis_concurrency, 'record_sample', lambda *a, **k: None)
    monkeypatch.setattr(visual_examples, 'example_content', lambda *a: [])
    monkeypatch.setattr(runner, 'load_category', lambda *a: category)
    monkeypatch.setattr(runner, 'protocol', lambda *a: ('完整身份规则不得删除', {}))
    monkeypatch.setattr(runner, 'score', decision_score)
    monkeypatch.setattr(runner, 'cached_image_url', lambda path:
                        'data:image/png;base64,' + base64.b64encode(Path(path).read_bytes()).decode())
    monkeypatch.setattr(runner, 'grid_data_url', lambda *a: pytest.fail('Native D1 must retain original single pairs'))
    args = SimpleNamespace(category=tmp_path / 'category.json', results=results,
                           validate_only=False, max_requests=0, max_pairs=0)
    runner.run(args, candidate_file)
    assert calls == [row['goods_id'] for row in pairs[5:]]
    saved = list(runner.rows(results))
    assert len(saved) == 25 and sum(row['api_status'] == 'success' for row in saved) == 15
    assert {row['api_status'] for row in saved[-10:]} == {'success'}
    assert {row['attempt'] for row in saved[-10:]} == {2}
    assert {row['goods_id'] for row in saved} == {row['goods_id'] for row in pairs}
    assert results.read_bytes().startswith(original) and ledger.read_bytes().startswith(paid_requests)
    assert category['stage3']['provider']['confidence'] == .9
    assert len(list(runner.rows(ledger))) == 13
    from selection_automation.stage3_billing import billing_hold
    hold = billing_hold(tmp_path / 'request_intents.jsonl', results)
    assert not hold['pairs'] and not hold['fail_closed']
    before = results.read_bytes(), ledger.read_bytes()
    runner.run(args, candidate_file)
    assert len(calls) == 10 and (results.read_bytes(), ledger.read_bytes()) == before


def test_native_image_limit_is_visible_without_showing_raw_errors_or_replaying(tmp_path, monkeypatch):
    from contextlib import nullcontext
    from pathlib import Path
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
    import run_category_layer3 as runner
    from watch_stage3 import failure_state
    from selection_automation import stage3_provider, stage3_transport, visual_examples, analysis_concurrency
    class Transport:
        def post(self, *a, **k):
            pytest.fail('Oversize image must not be sent')
    monkeypatch.setattr(stage3_transport, 'create_transport', lambda *a, **k: nullcontext(Transport()))
    monkeypatch.setattr(stage3_provider, 'request_key', lambda *a: '')
    monkeypatch.setattr(analysis_concurrency, 'record_sample', lambda *a, **k: None)
    monkeypatch.setattr(visual_examples, 'example_content', lambda *a: [])
    monkeypatch.setattr(runner, 'protocol', lambda *a: ('完整规则', {}))
    monkeypatch.setattr(runner, 'cached_image_url', lambda path: image_url('red' if path == 'amazon' else 'blue'))
    monkeypatch.setattr(d1, 'MAX_IMAGE_BASE64', 20)
    category = {'stage3': {'modules': {'shape': 100}}}
    apply(category, provider())
    pair = {'asin': 'A', 'goods_id': 'T', 'pair_revision': 'p', 'vision_revision_key': 'v', 'attempt': 1,
            'amazon_image_path': 'amazon', 'temu_image_path': 'temu'}
    args = SimpleNamespace(results=tmp_path / 'results.jsonl', validate_only=False, max_requests=0)
    with pytest.raises(SystemExit, match='没有远程费用'):
        runner.consume(args, category, tmp_path / 'blocked.json', 'fixture', iter([pair]))
    error_path = tmp_path / 'local_model_error.json'
    assert error_path.is_file()
    row = json.loads(error_path.read_text('utf-8'))
    assert row['code'] == d1.IMAGE_BASE64_LIMIT_CODE and row['model'] == d1.MODEL
    assert set(row) == {'code', 'time', 'request_id', 'model'}
    row['message'] = 'Bearer private-key arbitrary API body must never be displayed'
    error_path.write_text(json.dumps(row), encoding='utf-8')
    shown = failure_state(tmp_path, 0, provider())
    assert shown['error'] and shown['error_code'] == d1.IMAGE_BASE64_LIMIT_CODE
    assert '图片过大' in shown['phase'] and '未发送请求' in shown['phase'] and '分析方案后继续' in shown['phase']
    assert 'private-key' not in shown['phase'] and 'API body' not in shown['phase']
    for launched, target in [(row['time'] + 1, provider()), (0, {**provider(), 'model': 'another-model'}),
                             (0, settings({'mode': 'multimodal_api'}))]:
        assert 'error_code' not in failure_state(tmp_path, launched, target)
    request = list(runner.rows(tmp_path / 'requests.jsonl'))
    assert len(request) == 1 and request[0]['billing_state'] == 'NO_CHARGE'
    assert not (tmp_path / 'request_intents.jsonl').exists()


def test_single_pair_over_question_limit_is_clear_and_does_not_mutate_config_or_post():
    category = {'stage3': {}, 'match_criteria': criteria(10)}; before = deepcopy(category)
    with pytest.raises(d1.LocalD1InputError, match='单个商品对需要 11 题'):
        apply(category, provider())
    assert category == before
    transport = SimpleNamespace(post=lambda *a,**k: pytest.fail('No POST on preflight rejection'))
    client = VisualClient(provider(), transport, '', 30, criteria=criteria(10))
    with pytest.raises(d1.LocalD1InputError, match='本次需要 11 题'):
        client.create(input=[{'content': content()}], instructions='完整规则')


def test_native_question_batch_preserves_every_condition_and_candidate():
    client = VisualClient(provider(), None, '', 30, criteria=criteria(1))
    data = client.prepare(content(), '完整规则', candidate_ids=['C00', 'C01', 'C02', 'C03', 'C04'])
    questions = json.loads(data)['questions']
    assert len(questions) == 10 and set(questions) == {
        identity + suffix for identity in ['C00', 'C01', 'C02', 'C03', 'C04']
        for suffix in ['', '__condition__condition_0']}
    assert all('完整规则' in question['instructions'] for question in questions.values())
    assert all('完整视觉规则0不得删除' in questions[identity + '__condition__condition_0']['instructions']
               for identity in ['C00', 'C01', 'C02', 'C03', 'C04'])
    with pytest.raises(d1.LocalD1InputError, match='本次需要 12 题'):
        client.prepare(content(), '完整规则', candidate_ids=[f'C{i:02}' for i in range(6)])


def test_native_actual_answer_contract_reuses_existing_threshold_and_usage():
    requests = []
    probabilities = {'same': .95, 'similar': .03, 'different': .02}
    class Transport:
        def post(self, address, **request):
            requests.append((address, json.loads(request['content'])))
            return SimpleNamespace(raise_for_status=lambda: None, json=lambda: {
                'answers': {'same_product': {'choice': 'same', 'confidence': .95, 'probabilities': probabilities}},
                'usage': {'input_tokens': 912, 'output_tokens': 0}, 'elapsed_ms': 27})
    client = VisualClient(provider(), Transport(), '', 30)
    result = client.create(input=[{'content': content()}], instructions='完整规则')
    assert len(requests) == 1 and requests[0][0].endswith('/v1/decide')
    assert result.id is None and result.usage.model_dump() == {'input_tokens': 912, 'output_tokens': 0}
    facts = json.loads(result.output_text)['decision_facts']
    category = {'stage3': {'provider': provider(), 'modules': {'shape': 100}}}
    assert decision_score(category, facts)['state'] == '确认同款'
    category['stage3']['provider']['confidence'] = .99
    assert decision_score(category, facts)['state'] == '待补充证据'


def test_real_token_422_is_explicit_and_never_retried_or_truncated():
    calls = []
    class Transport:
        def post(self, address, **request):
            calls.append(json.loads(request['content']))
            def reject(): raise RuntimeError('HTTP422')
            return SimpleNamespace(status_code=422, raise_for_status=reject,
                                   json=lambda: {'detail': 'Text exceeds the local limit of 4096 tokens'})
    client = VisualClient(provider(), Transport(), '', 30)
    long_rule = '完整业务规则不可删减' * 1000
    with pytest.raises(RuntimeError) as caught:
        client.create(input=[{'content': content()}], instructions=long_rule)
    assert len(calls) == 1 and long_rule in calls[0]['questions']['same_product']['instructions']
    assert caught.value.status_code == 422
    import run_category_layer3 as runner
    diagnostic = runner.safe_provider_error(caught.value)
    assert '4096 tokens' in diagnostic['message'] and '不会自动重试' in diagnostic['message']
    assert diagnostic['code'] == 'LOCAL_D1_HTTP_422'


def test_fastapi_422_validation_input_and_credentials_are_not_in_safe_diagnostic():
    response = SimpleNamespace(status_code=422, json=lambda: {'detail': [
        {'loc': ['body', 'image_base64'], 'msg': 'String should have at most 8000000 characters',
         'input': 'private-complete-image-and-business-records', 'ctx': {'private': 'must not leak'}}]})
    message = d1.rejection_body(response)['error']['message']
    assert '8000000' in message and 'private' not in message and 'input' not in message
    response.json = lambda: {'detail': 'Bearer secret-value data:image/png;base64,sensitive-image'}
    message = d1.rejection_body(response)['error']['message']
    assert 'secret-value' not in message and 'sensitive-image' not in message


def test_image_limits_fail_locally_without_cropping_or_network(monkeypatch):
    with pytest.raises(d1.LocalD1InputError, match='不接受远程图片'):
        d1.compose_images(['https://example.invalid/private.png', image_url('blue')])
    large = image_url('white', (4000, 2000))
    with pytest.raises(d1.LocalD1InputError, match='未裁图、未发送请求'):
        d1.compose_images([large, large])
    monkeypatch.setattr(d1, 'MAX_IMAGE_BASE64', 20)
    with pytest.raises(d1.LocalD1InputError, match='image_base64'):
        d1.compose_images([image_url('red'), image_url('blue')])


def test_native_wrong_model_is_not_treated_as_compatible_systemone():
    value = provider(); value['model'] = 'd1-3b'
    client = VisualClient(value, None, '', 30)
    with pytest.raises(d1.LocalD1InputError, match='模型身份'):
        client.prepare(content(), '完整规则')
