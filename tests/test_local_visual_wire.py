import json
import sys
from contextlib import nullcontext
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace as N
import pytest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
from selection_automation.local_visual_wire import prepare,unpack,LocalVisualFormatError,ERROR_CODE


def schema():
    strings={'type':'string'}
    fields={'field':strings,'amazon':strings,'temu':strings,
            'variant_field':{'type':'string','enum':['NONE','color']},
            'kind':{'type':'string','enum':['IDENTITY','VARIANT','UNRESOLVED']}}
    item={'type':'object','properties':{'i':strings,'m':{'type':'object'},'f':{'type':'object'},
          'v':{'type':'object','properties':{'differences':{'type':'array','items':{'type':'object','properties':fields}}}},
          'c':{'type':'number','minimum':0,'maximum':1}},
          'required':['i','m','f','v','c'],'additionalProperties':False}
    return {'type':'object','properties':{'candidates':{'type':'array','items':item}},
            'required':['candidates'],'additionalProperties':False}


def test_keyed_schema_binds_all_twelve_images_and_does_not_modify_the_original():
    original=schema();before=deepcopy(original);ids=[f'C{i:02}' for i in range(12)]
    prompt,wire=prepare('KEEP CATEGORY RULES',original,ids)
    assert original==before and prompt.startswith('KEEP CATEGORY RULES')
    candidates=wire['properties']['candidates']
    assert list(candidates['properties'])==candidates['required']==ids
    assert not candidates['additionalProperties']
    branches=candidates['properties']['C00']['properties']['v']['properties']['differences']['items']['anyOf']
    assert branches[1]['properties']['kind']['enum']==['IDENTITY','UNRESOLVED']
    assert branches[1]['properties']['variant_field']['enum']==['NONE']
    assert branches[0]['properties']['kind']['enum']==['VARIANT']
    assert branches[0]['properties']['variant_field']['enum']==['NONE','color']


def test_unpack_is_lossless_and_keeps_missing_fact_fields_missing():
    first={'m':{'0':{'r':'D','a':'three keys','t':'two keys','h':'NONE'}},'c':.9}
    raw={'amazon':{'layout':{'a':'bound reference'}},'candidates':{'C01':{'c':.2},'C00':first}}
    before=deepcopy(raw);result=unpack(raw,['C00','C01'])
    assert raw==before and result['amazon']==raw['amazon']
    assert result['candidates']==[{'i':'C00',**first},{'i':'C01','c':.2}]
    assert 'f' not in result['candidates'][0]  # Existing strict decoder still rejects missing evidence.


@pytest.mark.parametrize('bad',[{'C1':{}},{'C00':{},'C01':{}},{'C00':{'i':'C01'}},[{'i':'C00'}]])
def test_misbound_or_ambiguous_ids_are_not_repaired_or_imported(bad):
    with pytest.raises(LocalVisualFormatError):unpack({'candidates':bad},['C00'])


@pytest.mark.parametrize('always_bad',[False,True])
def test_local_consumer_retries_once_as_twelve_then_saves_or_stops(tmp_path,monkeypatch,always_bad):
    import run_category_layer3 as runner
    from selection_automation import stage3_provider,stage3_transport,visual_examples,analysis_concurrency
    requests=[];active_counts=[]
    class Client:
        def __init__(self,*args):self.responses=self
        def __enter__(self):return self
        def __exit__(self,*args):pass
        def prepare(self,*args,**kwargs):return None
        def create(self,**request):
            events=list(runner.rows(tmp_path/'request_intents.jsonl'));active=set()
            for event in events:
                if event['status']=='started':active.add(event['request_id'])
                elif event['status']=='settled':active.discard(event['request_id'])
            active_counts.append(len(active))
            ids=list(request['text']['format']['schema']['properties']['candidates']['properties'])
            requests.append(ids)
            raw={'candidates':{'C1':{}}} if always_bad or len(requests)==1 else {'candidates':{i:{'fixture':True} for i in ids}}
            return N(id='fixture-'+str(len(requests)),usage=None,status='completed',output_text=json.dumps(raw))
    monkeypatch.setattr(stage3_provider,'VisualClient',Client)
    monkeypatch.setattr(stage3_transport,'create_transport',lambda *a,**k:nullcontext(object()))
    monkeypatch.setattr(analysis_concurrency,'record_sample',lambda *a,**k:None)
    monkeypatch.setattr(visual_examples,'example_content',lambda *a:[])
    monkeypatch.setattr(runner,'protocol',lambda cat,n:('KEEP',schema()))
    monkeypatch.setattr(runner,'evidence_block',lambda *a:{'type':'input_text','text':'bound product'})
    monkeypatch.setattr(runner,'cached_image_url',lambda path:'data:image/png;base64,fixture')
    monkeypatch.setattr(runner,'grid_data_url',lambda rows:('data:image/png;base64,grid',[{'id':f'C{i:02}','goods_id':r['goods_id']} for i,r in enumerate(rows)]))
    monkeypatch.setattr(runner,'decode',lambda cat,raw,ids:{i:{'overall_confidence':.9} for i in ids})
    monkeypatch.setattr(runner,'score',lambda *a:{'hard_conflicts':[]})
    provider=stage3_provider.settings({'mode':'local_model','base_url':'http://127.0.0.1:11434/v1',
                'protocol':'chat_completions','model':'fixture','workers':1,'concurrency_mode':'manual'})
    cat={'stage3':{'provider':provider,'model':'fixture','modules':{'shape':100},'batch_size':12,'max_concurrency':1}}
    rows=[{'asin':'ASIN','goods_id':str(i),'pair_revision':str(i),'vision_revision_key':str(i),'attempt':1,
           'amazon_image_path':'same-reference','temu_image_path':str(i)} for i in range(12)]
    args=N(results=tmp_path/'results.jsonl',validate_only=False,max_requests=0)
    if always_bad:
        with pytest.raises(SystemExit,match='不合格结果'):runner.consume(args,cat,tmp_path/'blocked.json','fixture',iter(rows))
        assert json.loads((tmp_path/'local_model_error.json').read_text('utf-8'))['code']==ERROR_CODE
    else:runner.consume(args,cat,tmp_path/'blocked.json','fixture',iter(rows))
    assert requests==[[f'C{i:02}' for i in range(12)]]*2
    assert active_counts==[1,1]
    results=list(runner.rows(args.results));assert len(results)==12
    assert {r['api_status'] for r in results}==({'failed'} if always_bad else {'success'})
    assert {r['billing_state'] for r in results}=={'KNOWN'} if always_bad else True
    assert len([r for r in runner.rows(tmp_path/'request_intents.jsonl') if r['status']=='settled'])==2


def test_only_current_matching_model_failure_becomes_a_specific_controller_error(tmp_path):
    from watch_stage3 import failure_state
    (tmp_path/'local_model_error.json').write_text(json.dumps({'code':ERROR_CODE,'time':20,'model':'chosen'}))
    assert failure_state(tmp_path,10,{'mode':'local_model','model':'chosen'})['error_code']==ERROR_CODE
    assert 'error_code' not in failure_state(tmp_path,30,{'mode':'local_model','model':'chosen'})
    assert 'error_code' not in failure_state(tmp_path,10,{'mode':'local_model','model':'another'})
    assert 'error_code' not in failure_state(tmp_path,10,{'mode':'multimodal_api','model':'chosen'})


@pytest.mark.parametrize('count,complete',[(6,True),(8,True),(6,False)])
def test_real_visual_decoder_keeps_all_twelve_decisions_and_inventory_evidence(count,complete):
    from test_vision_group_compact import responses
    from test_set_inventory_identity import side
    from selection_automation.vision import decode,protocol
    cat,legacy,grouped=responses(12);ids=[f'C{i:02}' for i in range(12)]
    grouped['candidates'][-1]['f']['body_type']['inventory']['temu']=side(count,complete)
    raw={**grouped,'candidates':{row['i']:{k:deepcopy(v) for k,v in row.items() if k!='i'} for row in grouped['candidates']}}
    prompt,original=protocol(cat,12);prepare(prompt,original,ids)
    assert decode(cat,unpack(raw,ids),ids)==decode(cat,grouped,ids)
    del raw['candidates']['C00']['v']['reason']
    with pytest.raises(ValueError):decode(cat,unpack(raw,ids),ids)
