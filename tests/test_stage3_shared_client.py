import sys
from pathlib import Path
import threading
from types import SimpleNamespace

import openai
import pytest

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
import run_category_layer3 as runner


def test_completed_slot_refills_while_another_request_is_still_slow(tmp_path,monkeypatch):
    third=threading.Event();guard=threading.Lock();calls=[]
    class Transport:
        def __init__(self,**kw):pass
        def __enter__(self):return self
        def __exit__(self,*args):pass
    class Client:
        def __init__(self,**kw):self.responses=self
        def __enter__(self):return self
        def __exit__(self,*args):pass
        def create(self,**kw):
            with guard:calls.append(kw);number=len(calls)
            if number==3:third.set()
            if number==1:assert third.wait(3),'slow request blocked refill of the completed second slot'
            return SimpleNamespace(id='response-'+str(number),status='completed',usage=None,output_text='{}')
    monkeypatch.setenv('DEEPSEEK_API_KEY','TEST_ONLY_KEY')
    monkeypatch.setattr(openai,'DefaultHttpxClient',Transport);monkeypatch.setattr(openai,'OpenAI',Client)
    monkeypatch.setattr(runner,'_data_url',lambda path:'data:image/png;base64,fixture')
    monkeypatch.setattr(runner,'protocol',lambda cat,n:('fixture',{}))
    monkeypatch.setattr(runner,'decode',lambda c,d,ids:{i:{'overall_confidence':.9} for i in ids})
    monkeypatch.setattr(runner,'score',lambda c,f:{'hard_conflicts':[]})
    cat={'stage3':{'modules':{'one':100},'batch_size':1,'max_concurrency':2}}
    rows=[{'asin':'A','goods_id':str(i),'amazon_image_path':'a','temu_image_path':str(i),
           'pair_revision':str(i),'vision_revision_key':str(i),'attempt':1} for i in range(4)]
    args=SimpleNamespace(results=tmp_path/'results.jsonl',validate_only=False,max_requests=0)
    runner.consume(args,cat,tmp_path/'blocked.json','offline',iter(rows))
    assert len(calls)==4 and third.is_set()
    assert all(row['api_status']=='success' for row in runner.rows(args.results))


@pytest.mark.parametrize('width,count,crash',[(4,8,False),(256,4,False),(1,1,True)])
def test_one_direct_pool_is_shared_and_closed_after_workers(tmp_path,monkeypatch,width,count,crash):
    events=[];calls=[];guard=threading.Lock()
    barrier=threading.Barrier(min(width,count))
    class Crash(BaseException):pass
    class Transport:
        def __init__(self,**kw):
            assert kw['trust_env'] is False
            assert kw['timeout'].read==600 and kw['timeout'].connect==30 and kw['timeout'].pool==30
            assert kw['limits'].max_connections==width
            assert kw['limits'].max_keepalive_connections==min(width,256)
            events.append('transport-created')
        def __enter__(self):return self
        def __exit__(self,*args):events.append('transport-closed')
    class Client:
        def __init__(self,**kw):
            assert isinstance(kw['http_client'],Transport) and kw['max_retries']==0
            assert kw['timeout'].read==600
            events.append('client-created');self.responses=self
        def __enter__(self):return self
        def __exit__(self,*args):
            assert len(calls)==count
            events.append('client-closed')
        def create(self,**kw):
            with guard:
                calls.append(kw);number=len(calls)
            barrier.wait(timeout=5)
            if crash:raise Crash()
            return SimpleNamespace(id='response-'+str(number),status='completed',usage=None,output_text='{}')
    monkeypatch.setenv('DEEPSEEK_API_KEY','offline')
    monkeypatch.setattr(openai,'DefaultHttpxClient',Transport)
    monkeypatch.setattr(openai,'OpenAI',Client)
    monkeypatch.setattr(runner,'_data_url',lambda path:'data:image/png;base64,fixture')
    monkeypatch.setattr(runner,'protocol',lambda cat,n:('fixture',{}))
    monkeypatch.setattr(runner,'decode',lambda c,d,ids:{i:{'overall_confidence':.9} for i in ids})
    monkeypatch.setattr(runner,'score',lambda c,f:{'hard_conflicts':[]})
    cat={'stage3':{'modules':{'one':100},'batch_size':1,'max_concurrency':width}}
    rows=[{'asin':'A','goods_id':str(i),'amazon_image_path':'a','temu_image_path':str(i),
           'pair_revision':str(i),'vision_revision_key':str(i),'attempt':1} for i in range(count)]
    args=SimpleNamespace(results=tmp_path/'results.jsonl',validate_only=False,max_requests=0)
    if crash:
        with pytest.raises(Crash):runner.consume(args,cat,tmp_path/'blocked.json','offline',iter(rows))
    else:
        runner.consume(args,cat,tmp_path/'blocked.json','offline',iter(rows))
        results=list(runner.rows(args.results))
        assert {r['goods_id'] for r in results}=={str(i) for i in range(count)}
        assert all(r['api_status']=='success' for r in results)
        assert len({r['response_id'] for r in results})==count
    assert events==['transport-created','client-created','client-closed','transport-closed']
    starts=[r for r in runner.rows(tmp_path/'request_intents.jsonl') if r['status']=='started']
    assert len(starts)==count and len({r['request_id'] for r in starts})==count


def test_truncated_paid_response_pauses_without_sending_next_pair(tmp_path,monkeypatch):
    calls=[]
    class Transport:
        def __init__(self,**kwargs):pass
        def __enter__(self):return self
        def __exit__(self,*args):return False
    class Client:
        def __init__(self,**kwargs):self.responses=self
        def __enter__(self):return self
        def __exit__(self,*args):return False
        def create(self,**kwargs):
            calls.append(kwargs)
            return SimpleNamespace(id='truncated_fixture',status='incomplete',output_text='',
                usage=SimpleNamespace(model_dump=lambda:{'total_tokens':8}),incomplete_details={'reason':'max_output_tokens'})
    monkeypatch.setenv('DEEPSEEK_API_KEY','TEST_ONLY_KEY')
    monkeypatch.setattr(openai,'DefaultHttpxClient',Transport)
    monkeypatch.setattr(openai,'OpenAI',Client)
    monkeypatch.setattr(runner,'_data_url',lambda path:'data:image/png;base64,fixture')
    monkeypatch.setattr(runner,'protocol',lambda cat,n:('fixture',{}))
    cat={'stage3':{'modules':{'one':100},'batch_size':1,'max_concurrency':1}}
    rows=[{'asin':'A','goods_id':str(i),'amazon_image_path':'a','temu_image_path':str(i),
           'pair_revision':str(i),'vision_revision_key':str(i),'attempt':1} for i in range(2)]
    args=SimpleNamespace(results=tmp_path/'results.jsonl',validate_only=False,max_requests=0)
    with pytest.raises(SystemExit,match='不自动重复请求'):
        runner.consume(args,cat,tmp_path/'blocked.json','fixture',iter(rows))
    assert len(calls)==1
    results=list(runner.rows(args.results))
    assert len(results)==1 and results[0]['response_id']=='truncated_fixture'
    ledger=list(runner.rows(tmp_path/'requests.jsonl'))
    assert ledger[0]['usage']['total_tokens']==8


def test_local_service_failure_does_not_create_paid_billing_hold(tmp_path,monkeypatch):
    from selection_automation.stage3_provider import settings,apply
    from selection_automation.stage3_billing import billing_hold
    calls=[]
    class Transport:
        def __init__(self,**kwargs):pass
        def __enter__(self):return self
        def __exit__(self,*args):return False
        def post(self,*args,**kwargs):calls.append(True);raise OSError('fixture local service down')
    monkeypatch.setattr(openai,'DefaultHttpxClient',Transport)
    monkeypatch.setattr(runner,'_data_url',lambda path:'data:image/png;base64,fixture')
    monkeypatch.setattr(runner,'grid_data_url',lambda rows:('data:image/png;base64,fixture',[{'id':f'C{i:02}','goods_id':row['goods_id']} for i,row in enumerate(rows)]))
    monkeypatch.setattr(runner,'protocol',lambda cat,n:('fixture',{}))
    cat={'stage3':{'modules':{'one':100}}};apply(cat,settings({'mode':'local_model'}))
    rows=[{'asin':'A','goods_id':str(i),'amazon_image_path':'a','temu_image_path':str(i),
           'pair_revision':str(i),'vision_revision_key':str(i),'attempt':1} for i in range(2)]
    args=SimpleNamespace(results=tmp_path/'results.jsonl',validate_only=False,max_requests=0)
    with pytest.raises(SystemExit,match='没有远程费用'):
        runner.consume(args,cat,tmp_path/'blocked.json','fixture',iter(rows))
    assert len(calls)==1
    assert not billing_hold(tmp_path/'request_intents.jsonl',args.results)['pairs']
