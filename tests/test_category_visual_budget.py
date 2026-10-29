"""Billed Responses truncation must retain usage and respect request limits."""
from offline_visual_api import grid
import json
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest
import openai

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import run_category_layer3 as runner


def test_budget_reserves_reasoning_and_scales_with_visual_work():
    stage={'modules': {str(i):1 for i in range(6)}}
    assert runner.output_token_budget(stage, 1)==16384
    assert runner.output_token_budget(stage, 3)>runner.output_token_budget(stage, 1)
    assert runner.output_token_budget(stage, 12)==54272
    assert runner.output_token_budget({**stage,'max_output_tokens':12000},1)==16384
    assert runner.output_token_budget({**stage,'max_output_tokens':4096},1)==16384
    assert runner.output_token_budget({**stage,'max_output_tokens':24000},1)==24000
    assert runner.output_token_budget({**stage,'max_output_tokens':16384},12)==54272
    assert runner.output_token_budget({**stage,'max_output_tokens':4096},3)>16384


def test_inventory_groups_get_room_without_exceeding_provider_limit():
    stage={'modules':{str(i):1 for i in range(6)},'max_output_tokens':16384}
    inventory={**stage,'set_inventory_features':{'knife_set_composition':['chef','bread']}}
    assert runner.output_token_budget(inventory,2)>runner.output_token_budget(stage,2)
    assert runner.output_token_budget(inventory,12)==65536
    assert runner.output_token_budget({**inventory,'max_output_tokens':65536},12)==65536


def test_runner_uses_sixty_four_workers_without_silent_clamping(offline_consumer,monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    widths=[]
    def pool(*args,**kwargs):
        widths.append(kwargs['max_workers'])
        return ThreadPoolExecutor(*args,**kwargs)
    monkeypatch.setattr(runner,'ThreadPoolExecutor',pool)
    offline_consumer({'max_concurrency':64})
    assert widths==[64]


@pytest.mark.parametrize('count',[1,2])
def test_request_contains_one_amazon_record_and_each_bound_temu_record(tmp_path,monkeypatch,count):
    calls=[]
    class Client:
        def __init__(self,**kw):self.responses=self
        def __enter__(self):return self
        def __exit__(self,*a):pass
        def create(self,**kw):
            calls.append(kw)
            return SimpleNamespace(id='r',status='completed',output_text='{}',usage=None)
    monkeypatch.setenv('DEEPSEEK_API_KEY','offline')
    monkeypatch.setattr(openai,'OpenAI',Client)
    monkeypatch.setattr(runner,'_data_url',lambda p:'data:image/png;base64,'+Path(p).name)
    monkeypatch.setattr(runner,'grid_data_url',grid)
    monkeypatch.setattr(runner,'protocol',lambda c,n:('fixture',{}))
    monkeypatch.setattr(runner,'decode',lambda c,raw,ids:{i:{'overall_confidence':.9} for i in ids})
    monkeypatch.setattr(runner,'score',lambda c,f:{'hard_conflicts':[]})
    cat={'stage3':{'modules':{'body':100},'batch_size':12,'max_concurrency':1}}
    rows=[{'asin':'A','goods_id':str(i),'amazon_image_path':'a','temu_image_path':str(i),
           'amazon_title':'Amazon 3 knives','temu_title':'TEMU set '+str(i),
           'pair_revision':str(i),'vision_revision_key':str(i),'attempt':1} for i in range(count)]
    runner.consume(SimpleNamespace(validate_only=False,max_requests=1,results=tmp_path/'results.jsonl'),cat,tmp_path/'blocked.json','offline',iter(rows))
    content=calls[0]['input'][0]['content']
    blocks=[b['text'] for b in content if b['type']=='input_text']
    assert len(blocks)==count+2
    assert blocks[1].startswith('TEMU 编号拼图' if count>1 else 'TEMU候选 C00 原图')
    assert blocks[0].startswith('Amazon基准') and 'Amazon 3 knives' in blocks[0]
    assert 'a 仅描述此 Amazon 图' in blocks[0] and 't 仅描述当前 Cxx TEMU' in blocks[0]
    assert '不可交换来源' in blocks[0]
    for index in range(count):
        assert blocks[index+2].startswith(f'C{index:02}') and f'TEMU set {index}' in blocks[index+2]
    assert [b['image_url'] for b in content if b['type']=='input_image']==[
        'data:image/png;base64,a','data:image/png;base64,fixture' if count>1 else 'data:image/png;base64,0']


@pytest.mark.parametrize('invalid',[True,0,65537,'8192'])
def test_invalid_override_rejected(invalid):
    with pytest.raises(ValueError):
        runner.output_token_budget({'modules':{'a':100},'max_output_tokens':invalid},1)


def test_exception_cause_diagnostics_do_not_include_sensitive_messages():
    try:
        try:raise OSError('secret-key-value')
        except OSError as e:raise RuntimeError('Authorization secret-key-value') from e
    except RuntimeError as e:
        result=runner.safe_exception_causes(e)
    assert result==['RuntimeError','OSError']
    assert 'secret-key-value' not in json.dumps(result)


def test_transport_unknown_isolated_without_stopping_next_unrequested_batch(offline_consumer,tmp_path):
    def respond(number,kwargs):
        if number==1:raise TimeoutError('offline')
        return '{}'
    calls,ledger,results=offline_consumer({'batch_size':1,'max_concurrency':1},count=3,responder=respond)
    assert len(calls)==3
    assert [r['api_status'] for r in results]==['failed','success','success']
    assert results[0]['billing_state']=='UNKNOWN'
    assert len(runner.unresolved_request_intents(tmp_path/'request_intents.jsonl'))==1


@pytest.mark.parametrize('value',[True,29,1801,'600'])
def test_request_timeout_configuration_rejects_invalid_values(value):
    with pytest.raises(ValueError,match='request_timeout_seconds'):
        runner.request_timeout_seconds({'request_timeout_seconds':value})


def test_request_timeout_default_and_configuration_boundaries():
    assert runner.request_timeout_seconds({})==600
    assert runner.request_timeout_seconds({'request_timeout_seconds':30})==30
    assert runner.request_timeout_seconds({'request_timeout_seconds':1800})==1800


def test_incomplete_response_records_paid_usage_without_retry(tmp_path,monkeypatch):
    monkeypatch.setenv('DEEPSEEK_API_KEY','offline-fixture')
    monkeypatch.setattr(runner,'_data_url',lambda path:'data:image/png;base64,fixture')
    monkeypatch.setattr(runner,'grid_data_url',grid)
    calls=[]
    class FakeClient:
        def __init__(self,**kwargs):
            assert kwargs['max_retries']==0
            self.responses=self
        def __enter__(self): return self
        def __exit__(self,*args): pass
        def create(self,**kwargs):
            calls.append(kwargs)
            usage={'output_tokens':8192,'output_tokens_details':{'reasoning_tokens':8192},'total_tokens':8500}
            return SimpleNamespace(id='fixture-incomplete',status='incomplete',
                incomplete_details=SimpleNamespace(reason='max_output_tokens'),output_text='',
                usage=SimpleNamespace(model_dump=lambda:usage))
    monkeypatch.setattr(openai,'OpenAI',FakeClient)
    args=SimpleNamespace(validate_only=False,max_requests=1,results=tmp_path/'results.jsonl')
    cat={'category_name_zh':'测试新品类','stage3':{'modules':{'first':60,'second':40},'max_concurrency':1,'batch_size':1}}
    pending=iter([{'asin':'A','goods_id':str(i),'amazon_image_path':'a','temu_image_path':'t',
                   'pair_revision':'p','vision_revision_key':str(i),'attempt':1} for i in range(2)])
    with pytest.raises(SystemExit, match='不自动重复请求'):
        runner.consume(args,cat,tmp_path/'blocked.json','fixture',pending)
    assert len(calls)==1
    assert calls[0]['max_output_tokens']==16384
    ledger=list(runner.rows(tmp_path/'requests.jsonl'))
    assert len(ledger)==1
    assert ledger[0]['usage']['output_tokens_details']['reasoning_tokens']==8192
    assert ledger[0]['incomplete_reason']=='max_output_tokens'
    results=list(runner.rows(args.results))
    assert len(results)==1
    assert results[0]['api_status']=='failed'
    assert results[0]['failure_reason']=='VISUAL_RESPONSE_INCOMPLETE: max_output_tokens'
    assert results[0]['usage']['total_tokens']==8500
    assert results[0]['response_id']=='fixture-incomplete'
    saved=list(runner.rows(tmp_path/'response_errors.jsonl'))
    assert saved[0]['response_id']=='fixture-incomplete' and saved[0]['vision_revision_keys']==['0']
    assert saved[0]['output_text']==''
    assert ledger[0]['effort']=='low'
    assert results[0]['effort']=='low'


@pytest.fixture
def offline_consumer(tmp_path, monkeypatch):
    """Observe real request scheduling and bookkeeping without network calls."""
    monkeypatch.setenv('DEEPSEEK_API_KEY', 'offline-fixture')
    monkeypatch.setattr(runner, '_data_url', lambda path: 'data:image/png;base64,fixture')
    monkeypatch.setattr(runner,'grid_data_url',grid)
    monkeypatch.setattr(runner, 'protocol', lambda cat, count: ('fixture', {}))
    monkeypatch.setattr(runner, 'decode', lambda cat, data, ids: {
        identity: {'overall_confidence': data.get('confidence', .9)} for identity in ids
    })
    monkeypatch.setattr(runner, 'score', lambda cat, facts: {
        'hard_conflicts': ['fixture conflict'] if facts['overall_confidence'] < .65 else []
    })
    calls = []

    def run(stage, count=1, max_requests=10, responder=None):
        class FakeClient:
            def __init__(self, **kwargs):
                assert kwargs['max_retries'] == 0
                self.responses = self
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def create(self, **kwargs):
                calls.append(kwargs)
                output = responder(len(calls), kwargs) if responder else '{}'
                return SimpleNamespace(id=f'fixture-{len(calls)}', status='completed',
                    output_text=output, usage=SimpleNamespace(model_dump=lambda: {'total_tokens': 100}))
        monkeypatch.setattr(openai, 'OpenAI', FakeClient)
        args = SimpleNamespace(validate_only=False, max_requests=max_requests, results=tmp_path/'results.jsonl')
        cat = {'category_name_zh': '测试品类', 'stage3': {
            'modules': {'first': 100}, 'max_concurrency': 1, 'batch_size': 1, **stage}}
        pending = iter([{'asin': 'A', 'goods_id': str(i), 'amazon_image_path': 'a',
            'temu_image_path': f't{i}', 'pair_revision': 'p', 'vision_revision_key': str(i),
            'attempt': 1} for i in range(count)])
        runner.consume(args, cat, tmp_path/'blocked.json', 'fixture', pending)
        return calls, list(runner.rows(tmp_path/'requests.jsonl')), list(runner.rows(args.results))

    return run


@pytest.mark.parametrize('stage, expected', [({}, 'low'),
    ({'reasoning_effort': 'medium'}, 'medium'), ({'reasoning_effort': 'high'}, 'high')])
def test_initial_request_uses_configured_effort_and_records_it(offline_consumer, stage, expected):
    calls, ledger, results = offline_consumer(stage)
    assert [call['reasoning']['effort'] for call in calls] == [expected]
    assert ledger[0]['effort'] == expected
    assert results[0]['api_status'] == 'success'
    assert results[0]['effort'] == expected


@pytest.mark.parametrize('effort', ['medium', 'high'])
@pytest.mark.parametrize('limit', [2, 3])
def test_schema_split_inherits_effort_and_keeps_request_cap(offline_consumer, effort, limit):
    calls, ledger, results = offline_consumer(
        {'reasoning_effort': effort, 'batch_size': 2}, count=2, max_requests=limit,
        responder=lambda number, kwargs: '{invalid JSON' if number == 1 else '{}')
    assert len(calls) == limit
    assert [call['reasoning']['effort'] for call in calls] == [effort] * limit
    assert [row['pairs'] for row in ledger] == [2] + [1] * (limit - 1)
    assert [row['effort'] for row in ledger] == [effort] * limit
    assert len(results) == limit - 1
    assert all(row['api_status'] == 'success' and row['effort'] == effort for row in results)


@pytest.mark.parametrize('limit', [1, 2])
def test_low_conflict_upgrade_still_uses_high_within_cap(offline_consumer, limit):
    calls, ledger, results = offline_consumer({}, max_requests=limit,
        responder=lambda number, kwargs: json.dumps({'confidence': .5}))
    assert [call['reasoning']['effort'] for call in calls] == ['low', 'high'][:limit]
    assert [row['effort'] for row in ledger] == ['low', 'high'][:limit]
    assert len(results) == limit - 1
    if results:
        assert results[0]['effort'] == 'high'


def test_explicit_rejection_retains_actual_effort(offline_consumer, tmp_path):
    def fail(number, kwargs):
        error = RuntimeError('offline provider rejection')
        error.status_code = 401
        raise error
    with pytest.raises(SystemExit, match='费用待核对'):
        offline_consumer({'reasoning_effort': 'medium'}, responder=fail)
    ledger = list(runner.rows(tmp_path/'requests.jsonl'))
    results = list(runner.rows(tmp_path/'results.jsonl'))
    assert ledger[0]['effort'] == results[0]['effort'] == 'medium'
    assert ledger[0]['billing_state'] == results[0]['billing_state'] == 'UNKNOWN'


@pytest.mark.parametrize('effort', ['HIGH', 'ultra', '', None, True, [], 1])
def test_invalid_effort_fails_before_constructing_api_client(tmp_path, monkeypatch, effort):
    def forbidden_client(**kwargs):
        pytest.fail('invalid effort must never construct an API client')
    monkeypatch.setattr(openai, 'OpenAI', forbidden_client)
    args = SimpleNamespace(validate_only=False, max_requests=1, results=tmp_path/'results.jsonl')
    cat = {'stage3': {'reasoning_effort': effort}}
    with pytest.raises(ValueError, match='reasoning_effort must be low, medium, or high'):
        runner.consume(args, cat, tmp_path/'blocked.json', 'fixture', iter([{}]))
    assert not args.results.exists()
    assert not (tmp_path/'requests.jsonl').exists()

pytestmark = pytest.mark.usefixtures("offline_api_transport")
