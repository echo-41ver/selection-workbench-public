import json,sys
from pathlib import Path
from types import SimpleNamespace
import pytest,openai
from PIL import Image
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
import run_category_layer3 as runner

@pytest.mark.parametrize('bad_kind',['corrupt','tiff'])
def test_invalid_image_creates_visible_failure_without_provider_or_intent(tmp_path,monkeypatch,bad_kind):
    monkeypatch.setenv('DEEPSEEK_API_KEY','offline-fixture')
    bad=tmp_path/'bad.img';good=tmp_path/'good.img';Image.new('RGB',(16,16),'red').save(good,format='PNG')
    if bad_kind=='tiff':Image.new('RGB',(16,16),'red').save(bad,format='TIFF')
    else:bad.write_bytes(b'corrupt')
    def forbid(**kwargs):raise AssertionError('Invalid input reached provider construction')
    monkeypatch.setattr(openai,'OpenAI',forbid)
    args=SimpleNamespace(validate_only=False,max_requests=1,results=tmp_path/'results.jsonl')
    cat={'stage3':{'modules':{'one':100},'batch_size':1,'max_concurrency':1}}
    row={'asin':'A','goods_id':'T','amazon_image_path':str(good),'temu_image_path':str(bad),'pair_revision':'p','vision_revision_key':'v','attempt':1}
    runner.consume(args,cat,tmp_path/'blocked.json','offline',iter([row]))
    result=list(runner.rows(args.results));assert len(result)==1
    assert result[0]['api_status']=='failed' and result[0]['billing_state']=='NO_CHARGE' and result[0]['usage']=={}
    assert result[0]['failure_reason']==('IMAGE_FORMAT_UNSUPPORTED' if bad_kind=='tiff' else 'IMAGE_DECODE_FAILED')
    assert not (tmp_path/'request_intents.jsonl').exists()
    assert all(r['status']=='local_error' for r in runner.rows(tmp_path/'requests.jsonl'))

def test_one_bad_candidate_does_not_block_valid_batch_member(tmp_path,monkeypatch):
    monkeypatch.setenv('DEEPSEEK_API_KEY','offline-fixture');good=tmp_path/'good.img';Image.new('RGB',(16,16),'red').save(good,format='PNG');bad=tmp_path/'bad.img';bad.write_bytes(b'corrupt')
    monkeypatch.setattr(runner,'protocol',lambda *a:('offline',{}));calls=[]
    class Client:
        def __init__(self,**kwargs):self.responses=self
        def __enter__(self):return self
        def __exit__(self,*args):pass
        def create(self,**kwargs):
            calls.append(kwargs);return SimpleNamespace(id='offline',usage=None,status='completed',output_text='{}')
    monkeypatch.setattr(openai,'OpenAI',Client);monkeypatch.setattr(runner,'decode',lambda *a:{'C00':{'overall_confidence':.9}});monkeypatch.setattr(runner,'score',lambda *a:{'hard_conflicts':[]})
    cat={'stage3':{'modules':{'one':100},'batch_size':2,'max_concurrency':1}};args=SimpleNamespace(validate_only=False,max_requests=1,results=tmp_path/'results.jsonl')
    rows=[{'asin':'A','goods_id':str(i),'amazon_image_path':str(good),'temu_image_path':str(path),'pair_revision':str(i),'vision_revision_key':str(i),'attempt':1} for i,path in enumerate([bad,good])]
    runner.consume(args,cat,tmp_path/'blocked.json','offline',iter(rows))
    assert len(calls)==1
    results=list(runner.rows(args.results));assert len(results)==2
    assert next(r for r in results if r['goods_id']=='0')['billing_state']=='NO_CHARGE'
    assert next(r for r in results if r['goods_id']=='1')['api_status']=='success'
    intents=list(runner.rows(tmp_path/'request_intents.jsonl'));assert [r['status'] for r in intents]==['started','settled']
