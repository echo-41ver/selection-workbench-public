import sys
from pathlib import Path
import threading
from types import SimpleNamespace

import openai
import pytest

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
import run_category_layer3 as runner


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
