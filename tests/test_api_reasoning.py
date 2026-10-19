import json
from types import SimpleNamespace
from urllib.error import HTTPError

import pytest

from selection_automation.api_reasoning import capabilities, discover, list_models, validate
from selection_automation.stage3_provider import VisualClient, settings


def provider(**values):
    return settings({'mode':'multimodal_api','base_url':'https://models.example/v1','model':'vision-test',**values})


@pytest.mark.parametrize('metadata',[
    {'supported_reasoning_efforts':['low','high'],'default_reasoning_effort':'low'},
    {'reasoning_efforts':['low','high'],'default_reasoning_effort':'low'},
    {'effort':{'supported_levels':['low','high'],'default_level':'low'}},
    {'capabilities':{'reasoning':{'efforts':['low','high'],'default_effort':'low'}}},
    {'parameters':{'reasoning_effort':{'enum':['low','high'],'default':'low'}}},
    {'parameters':{'reasoning':{'properties':{'effort':{'enum':['low','high'],'default':'low'}}}}},
])
def test_only_explicit_model_efforts_are_selectable(metadata):
    result=capabilities({'id':'vision-test',**metadata},provider())
    assert result['efforts']==['low','high'] and result['default_effort']=='low'
    assert result['status']=='ready'


def test_model_and_protocol_capabilities_do_not_leak_to_other_choices():
    record={'id':'other-model','supported_reasoning_efforts':['high']}
    assert not capabilities(record,provider())['efforts']
    record={'id':'vision-test','capabilities':{'responses':{'reasoning_efforts':['high']},
                                            'chat_completions':{'reasoning_efforts':['low']}}}
    assert capabilities(record,provider())['efforts']==['high']
    assert capabilities(record,provider(protocol='chat_completions'))['efforts']==['low']


def test_standard_model_metadata_does_not_prove_reasoning_support():
    result=capabilities({'id':'vision-test','owned_by':'openai','supported_parameters':['reasoning']},provider())
    assert result['status']=='unreported' and result['efforts']==[] and result['default_effort'] is None
    assert capabilities({'id':'vision-test','reasoning_efforts':[]},provider())['status']=='unsupported'


def test_metadata_redirect_cannot_forward_authorization_to_another_destination():
    from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
    import threading
    from selection_automation.api_reasoning import _read
    requests=[]
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            requests.append(self.path)
            self.send_response(302);self.send_header('Location','http://127.0.0.1:'+str(self.server.server_port)+'/other-origin');self.end_headers()
        def log_message(self,*args):pass
    server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    try:
        with pytest.raises(HTTPError):_read('http://127.0.0.1:'+str(server.server_port)+'/models','TEST_ONLY_SECRET')
        assert requests==['/models']
    finally:server.shutdown();server.server_close();thread.join()


def test_discovery_reads_model_then_exact_list_record_without_inference():
    urls=[]
    def fetch(url):
        urls.append(url)
        if url.endswith('/vision-test'):return {'id':'vision-test'}
        return {'data':[{'id':'other','reasoning_efforts':['max']},
                        {'id':'vision-test','reasoning_efforts':['medium','high']}]}
    result=discover(provider(),fetch=fetch)
    assert result['efforts']==['medium','high']
    assert urls==['https://models.example/v1/models/vision-test','https://models.example/v1/models']


def test_live_deepseek_model_schema_supplies_per_model_efforts_and_image_support():
    payload={'data':[
        {'id':'deepseek-flash','name':'DeepSeek Flash','input_modalities':['text','image'],
         'effort':{'supported_levels':['low','high','max'],'default_level':'high'}},
        {'id':'deepseek-v4-pro','input_modalities':['text'],
         'effort':{'supported_levels':['low','high','max'],'default_level':'high'}}]}
    inventory=list_models(provider(),fetch=lambda url:payload)
    assert inventory['status']=='ready' and len(inventory['models'])==2
    first,second=inventory['models']
    assert first['vision'] is True and second['vision'] is False
    assert first['reasoning']['efforts']==['low','high','max']
    assert first['reasoning']['default_effort']=='high'


def test_model_list_does_not_return_raw_vendor_metadata_or_invent_missing_vision():
    inventory=list_models(provider(),fetch=lambda url:{'data':[{'id':'vision-test','vendor_secret':'PRIVATE'}, {'id':'bad id'}]})
    assert len(inventory['models'])==1 and inventory['models'][0]['vision'] is None
    assert 'PRIVATE' not in json.dumps(inventory)


def test_failed_metadata_read_never_returns_key_or_guessed_levels():
    urls=[]
    def fetch(url):
        urls.append(url);raise HTTPError(url,403,'private response',{},None)
    result=discover(provider(),key='TEST_ONLY_SECRET',fetch=fetch)
    assert len(urls)==1 and result['status']=='unavailable' and result['efforts']==[]
    assert 'TEST_ONLY_SECRET' not in json.dumps(result)


def test_default_needs_no_lookup_but_unconfirmed_override_is_rejected():
    validate(provider(reasoning_effort='default'),catalog={})
    with pytest.raises(ValueError):validate(provider(reasoning_effort='high'),catalog={'status':'unreported','efforts':[]})
    with pytest.raises(ValueError):validate(provider(reasoning_effort='high'),catalog={'status':'ready','efforts':['low']})
    validate(provider(reasoning_effort='high'),catalog={'status':'ready','efforts':['high']})


@pytest.mark.parametrize('protocol',['responses','chat_completions'])
@pytest.mark.parametrize('effort',['default','none','high','xhigh'])
def test_actual_request_uses_selected_effort_or_omits_override(protocol,effort):
    captured=[]
    def send(**kwargs):
        captured.append(kwargs)
        return SimpleNamespace(id='test',usage=None,choices=[SimpleNamespace(
            message=SimpleNamespace(content='{}'),finish_reason='stop')])
    client=VisualClient.__new__(VisualClient)
    client.provider=provider(protocol=protocol,reasoning_effort=effort)
    client.openai=SimpleNamespace(responses=SimpleNamespace(create=send),
        chat=SimpleNamespace(completions=SimpleNamespace(create=send)))
    client.create(model='vision-test',instructions='test',input=[{'content':[{'type':'input_text','text':'test'}]}],
        reasoning={'effort':'low'},text={'format':{'type':'json_schema','name':'test','schema':{}}},max_output_tokens=10)
    if protocol=='responses':
        assert captured[0].get('reasoning')==(None if effort=='default' else {'effort':effort})
    else:
        assert captured[0].get('reasoning_effort')==(None if effort=='default' else effort)
    assert 'temperature' not in captured[0]


def test_native_default_and_explicit_strengths_do_not_reuse_legacy_fixed_effort_profiles():
    from selection_automation.analysis_concurrency import profile
    assert profile(provider())!=profile(provider(reasoning_effort='default'))
    assert profile(provider(reasoning_effort='low'))!=profile(provider(reasoning_effort='high'))


def test_capability_endpoint_reads_without_saving_key_or_starting_analysis(tmp_path,monkeypatch):
    import sys
    from pathlib import Path
    sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
    import web_console
    from selection_automation import api_reasoning
    console=web_console.Console(tmp_path)
    seen=[]
    def read(p,key,**kwargs):
        seen.append((p['base_url'],key));return {'status':'ready','efforts':['high'],'default_effort':'high'}
    monkeypatch.setattr(api_reasoning,'discover',read)
    monkeypatch.setattr(api_reasoning,'list_models',lambda *a,**kw:{'status':'unavailable','models':[]})
    monkeypatch.setattr(console,'invoke',lambda *a,**kw:pytest.fail('metadata read must not start an analysis'))
    result=console.api_reasoning_catalog({'provider':provider(),'api_key':'TEST_ONLY_SECRET'})
    assert result['catalog']['efforts']==['high'] and seen==[('https://models.example/v1','TEST_ONLY_SECRET')]
    assert 'TEST_ONLY_SECRET' not in json.dumps(result)
    assert not (tmp_path/'runtime/secrets/stage3-api.dpapi').exists()
