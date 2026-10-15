import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from selection_automation import desktop_agents as desktop
from selection_automation import local_agents
from selection_automation import native_agent_transport as native
from selection_automation.stage3_provider import settings


def test_three_desktop_agents_are_selectable_and_have_independent_limits():
    assert {'workbuddy','acciowork','doubao'}<=set(local_agents.IDS)
    assert settings({'mode':'local_agent','agent_ids':['workbuddy','acciowork','doubao']})['agent_ids']==['workbuddy','acciowork','doubao']


def test_recovered_response_settles_original_receipt_without_claiming_valid_classification(tmp_path):
    original=tmp_path/'original';recovery=tmp_path/'recovery';original.mkdir();recovery.mkdir()
    native.save(original/'native-session.json',{'agent':'doubao','state':'send_intended','model':'TEST_MODEL'})
    state={'agent':'doubao','state':'send_intended','model':'TEST_MODEL','result_job':str(original)}
    # A complete model response may still fail the independent fact validator.
    (recovery/'reviews.jsonl').write_text('{"TEST_ONLY":"invalid facts are retained for correction"}\n','utf-8')
    native.finish_native_response(recovery,state)
    assert native.read(original/'native-session.json')['state']=='complete'
    assert native.read(original/'native-session.json')['model']=='TEST_MODEL'
    assert (original/'reviews.jsonl').read_bytes()==(recovery/'reviews.jsonl').read_bytes()
    assert not native.result_exists(original)


def test_compact_desktop_response_expands_twelve_bound_candidates_without_losing_facts(tmp_path,monkeypatch):
    from test_vision_group_compact import responses
    from selection_automation.vision import decode
    cat,_,raw=responses(12)
    rows=[{'asin':'TEST_A','goods_id':f'TEST_T{i}','vision_revision_key':f'TEST_HASH_{i}'} for i in range(12)]
    (tmp_path/'manifest.jsonl').write_text(''.join(json.dumps(row)+'\n' for row in rows),'utf-8')
    native.save(tmp_path/'analysis-contract.json',{'category':'TEST_ONLY_CATEGORY.json'})
    native.save(tmp_path/'job.json',{'model':'TEST_ONLY_MODEL'})
    native.save(tmp_path/'comparison-input.json',{'amazon':{},'candidates':[{'id':f'C{i:02}','record':{}} for i in range(12)]})
    monkeypatch.setattr('selection_automation.category_config.load_category',lambda path:cat)
    request=native.doubao_request(tmp_path,'unused')
    assert 'TEST_HASH_' not in request and 'asin' not in request
    assert (tmp_path/'compact-output.json').is_file()
    assert native.write_response(tmp_path,json.dumps(raw,ensure_ascii=False))
    actual=[json.loads(line) for line in (tmp_path/'reviews.jsonl').read_text('utf-8').splitlines()]
    expected=decode(cat,raw,[f'C{i:02}' for i in range(12)])
    assert len(actual)==12
    assert [row['goods_id'] for row in actual]==[row['goods_id'] for row in rows]
    assert all(row['visual_facts']==expected[f'C{i:02}'] for i,row in enumerate(actual))
    before=(tmp_path/'reviews.jsonl').read_bytes()
    raw['candidates'][11]['i']='C00'
    assert not native.write_response(tmp_path,json.dumps(raw))
    assert (tmp_path/'reviews.jsonl').read_bytes()==before


def test_finished_invalid_reply_settles_original_instead_of_observing_forever(tmp_path):
    original=tmp_path/'original';job=tmp_path/'recovery';original.mkdir();job.mkdir()
    native.save(original/'native-session.json',{'state':'send_intended','model':'TEST_MODEL'})
    state={'state':'send_intended','result_job':str(original),'model':'TEST_MODEL'}
    with pytest.raises(RuntimeError,match='有限补跑'):
        native.receive_finished_response(job,state,'This finished reply contains no candidate output.')
    assert native.read(job/'native-session.json')['response_status']=='invalid'
    assert native.read(original/'native-session.json')['state']=='complete'
    assert not native.result_exists(original)


def test_failed_owned_observer_is_closed_without_closing_desktop_composer(monkeypatch):
    closed=[];monkeypatch.setattr(native,'close_doubao_target',lambda root,identifier:closed.append(identifier))
    native.release_doubao_observer('root',{'id':'OWNED'},{'state':'send_intended','target_id':'OWNED','owned_target':True})
    native.release_doubao_observer('root',{'id':'MAIN'},{'state':'send_intended','target_id':'MAIN','owned_target':False})
    native.release_doubao_observer('root',{'id':'OTHER'},{'target_id':'OWNED','owned_target':True})
    assert closed==['OWNED']


def test_observer_setup_timeout_cleans_created_view_before_worker_receives_it(tmp_path,monkeypatch):
    import io
    monkeypatch.setattr(native,'doubao_endpoint',lambda root:'http://127.0.0.1:9225')
    class Opener:
        def open(self,*args,**kwargs):return io.BytesIO(b'{"webSocketDebuggerUrl":"ws://127.0.0.1/browser"}')
    class Browser:
        def __init__(self,url):pass
        def call(self,*args):return {'targetId':'OWNED_CREATED_VIEW'}
        def close(self):pass
    monkeypatch.setattr(native,'build_opener',lambda *args:Opener())
    monkeypatch.setattr(native,'CDP',Browser)
    monkeypatch.setattr(native,'wait_doubao_target',lambda *args:(_ for _ in ()).throw(TimeoutError('TEST_ONLY_SOCKET_TIMEOUT')))
    closed=[];monkeypatch.setattr(native,'close_doubao_target',lambda root,identifier:closed.append(identifier))
    with pytest.raises(TimeoutError):native.new_doubao_target(tmp_path,tmp_path/'job','chrome://doubao-chat/chat/12345')
    assert closed==['OWNED_CREATED_VIEW']


def test_compact_recovery_uses_original_codec_and_accepts_numeric_text_without_changing_facts(tmp_path,monkeypatch):
    from test_vision_group_compact import responses
    cat,_,raw=responses(2)
    original=tmp_path/'original';job=tmp_path/'recovery';original.mkdir();job.mkdir()
    native.save(original/'compact-output.json',{'version':1})
    native.save(job/'native-session.json',{'result_job':str(original)})
    native.save(job/'analysis-contract.json',{'category':'TEST_ONLY_CATEGORY.json'})
    rows=[{'asin':'TEST_A','goods_id':f'TEST_T{i}','vision_revision_key':f'TEST_HASH_{i}'} for i in range(2)]
    (job/'manifest.jsonl').write_text(''.join(json.dumps(row)+'\n' for row in rows),'utf-8')
    monkeypatch.setattr('selection_automation.category_config.load_category',lambda path:cat)
    raw['candidates'][1]['c']='0.95'
    # Missing final candidate brace and an extra list terminator are punctuation
    # failures seen in finished desktop replies. All facts stay model-authored.
    text=json.dumps(raw,ensure_ascii=False)
    malformed=text[:-3]+']}'
    assert native.write_response(job,malformed)
    actual=[json.loads(line) for line in (job/'reviews.jsonl').read_text('utf-8').splitlines()]
    assert len(actual)==2 and actual[1]['visual_facts']['overall_confidence']==.95
    assert actual[1]['visual_facts']['identity']==raw['candidates'][1]['v']
    assert (job/'desktop-response.txt').read_text('utf-8')==malformed
    raw['candidates'][1]['c']='probably 0.95'
    assert not native.write_response(job,json.dumps(raw))


def test_legacy_response_rebinds_only_revision_metadata_for_unique_exact_product_ids(tmp_path):
    row={'asin':'TEST_A','goods_id':'TEST_T','vision_revision_key':'MODEL_TYPO','visual_facts':{'TEST_ONLY':True}}
    bound={**row,'vision_revision_key':'LOCAL_IMAGE_HASH'}
    (tmp_path/'manifest.jsonl').write_text(json.dumps(bound)+'\n','utf-8')
    assert native.write_response(tmp_path,json.dumps(row))
    assert native.read(tmp_path/'reviews.jsonl')['vision_revision_key']=='LOCAL_IMAGE_HASH'
    assert not native.write_response(tmp_path,json.dumps([{**row,'goods_id':'OTHER_PRODUCT'}]))


@pytest.mark.parametrize('text',['{"a": [1,}, "b":2}','{"a":"unfinished','{"a": [1, 2,]}'])
def test_desktop_punctuation_recovery_does_not_rewrite_invalid_internal_content(text):
    with pytest.raises(ValueError):json.loads(native.repair_terminal_delimiters(text))


def test_installed_desktop_executable_is_never_invoked_as_a_generic_cli(tmp_path,monkeypatch):
    app=tmp_path/'Doubao.exe';app.touch()
    monkeypatch.setattr(desktop,'installations',lambda agent:app)
    monkeypatch.setattr(desktop,'doubao_endpoint',lambda root:(_ for _ in ()).throw(RuntimeError('not enabled')))
    result=desktop.probe(tmp_path,'doubao')
    assert result['installed'] and result['requires_enable'] and not result['ready']
    assert result['max_parallel']==6 and result['max_group_size']==12
    command=desktop.command(tmp_path,result,tmp_path/'job with spaces')
    assert str(app) not in command and 'run_desktop_agent.py' in command[1]


@pytest.mark.parametrize('value',['http://example.com:4097','http://localhost.evil:4097','http://'+'user:secret'+'@127.0.0.1:4097','http://127.0.0.1:4097/?token=value'])
def test_local_pairing_password_never_goes_to_a_remote_or_credential_url(value):
    with pytest.raises(ValueError):desktop.local_url(value)


def test_accio_pairs_only_with_a_verified_application_owned_listener(tmp_path,monkeypatch):
    monkeypatch.setattr(Path,'home',classmethod(lambda cls:tmp_path))
    file=tmp_path/'.accio/accounts/test/.accio/runtime/gateway-cli.json';file.parent.mkdir(parents=True)
    file.write_text(json.dumps({'schemaVersion':1,'url':'http://localhost:4097','pid':123,
                               'authMode':'basic','username':'TEST_ONLY_USER','password':'TEST_ONLY_LOCAL_PAIRING'}))
    monkeypatch.setattr(desktop,'owned_listener',lambda *a:False)
    with pytest.raises(RuntimeError):desktop.accio_gateway()
    monkeypatch.setattr(desktop,'owned_listener',lambda port,agent,pid:port==4097 and agent=='acciowork' and pid==123)
    cfg=desktop.accio_gateway()
    assert desktop.gateway_headers(cfg)['Authorization'].startswith('Basic ')
    assert cfg['pid']==123


def test_a_lost_native_response_is_observed_without_resending_paid_work(tmp_path,monkeypatch):
    import websocket
    job=tmp_path/'job';job.mkdir()
    (job/'transport-result.json').write_text(json.dumps({'type':'vision_probe','path':str(job/'vision-probe.txt')}))
    native.save(job/'native-session.json',{'agent':'acciowork','conversation_id':'owned-conversation',
        'agent_id':'owned-agent','result_job':str(job),'state':'send_intended'})
    cfg={'url':'http://localhost:4097','authMode':'none'}
    monkeypatch.setattr(native,'accio_gateway',lambda:cfg)
    monkeypatch.setattr(native,'gateway_request',lambda *a,**kw:pytest.fail('A resumed request must not create a new conversation'))
    sent=[];responses=iter([
        {'type':'event','method':'finished','payload':{'conversationId':'other','role':'assistant','content':'999999'}},
        {'type':'event','method':'finished','payload':{'conversationId':'owned-conversation','role':'assistant','content':'123456'}}])
    connection=SimpleNamespace(send=lambda text:sent.append(json.loads(text)),recv=lambda:json.dumps(next(responses)),close=lambda:None)
    monkeypatch.setattr(websocket,'create_connection',lambda *a,**kw:connection)
    native.accio_run(tmp_path,job,'ignored on resume',timeout=2)
    assert [row['method'] for row in sent]==['observeConversation']
    assert (job/'vision-probe.txt').read_text()=='123456'


def test_accio_new_jobs_belong_to_desktop_and_keep_the_selected_model(tmp_path,monkeypatch):
    import websocket
    native.save(tmp_path/'transport-result.json',{'type':'vision_probe','path':str(tmp_path/'vision-probe.txt')})
    monkeypatch.setattr(native,'accio_gateway',lambda:{'url':'http://localhost:4097','authMode':'none'})
    created=[];sent=[]
    def gateway(config,path,body=None):
        if path=='/agent':return {'data':[{'id':'owned-agent','name':'Accio assistant'}]}
        assert path=='/conversation';created.append(body);return {'data':{'id':'owned-conversation'}}
    monkeypatch.setattr(native,'gateway_request',gateway)
    monkeypatch.setattr(native,'input_images',lambda job:[])
    connection=SimpleNamespace(send=lambda text:sent.append(json.loads(text)),close=lambda:None,
        recv=lambda:json.dumps({'method':'finished','payload':{'role':'assistant','conversationId':'owned-conversation','content':'123456'}}))
    monkeypatch.setattr(websocket,'create_connection',lambda *a,**kw:connection)
    native.accio_run(tmp_path,tmp_path,'probe',timeout=2,model='actual-native-model')
    assert created[0]['from']=='desktop' and created[0]['type']=='dm'
    assert created[0]['sessionModel']==sent[0]['params']['model']=='actual-native-model'
    assert [message['method'] for message in sent]==['sendQuery']


def test_desktop_response_with_other_product_ids_cannot_overwrite_job_results(tmp_path):
    expected={'asin':'A','goods_id':'T','vision_revision_key':'revision'}
    (tmp_path/'manifest.jsonl').write_text(json.dumps(expected)+'\n')
    output=tmp_path/'reviews.jsonl';output.write_text('TEST_ONLY_PREVIOUS_RESULT')
    other={**expected,'goods_id':'another','visual_facts':{}}
    assert not native.write_response(tmp_path,json.dumps(other))
    assert output.read_text()=='TEST_ONLY_PREVIOUS_RESULT'


def test_doubao_receives_the_actual_contract_without_local_file_tools(tmp_path):
    manifest='{"asin":"ACTUAL_ASIN","goods_id":"ACTUAL_GOODS","vision_revision_key":"REVISION"}\n'
    (tmp_path/'manifest.jsonl').write_text(manifest)
    (tmp_path/'visual-facts-guide.json').write_text('{"feature_comparison_ids":["actual_feature"]}')
    (tmp_path/'image-inputs.json').write_text('{"images":[{"path":"original.jpg"}]}')
    request=native.doubao_request(tmp_path,'Analyze the images')
    assert manifest in request and 'actual_feature' in request and 'original.jpg' in request
    assert 'JSONL' in request


def test_doubao_keeps_all_distinct_specs_and_rules_without_repeated_scrape_payload(tmp_path):
    specs=[{'label':'Layout','value':'ISO 105 keys'},{'label':'Connection','value':'Bluetooth'}]
    row={'asin':'A','goods_id':'T','vision_revision_key':'R','amazon_title':'Keyboard',
        'amazon_product_facts':{'fields':{'title':'Keyboard','specifications':specs,'sku':specs,
            'material':'Aluminium','size':None},'gate_text':'UNRELATED_RAW_TEXT'*2000,
            'raw_record':{'session_metadata':'RAW_SESSION_DATA'*2000}}}
    (tmp_path/'manifest.jsonl').write_text(json.dumps(row)+'\n')
    guide={'product_scope':'Only standalone keyboards','visual_instructions':'Keep ISO/ANSI layout differences',
        'identity_array_item_shapes':{'differences':{'kind_values':['IDENTITY','VARIANT','UNRESOLVED']}}}
    native.save(tmp_path/'visual-facts-guide.json',guide)
    request=native.doubao_request(tmp_path,'LOCAL_FILE_AND_SHELL_INSTRUCTIONS')
    assert 'UNRELATED_RAW_TEXT' not in request and 'RAW_SESSION_DATA' not in request
    assert 'LOCAL_FILE_AND_SHELL_INSTRUCTIONS' not in request
    assert request.count('ISO 105 keys')==1 and 'Bluetooth' in request and 'Aluminium' in request
    assert json.dumps(guide,ensure_ascii=False,separators=(',',':')) in request
    assert '"asin":"A","goods_id":"T","vision_revision_key":"R"' in request
    assert len(request)<2000


def test_doubao_parses_model_jsonl_but_rejects_incomplete_identities(tmp_path):
    row={'asin':'A','goods_id':'T','vision_revision_key':'R','visual_facts':{}}
    (tmp_path/'manifest.jsonl').write_text(json.dumps(row)+'\n')
    assert native.write_response(tmp_path,'```jsonl\n'+json.dumps(row)+'\n```')
    assert native.result_exists(tmp_path)


def test_desktop_rendered_code_block_has_no_markdown_fence(tmp_path):
    row={'asin':'A','goods_id':'T','vision_revision_key':'R','visual_facts':{}}
    (tmp_path/'manifest.jsonl').write_text(json.dumps(row)+'\n')
    assert native.write_response(tmp_path,'jsonl\n'+json.dumps(row)+'\n}\nWritten one row.')
    assert json.loads((tmp_path/'reviews.jsonl').read_text())==row


def test_native_file_is_not_complete_just_because_ids_exist(tmp_path,monkeypatch):
    row={'asin':'A','goods_id':'T','vision_revision_key':'R','visual_facts':{}}
    for name in ('manifest.jsonl','reviews.jsonl'):(tmp_path/name).write_text(json.dumps(row)+'\n')
    native.save(tmp_path/'analysis-contract.json',{'category':'category.json','task_id':'owned'})
    monkeypatch.setattr('selection_automation.category_config.load_category',lambda path:{})
    monkeypatch.setattr('selection_automation.codex_visual_review.checked_facts',lambda *a:(_ for _ in ()).throw(ValueError('facts still being written')))
    assert not native.result_exists(tmp_path)


def test_workbuddy_uses_native_structured_output_with_only_its_image_tool(tmp_path,monkeypatch):
    import subprocess
    row={'asin':'A','goods_id':'T','vision_revision_key':'R','visual_facts':{}}
    (tmp_path/'manifest.jsonl').write_text(json.dumps(row)+'\n')
    (tmp_path/'visual-facts-guide.json').write_text('{"facts_output_template":{}}')
    monkeypatch.setattr(desktop,'workbuddy_cli',lambda:tmp_path/'codebuddy.exe')
    monkeypatch.setattr(local_agents,'command_prefix',lambda *a:['codebuddy.exe'])
    monkeypatch.setattr(local_agents,'environment',lambda:{})
    monkeypatch.setattr(local_agents,'workbuddy_environment',lambda *a:{})
    monkeypatch.setattr(native,'doubao_attachments',lambda job:['original.jpg'])
    def run(command,**kwargs):
        assert '--strict-mcp-config' in command and '--json-schema' in command
        assert set(command[command.index('--tools')+1].split(','))=={'Read','StructuredOutput'}
        assert 'original.jpg' in kwargs['input']
        return SimpleNamespace(returncode=0,stdout=json.dumps({'structured_output':{'reviews':[row]}}))
    monkeypatch.setattr(subprocess,'run',run)
    native.workbuddy_run(tmp_path,tmp_path,'unused')
    assert json.loads((tmp_path/'reviews.jsonl').read_text())==row


def test_workbuddy_schema_requires_canonical_identity_arrays_and_inventory_counts():
    guide={'facts_output_template':{'identity':{'matches':[],'differences':[]},
        'feature_comparisons':{'pieces':{'inventory':{'amazon':{'complete':False,'groups':[]}}}}},
        'identity_array_item_shapes':{'matches':{'example':{'feature':'','amazon':'','temu':''}},
            'differences':{'example':{'field':'','amazon':'','temu':'','kind':'','variant_field':''}}}}
    schema=native.facts_schema(guide)['properties']
    matches=schema['identity']['properties']['matches']
    assert matches['type']=='array' and matches['items']['required']==['feature','amazon','temu']
    groups=schema['feature_comparisons']['properties']['pieces']['properties']['inventory']['properties']['amazon']['properties']['groups']
    assert groups['items']['properties']['count']['type']=='integer'


def test_doubao_attachments_keep_original_bytes_when_cache_extension_is_img(tmp_path):
    from PIL import Image
    contacts=tmp_path/'contacts';contacts.mkdir()
    Image.new('RGB',(8,8),'white').save(contacts/'contact-001.jpg')
    original=tmp_path/'original.img';Image.new('RGB',(8,8),'black').save(original,format='PNG')
    (tmp_path/'image-inputs.json').write_text(json.dumps({'images':[{'path':str(original)}]}))
    attachments=native.doubao_attachments(tmp_path)
    assert len(attachments)==2 and Path(attachments[1]).suffix=='.png'
    assert Path(attachments[1]).read_bytes()==original.read_bytes()


def test_doubao_control_cannot_be_enabled_by_ordinary_detection(tmp_path,monkeypatch):
    monkeypatch.setattr(desktop,'installations',lambda agent:pytest.fail('Confirmation must precede app/process access'))
    with pytest.raises(ValueError):desktop.enable_doubao(tmp_path,'')
    with pytest.raises(ValueError):desktop.enable_doubao(tmp_path,'yes')


def test_doubao_port_owned_by_other_app_is_not_claimed(tmp_path,monkeypatch):
    import psutil
    app=tmp_path/'Doubao.exe';app.touch()
    monkeypatch.setattr(desktop,'installations',lambda agent:app)
    monkeypatch.setattr(psutil,'net_connections',lambda **kw:[SimpleNamespace(status=psutil.CONN_LISTEN,laddr=SimpleNamespace(port=9225))])
    monkeypatch.setattr(desktop,'owned_listener',lambda *a:False)
    with pytest.raises(RuntimeError,match='9225'):desktop.enable_doubao(tmp_path,'restart-doubao-and-enable-loopback-control')
