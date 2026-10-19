from offline_visual_api import OfflineTransport
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import openai
import pytest

from selection_automation.stage3_billing import billing_hold
from selection_automation import stage3_progress as progress
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
import run_category_layer3 as runner


def write(path, rows):
    path.parent.mkdir(parents=True,exist_ok=True)
    path.write_text(''.join(json.dumps(r)+'\n' for r in rows),encoding='utf-8')


def held_fixture(base):
    intents=base/'request_intents.jsonl';results=base/'results.jsonl'
    write(intents,[{'request_id':'uncertain','status':'started','pairs':1,'vision_revision_keys':['old-key']}])
    write(results,[{'asin':'A','goods_id':'held','vision_revision_key':'old-key','api_status':'failed','billing_state':'UNKNOWN','attempt':1}])
    return intents,results


def test_uncertain_pairs_bound_read_only_and_held_across_revision(tmp_path):
    intents,results=held_fixture(tmp_path);before=intents.read_bytes()
    hold=billing_hold(intents,results)
    assert hold['pairs']=={('A','held')} and not hold['fail_closed']
    assert hold['keys']=={'old-key'} and hold['request_ids']=={'uncertain'}
    assert intents.read_bytes()==before


@pytest.mark.parametrize('group,pairs',[(None,1),([],1),(['old-key'],2),(['old-key','old-key'],2)])
def test_missing_or_invalid_key_mapping_fails_closed(tmp_path,group,pairs):
    intents,results=held_fixture(tmp_path)
    write(intents,[{'request_id':'uncertain','status':'started','pairs':pairs,'vision_revision_keys':group}])
    assert billing_hold(intents,results)['fail_closed']


def test_valid_keys_without_pair_result_binding_fail_closed(tmp_path):
    intents,results=held_fixture(tmp_path);results.unlink()
    assert billing_hold(intents,results)['fail_closed']


def test_pending_remains_total_but_eta_and_request_groups_exclude_unknown(tmp_path,monkeypatch):
    base=tmp_path/'task';base.mkdir()
    held_fixture(base/'stage123')
    write(progress.candidate_path(base),[{'asin':'A','goods_id':g,'pair_revision':g} for g in ['held','new','known-truncated']])
    monkeypatch.setattr(progress,'compatible_vision_keys',lambda c,r:[r['pair_revision']])
    s=progress.snapshot(base,{},history=False)
    assert (s['total'],s['pending'],s['billing_unknown_pairs'],s['runnable_pending'])==(3,3,1,2)
    assert s['pending_requests']==2 and not s['billing_hold_fail_closed']


@pytest.mark.parametrize('fail',[False,True])
def test_direct_transport_closed_and_only_unrequested_pairs_sent(tmp_path,monkeypatch,fail):
    intents,results=held_fixture(tmp_path);before=intents.read_bytes()
    events=[];requests=[]
    class Transport(OfflineTransport):
        def __init__(self,**kw):
            assert kw['trust_env'] is False
            assert kw['timeout'].read==600 and kw['timeout'].connect==30
            events.append('transport_created')
        def __enter__(self):return self
        def __exit__(self,*a):events.append('transport_closed')
    class Client:
        def __init__(self,**kw):
            assert isinstance(kw['http_client'],Transport) and kw['max_retries']==0
            self.responses=self
        def __enter__(self):return self
        def __exit__(self,*a):events.append('client_closed')
        def create(self,**kw):
            requests.append(kw)
            if fail:raise ConnectionResetError('offline')
            return SimpleNamespace(id='new-response',status='completed',usage=None,output_text='{}')
    monkeypatch.setenv('DEEPSEEK_API_KEY','offline')
    monkeypatch.setattr(openai,'DefaultHttpxClient',Transport)
    monkeypatch.setattr(openai,'OpenAI',Client)
    monkeypatch.setattr(runner,'_data_url',lambda p:'data:image/png;base64,fixture')
    monkeypatch.setattr(runner,'grid_data_url',lambda batch:('data:image/jpeg;base64,fixture',[{'id':f'C{i:02}','goods_id':r['goods_id']} for i,r in enumerate(batch)]))
    monkeypatch.setattr(runner,'protocol',lambda c,n:('fixture',{}))
    monkeypatch.setattr(runner,'decode',lambda c,d,ids:{i:{'overall_confidence':.9} for i in ids})
    monkeypatch.setattr(runner,'score',lambda c,f:{'hard_conflicts':[]})
    cat={'stage3':{'modules':{'one':100},'batch_size':12,'max_concurrency':1}}
    pending=iter([{'asin':'A','goods_id':g,'amazon_image_path':'a','temu_image_path':g,'vision_revision_key':g,'pair_revision':g,'attempt':1}
                  for g in ['held','new','known-truncated']])
    args=SimpleNamespace(results=results,validate_only=False,max_requests=1)
    runner.consume(args,cat,tmp_path/'blocked.json','offline',pending)
    assert len(requests)==1
    assert [r['goods_id'] for r in runner.rows(results)]==['held','new','known-truncated']
    assert events==['transport_created','client_closed','transport_closed']
    assert intents.read_bytes().startswith(before)
    assert runner.unresolved_request_intents(intents)>={'uncertain'}

pytestmark = pytest.mark.usefixtures("offline_api_transport")
