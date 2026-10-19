import json
from types import SimpleNamespace as N
import pytest
import openai
from offline_visual_api import OfflineTransport,grid
from test_compact_rules import candidate
from test_match_criteria import configured,products
import run_category_layer3 as runner


@pytest.mark.parametrize('second_relation,final_state',[('S','确认同款'),('U','待补充证据')])
def test_only_unclear_tile_gets_one_automatic_original_recheck(tmp_path,monkeypatch,products,second_relation,final_state):
    calls=[]
    class Transport(OfflineTransport):
        def __init__(self,**kw):pass
        def __enter__(self):return self
        def __exit__(self,*args):pass
    class Client:
        def __init__(self,**kw):self.responses=self
        def __enter__(self):return self
        def __exit__(self,*args):pass
        def create(self,**kw):
            calls.append(kw)
            values=([candidate(),candidate('C01','U','U')] if len(calls)==1 else
                    [candidate(relation=second_relation,condition=second_relation)])
            return N(id='response-'+str(len(calls)),status='completed',usage=None,
                     output_text=json.dumps({'candidates':values}))
    monkeypatch.setenv('DEEPSEEK_API_KEY','TEST_ONLY_KEY')
    monkeypatch.setattr('selection_automation.stage3_provider.request_key',lambda _:'TEST_ONLY_KEY')
    monkeypatch.setattr(openai,'DefaultHttpxClient',Transport);monkeypatch.setattr(openai,'OpenAI',Client)
    monkeypatch.setattr(runner,'_data_url',lambda path:'data:image/png;base64,fixture')
    monkeypatch.setattr(runner,'grid_data_url',grid)
    category=configured(products);category['stage3'].update(batch_size=12,max_concurrency=1)
    category['stage3']['provider']={'mode':'multimodal_api','protocol':'responses','base_url':'https://fixture.example/v1','model':'fixture','workers':1}
    rows=[{'asin':'A1','goods_id':str(i),'amazon_image_path':'a','temu_image_path':str(i),
           'pair_revision':str(i),'vision_revision_key':str(i),'attempt':1} for i in range(2)]
    args=N(results=tmp_path/'results.jsonl',validate_only=False,max_requests=10)
    runner.consume(args,category,tmp_path/'blocked.json','offline',iter(rows))
    result={row['goods_id']:row for row in runner.rows(args.results) if row['api_status']=='success'}
    assert len(calls)==2 and len(result)==2
    assert result['0']['module_result']['state']=='确认同款'
    assert result['1']['module_result']['state']==final_state
    assert result['1']['automatic_original_recheck']['attempt']==1
    assert len(list(runner.rows(tmp_path/'evidence_rechecks.jsonl')))==1
    assert not runner.unresolved_request_intents(tmp_path/'request_intents.jsonl')
