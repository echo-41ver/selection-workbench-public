import sys
from pathlib import Path
from types import SimpleNamespace
import json
from contextlib import nullcontext
import pytest
import openai

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
import run_category_layer3 as runner
from selection_automation.stage3_provider import VisualClient


@pytest.mark.parametrize('host,expected',[('https://api.deepseek.com','json_object'),('http://localhost:11434/v1','json_schema')])
def test_chat_schema_compatibility_is_specific_to_deepseek(host,expected):
    calls=[]
    client=VisualClient.__new__(VisualClient)
    client.provider={'protocol':'chat_completions','base_url':host}
    def create(**kw):
        calls.append(kw)
        return SimpleNamespace(id='test',usage=None,choices=[SimpleNamespace(message=SimpleNamespace(content='{}'),finish_reason='stop')])
    client.openai=SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    schema={'type':'object','required':['facts']}
    client.create(input=[{'content':[{'type':'input_text','text':'JSON facts'}]}],instructions='Compare',model='test',max_output_tokens=500,text={'format':{'type':'json_schema','name':'facts','schema':schema}})
    assert calls[0]['response_format']['type']==expected
    if expected=='json_object':assert json.dumps(schema,ensure_ascii=False,separators=(',',':')) in calls[0]['messages'][0]['content']


def test_provider_error_does_not_include_credentials_or_image_payload():
    error=SimpleNamespace(body={'error':{'message':'bad TEST_SECRET Bearer auth_value data:image/png;base64,abc','code':'bad_format'},'headers':{'Authorization':'secret'}})
    safe=runner.safe_provider_error(error,'TEST_SECRET')
    assert safe['code']=='bad_format'
    assert all(s not in str(safe) for s in ('TEST_SECRET','auth_value','base64,abc','Authorization'))


def test_invalid_request_stops_before_consuming_next_candidate(tmp_path,monkeypatch):
    monkeypatch.setenv('DEEPSEEK_API_KEY','TEST_ONLY_OFFLINE_KEY')
    calls=[]
    class Transport:
        def __init__(self,**kw):pass
        def __enter__(self):return self
        def __exit__(self,*args):pass
        def stream(self,*args,**kwargs):return nullcontext()
        def close(self):pass
    class Client:
        def __init__(self,**kw):self.responses=self
        def __enter__(self):return self
        def __exit__(self,*args):pass
        def create(self,**kw):
            calls.append(kw)
            error=RuntimeError('rejected');error.status_code=400;error.body={'error':{'message':'unsupported response_format'}}
            raise error
    monkeypatch.setattr(openai,'DefaultHttpxClient',Transport)
    monkeypatch.setattr(openai,'OpenAI',Client)
    monkeypatch.setattr(runner,'_data_url',lambda path:'data:image/png;base64,fixture')
    monkeypatch.setattr(runner,'protocol',lambda cat,n:('fixture',{}))
    cat={'stage3':{'modules':{'one':100},'batch_size':1,'max_concurrency':1}}
    rows=[{'asin':'A','goods_id':str(i),'amazon_image_path':'a','temu_image_path':'t','pair_revision':str(i),'vision_revision_key':str(i),'attempt':1} for i in range(3)]
    args=SimpleNamespace(results=tmp_path/'results.jsonl',validate_only=False,max_requests=0)
    blocked=tmp_path/'blocked.json'
    with pytest.raises(SystemExit):runner.consume(args,cat,blocked,'fixture',iter(rows))
    assert len(calls)==1
    assert json.loads(blocked.read_text())['status']==400
    assert 'unsupported response_format' in next(runner.rows(args.results))['failure_reason']
