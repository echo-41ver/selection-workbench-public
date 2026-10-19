import json
from pathlib import Path
import sys
from types import SimpleNamespace
import pytest
import openai
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
import run_category_layer3 as runner

@pytest.fixture(autouse=True)
def offline_connectivity(offline_api_transport):
    """A fake inference response must not depend on a real connectivity GET."""

class SimulatedProcessCrash(BaseException):pass

def fixture(tmp_path,monkeypatch,crash_at):
    monkeypatch.setenv('DEEPSEEK_API_KEY','offline-fixture-key')
    monkeypatch.setattr(runner,'_data_url',lambda *a:'data:image/png;base64,fixture')
    monkeypatch.setattr(runner,'protocol',lambda *a:('fixture',{}))
    calls=[]
    class Client:
        def __init__(self,**kw):self.responses=self
        def __enter__(self):return self
        def __exit__(self,*a):pass
        def create(self,**kw):
            intents=list(runner.rows(tmp_path/'request_intents.jsonl'))
            assert intents[-1]['status']=='started'
            calls.append(kw)
            if crash_at=='before_receipt':raise SimulatedProcessCrash()
            return SimpleNamespace(id='fixture-response',status='completed',output_text='{}',usage=SimpleNamespace(model_dump=lambda:{'total_tokens':20}))
    monkeypatch.setattr(openai,'OpenAI',Client)
    def decode(*a):
        if crash_at=='after_receipt':raise SimulatedProcessCrash()
        return {'C00':{'overall_confidence':.9}}
    monkeypatch.setattr(runner,'decode',decode)
    monkeypatch.setattr(runner,'score',lambda *a:{'hard_conflicts':[]})
    args=SimpleNamespace(validate_only=False,max_requests=1,results=tmp_path/'results.jsonl')
    cat={'stage3':{'modules':{'one':100},'batch_size':1,'max_concurrency':1}}
    def pending():return iter([{'asin':'A','goods_id':'T','amazon_image_path':'a','temu_image_path':'t','vision_revision_key':'v','pair_revision':'p','attempt':1}])
    return args,cat,pending,calls

@pytest.mark.parametrize('crash_at',['before_receipt','after_receipt'])
def test_crash_never_reissues_an_uncertain_paid_request(tmp_path,monkeypatch,crash_at):
    args,cat,pending,calls=fixture(tmp_path,monkeypatch,crash_at)
    with pytest.raises(SimulatedProcessCrash):runner.consume(args,cat,tmp_path/'blocked.json','k',pending())
    journal=tmp_path/'request_intents.jsonl';before=journal.read_bytes()
    assert runner.unresolved_request_intents(journal)
    with pytest.raises(SystemExit,match='未结算视觉请求'):runner.consume(args,cat,tmp_path/'blocked.json','k',pending())
    assert len(calls)==1 and journal.read_bytes()==before
    assert b'offline-fixture-key' not in before
    if crash_at=='after_receipt':assert list(runner.rows(tmp_path/'requests.jsonl'))[0]['usage']['total_tokens']==20

def test_success_settles_intent_and_retains_legacy_ledger(tmp_path,monkeypatch):
    args,cat,pending,calls=fixture(tmp_path,monkeypatch,None)
    runner.consume(args,cat,tmp_path/'blocked.json','k',pending())
    journal=list(runner.rows(tmp_path/'request_intents.jsonl'))
    assert [r['status'] for r in journal]==['started','settled']
    assert journal[0]['request_id']==journal[1]['request_id']
    assert not runner.unresolved_request_intents(tmp_path/'request_intents.jsonl')
    assert len(list(runner.rows(tmp_path/'requests.jsonl')))==1
    assert list(runner.rows(args.results))[0]['api_status']=='success'

@pytest.mark.parametrize('tail',[b'{"request_id":',b'{"status":"started"}\n'])
def test_broken_intent_journal_stops_before_client(tmp_path,monkeypatch,tail):
    args,cat,pending,calls=fixture(tmp_path,monkeypatch,None)
    (tmp_path/'request_intents.jsonl').write_bytes(tail)
    with pytest.raises(SystemExit,match='未结算视觉请求'):runner.consume(args,cat,tmp_path/'blocked.json','k',pending())
    assert calls==[]

@pytest.mark.parametrize('sources,expected',[
    (('候选商品可见纹理','Amazon基准图可见纹理'),True),
    (('TEMU图显示按钮','Amazon商品显示按钮'),True),
    (('Amazon图显示按钮，与TEMU图相同','TEMU图显示按钮，与Amazon基准图相同'),False),
    (('左侧有装饰带','与Amazon基准图一样的边缘'),False),
])
def test_source_guard_needs_both_explicit_opposite_owners(sources,expected):
    from selection_automation.identity import explicitly_reversed_sources
    facts={'modules':{'any_category_part':{'amazon_evidence':sources[0],'temu_evidence':sources[1]}}}
    before=json.dumps(facts,ensure_ascii=False)
    assert bool(explicitly_reversed_sources(facts)) is expected
    assert json.dumps(facts,ensure_ascii=False)==before

@pytest.mark.parametrize('error',[TimeoutError,ConnectionResetError])
def test_accepted_transport_failure_blocks_second_real_cache_run(tmp_path,monkeypatch,error):
    from test_incremental_delivery_repairs import candidate_fixture
    cat,category,candidates,row=candidate_fixture(tmp_path)
    monkeypatch.setenv('DEEPSEEK_API_KEY','offline-fixture-key')
    monkeypatch.setattr(runner,'_data_url',lambda *a:'data:image/png;base64,fixture')
    monkeypatch.setattr(runner,'protocol',lambda *a:('fixture',{}))
    accepted=[]
    class Client:
        def __init__(self,**kw):self.responses=self
        def __enter__(self):return self
        def __exit__(self,*a):pass
        def create(self,**kw):
            accepted.append('provider_accepted')
            raise error('response unavailable after acceptance')
    monkeypatch.setattr(openai,'OpenAI',Client)
    args=SimpleNamespace(category=category,results=tmp_path/'results.jsonl',validate_only=False,max_pairs=0,max_requests=1)
    stops=[]
    for _ in range(2):
        try:
            with runner.frozen_candidates(candidates) as frozen:runner.run(args,frozen)
        except SystemExit as exc:stops.append(str(exc))
    assert len(accepted)==1
    assert runner.unresolved_request_intents(tmp_path/'request_intents.jsonl')
    assert stops and all('未结算' in value for value in stops)
    result=list(runner.rows(args.results));assert len(result)==1 and result[0]['billing_state']=='UNKNOWN'

@pytest.mark.parametrize('status', [401, 402, 403, 429, 500])
def test_http_rejection_and_uncertain_server_error(tmp_path, monkeypatch, status):
    args, cat, pending, calls = fixture(tmp_path, monkeypatch, None)
    class Client:
        def __init__(self, **kw): self.responses = self
        def __enter__(self): return self
        def __exit__(self, *a): pass
        def create(self, **kw):
            calls.append(kw)
            error = RuntimeError('fixture rejection')
            error.status_code = status
            raise error
    monkeypatch.setattr(openai, 'OpenAI', Client)
    if status==500:
        runner.consume(args,cat,tmp_path/'blocked.json','k',pending())
    else:
        with pytest.raises(SystemExit):runner.consume(args,cat,tmp_path/'blocked.json','k',pending())
    with pytest.raises(SystemExit):runner.consume(args,cat,tmp_path/'blocked.json','k',pending())
    assert runner.unresolved_request_intents(tmp_path/'request_intents.jsonl')
    result = list(runner.rows(args.results))[0]
    assert result['billing_state'] == 'UNKNOWN'
    if status != 500:
        assert '被拒绝' in result['failure_reason'] and '费用待核对' in result['failure_reason']
    assert len(calls) == 1


def test_local_client_failure_creates_no_sent_intent(tmp_path, monkeypatch):
    args, cat, pending, calls = fixture(tmp_path, monkeypatch, None)
    def invalid_client(**kw): raise ValueError('local fixture setup failure')
    monkeypatch.setattr(openai, 'OpenAI', invalid_client)
    runner.consume(args, cat, tmp_path/'blocked.json', 'k', pending())
    assert not (tmp_path/'request_intents.jsonl').exists()
    assert calls == []
    assert list(runner.rows(args.results))[0]['billing_state'] == 'NO_CHARGE'

@pytest.mark.parametrize('field', ['design_comparison', 'matches', 'differences'])
def test_identity_normal_comparison_references_keep_source_ownership(field):
    from selection_automation.identity import explicitly_reversed_sources
    row = {'amazon': 'Amazon商品可见圆形连接件，与TEMU图一致',
           'temu': 'TEMU商品可见圆形连接件，与Amazon基准图一致'}
    identity = {field: row if field == 'design_comparison' else [row]}
    facts = {'identity': identity}
    before = json.dumps(facts, ensure_ascii=False)
    assert not explicitly_reversed_sources(facts)
    assert json.dumps(facts, ensure_ascii=False) == before
