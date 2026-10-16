import json
import threading
from pathlib import Path
from types import SimpleNamespace

from PIL import Image
import pytest

from selection_automation import visual_batch as batch
from selection_automation.stage3_provider import VisualClient,settings


def sample_rows(tmp_path,count=24):
    amazon=tmp_path/'amazon.png';Image.new('RGB',(600,400),'red').save(amazon)
    rows=[]
    for index in range(count):
        image=tmp_path/f'temu-{index}.png';Image.new('RGB',(600,400),(index*7%255,80,160)).save(image)
        rows.append({'asin':'AMAZON','goods_id':f'T{index}','amazon_image_path':str(amazon),'temu_image_path':str(image),
                     'pair_revision':str(index),'vision_revision_key':str(index),'attempt':1,'vector_score':90})
    return rows


def test_groups_and_numbered_grid_preserve_all_candidate_bindings(tmp_path):
    rows=sample_rows(tmp_path,25);groups=batch.groups(rows)
    assert [len(group) for group in groups]==[12,12,1]
    receipt=batch.save_assets(tmp_path,groups[0])
    assert len(receipt['attachments'])==2
    with Image.open(receipt['attachments'][1]['path']) as grid:assert grid.size==(2400,3344)
    assert [tile['goods_id'] for tile in receipt['grid_candidates']]==[f'T{i}' for i in range(12)]
    assert len({tuple(tile['box']) for tile in receipt['grid_candidates']})==12
    batch.check_assets(receipt,rows[11])
    Path(receipt['attachments'][1]['path']).write_bytes(b'changed')
    with pytest.raises(ValueError,match='发生变化'):batch.check_assets(receipt,rows[11])


def test_image_cache_invalidates_when_source_changes(tmp_path):
    image=tmp_path/'image.png';image.write_bytes(b'first');calls=[]
    def encode(path):calls.append(path);return Path(path).read_bytes().hex()
    assert batch.data_url(image,encode)==batch.data_url(image,encode)
    assert len(calls)==1
    image.write_bytes(b'second-longer')
    assert batch.data_url(image,encode)!=b'first'.hex() and len(calls)==2


@pytest.mark.parametrize('workers',[2,80,400])
def test_two_real_twelve_candidate_payloads_run_concurrently(tmp_path,monkeypatch,workers):
    import openai
    import run_category_layer3 as runner
    from selection_automation import stage3_provider
    rows=sample_rows(tmp_path);barrier=threading.Barrier(2);requests=[];connections=[];executors=[]
    executor=runner.ThreadPoolExecutor
    def create_executor(**kwargs):executors.append(kwargs['max_workers']);return executor(**kwargs)
    monkeypatch.setattr(runner,'ThreadPoolExecutor',create_executor)
    class Transport:
        def __init__(self,**kwargs):connections.append(kwargs['limits'].max_connections)
        def __enter__(self):return self
        def __exit__(self,*args):pass
    class Client:
        def __init__(self,**kwargs):self.responses=self
        def __enter__(self):return self
        def __exit__(self,*args):pass
        def create(self,**request):
            requests.append(request);barrier.wait(timeout=5)
            return SimpleNamespace(id=str(len(requests)),usage=None,status='completed',output_text='{}')
    monkeypatch.setattr(openai,'DefaultHttpxClient',Transport);monkeypatch.setattr(openai,'OpenAI',Client)
    monkeypatch.setattr(stage3_provider,'request_key',lambda provider:'TEST_ONLY_KEY')
    monkeypatch.setattr(runner,'protocol',lambda category,count:('compare',{}))
    monkeypatch.setattr(runner,'decode',lambda category,response,ids:{identity:{'overall_confidence':.9} for identity in ids})
    monkeypatch.setattr(runner,'score',lambda category,facts:{'hard_conflicts':[]})
    category={'stage3':{'modules':{'shape':100},'batch_size':12,'max_concurrency':workers}}
    args=SimpleNamespace(results=tmp_path/'results.jsonl',validate_only=False,max_requests=0)
    runner.consume(args,category,tmp_path/'blocked.json','fixture',iter(rows))
    assert len(requests)==2
    assert connections==executors==[workers]
    assert all(len([block for block in request['input'][0]['content'] if block['type']=='input_image'])==2 for request in requests)
    results=list(runner.rows(args.results))
    assert len(results)==24 and {row['goods_id'] for row in results}=={row['goods_id'] for row in rows}
    assert all(row['api_status']=='success' for row in results)


def test_local_decision_batch_maps_twelve_answers_and_d1_rejects_expensive_multitask():
    provider=settings({'mode':'local_model','concurrency_mode':'manual','workers':2})
    class Transport:
        def post(self,url,**request):
            payload=json.loads(request['content']);assert len(payload['images'])==2
            answers={identity:{'choice':'different','probabilities':{'same':.01,'similar':.01,'different':.98}} for identity in payload['questions']}
            return SimpleNamespace(raise_for_status=lambda:None,json=lambda:{'answers':answers})
    content=[{'type':'input_image','image_url':'data:image/png;base64,fixture'}]*2
    ids=[f'C{i:02}' for i in range(12)]
    client=VisualClient(provider,Transport(),'fixture',10)
    prepared=client.prepare(content,'rules',candidate_ids=ids)
    result=client.create(_prepared_data=prepared)
    assert set(json.loads(result.output_text)['decision_facts_by_id'])==set(ids)
    d1=VisualClient(settings({'mode':'liquid_d1'}),Transport(),'fixture',10)
    with pytest.raises(ValueError,match='省费用'):d1.prepare(content,'rules',candidate_ids=ids)
