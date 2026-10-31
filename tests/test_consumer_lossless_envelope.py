import json,sys
from pathlib import Path
from types import SimpleNamespace
import openai
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
import run_category_layer3 as runner
def test_complete_extra_brace_scored_once_without_paid_tail_retry(tmp_path,monkeypatch,offline_api_transport):
    monkeypatch.setenv('DEEPSEEK_API_KEY','TEST_ONLY_KEY')
    monkeypatch.setattr(runner,'_data_url',lambda *a:'data:image/png;base64,fixture')
    monkeypatch.setattr(runner,'protocol',lambda *a:('unchanged-protocol',{}));calls=[]
    candidate={'i':'C00','m':{'0':{'r':'D','a':'left','t':'right','h':'NONE'}},'v':{},'c':.9}
    text=json.dumps({'candidates':[candidate]});position=text.rfind(']');bad=text[:position]+'}'+text[position:]
    class Client:
        def __init__(self,**kw):self.responses=self
        def __enter__(self):return self
        def __exit__(self,*a):pass
        def create(self,**kw):
            calls.append(kw);return SimpleNamespace(id='TEST_ONLY_RESPONSE',status='completed',output_text=bad,usage=SimpleNamespace(model_dump=lambda:{'input_tokens':100,'output_tokens':100}))
    monkeypatch.setattr(openai,'OpenAI',Client)
    def checked_decode(cat,raw,ids):
        assert raw=={'candidates':[candidate]} and ids==['C00']
        return {'C00':{'overall_confidence':.9,'unchanged_observations':candidate}}
    monkeypatch.setattr(runner,'decode',checked_decode);monkeypatch.setattr(runner,'score',lambda *a:{'hard_conflicts':[]})
    args=SimpleNamespace(validate_only=False,max_requests=2,results=tmp_path/'results.jsonl');cat={'stage3':{'modules':{'body':100},'batch_size':12,'max_concurrency':4}}
    row={'asin':'A','goods_id':'T','amazon_image_path':'a','temu_image_path':'t','vision_revision_key':'v','pair_revision':'p','attempt':1}
    runner.consume(args,cat,tmp_path/'blocked.json','fixture',iter([row]));result=list(runner.rows(args.results))
    assert len(calls)==1 and len(result)==1 and result[0]['api_status']=='success'
    assert result[0]['response_format_compatibility']['version']=='lossless-envelope-v2-syntax-only'
    assert result[0]['visual_facts']['unchanged_observations']==candidate
    assert not runner.unresolved_request_intents(tmp_path/'request_intents.jsonl')
