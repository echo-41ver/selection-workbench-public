import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from selection_automation import agent_models as models
from selection_automation import local_agents
from selection_automation import native_agent_transport as native
from selection_automation.stage3_provider import settings,apply,from_category


def test_each_agent_model_survives_settings_and_frozen_category():
    selected={'codex':'native-codex-model','acciowork':'opaque-native-model-id','doubao':'豆包 专家'}
    config=settings({'mode':'local_agent','agent_models':selected})
    category={'stage3':{}};apply(category,config)
    assert from_category(category)['agent_models']==selected
    for value in ({'unknown':'x'},{'codex':'bad\nmodel'},{'codex':123}):
        with pytest.raises(ValueError):settings({'mode':'local_agent','agent_models':value})


def test_codex_reads_native_pages_without_starting_an_llm_turn(tmp_path,monkeypatch):
    methods=[]
    class Peer:
        def __init__(self,*args):pass
        def rpc(self,method,params):
            methods.append(method)
            if method=='initialize':return {}
            if params.get('cursor')=='next':return {'data':[{'model':'native-two','displayName':'Native two','inputModalities':['text']}],'nextCursor':None}
            return {'data':[{'model':'native-one','displayName':'Native one','inputModalities':['text','image'],'isDefault':True}],'nextCursor':'next'}
        def send(self,message):assert message['method']=='initialized'
        def close(self):pass
    monkeypatch.setattr(models,'Peer',Peer)
    monkeypatch.setattr(local_agents,'command_prefix',lambda *a:['codex'])
    rows,default,_=models._codex(tmp_path,{'executable':'codex'})
    assert [m['id'] for m in rows]==['native-one','native-two'] and default=='native-one'
    assert rows[0]['vision'] is True and rows[1]['vision'] is False
    assert methods==['initialize','model/list','model/list']


def test_acp_reads_actual_model_ids_and_image_capabilities(tmp_path,monkeypatch):
    class Peer:
        def __init__(self,*args):pass
        def rpc(self,method,params):
            if method=='initialize':return {}
            assert method=='session/new' and params['mcpServers']==[]
            return {'models':{'currentModelId':'native-id','availableModels':[{'modelId':'native-id','name':'Account model','_meta':{'supportsImages':True}}]}}
        def close(self):pass
    monkeypatch.setattr(models,'Peer',Peer)
    monkeypatch.setattr(local_agents,'command_prefix',lambda *a:['native'])
    monkeypatch.setattr(local_agents,'workbuddy_environment',lambda *a:{})
    rows,current,_=models._acp(tmp_path,{'id':'workbuddy','name':'WorkBuddy','executable':'native'})
    assert current==rows[0]['id']=='native-id' and rows[0]['vision'] is True


def test_claude_uses_control_initialization_without_sending_a_prompt(tmp_path,monkeypatch):
    sent=[]
    class Peer:
        def __init__(self,*args):pass
        def send(self,row):sent.append(row)
        def wait(self,predicate):
            row={'type':'control_response','response':{'request_id':'selection-model-reader','subtype':'success','response':{'models':[{'value':'account-model','displayName':'Account model'}]}}}
            assert predicate(row);return row
        def close(self):pass
    monkeypatch.setattr(models,'Peer',Peer)
    monkeypatch.setattr(local_agents,'command_prefix',lambda *a:['claude'])
    rows,_,_=models._claude(tmp_path,{'executable':'claude'})
    assert rows[0]['id']=='account-model' and len(sent)==1
    assert sent[0]['request']['subtype']=='initialize' and sent[0]['type']=='control_request'


def test_accio_uses_server_codes_and_hides_unavailable_models(tmp_path,monkeypatch):
    from selection_automation import desktop_agents
    monkeypatch.setattr(desktop_agents,'accio_gateway',lambda:{})
    monkeypatch.setattr(desktop_agents,'gateway_request',lambda *a:{'data':[{'provider':'native','modelList':[
        {'modelCode':'opaque-code','modelDisplayName':'Actual model','multimodal':True},
        {'modelCode':'hidden-code','modelDisplayName':'Hidden','visible':False}]}]})
    rows,_,_=models._accio(tmp_path,{})
    assert len(rows)==1 and rows[0]['id']=='opaque-code' and rows[0]['name']=='Actual model'


def test_model_catalog_cache_is_bound_to_installation_root(tmp_path,monkeypatch):
    executable=tmp_path/'codex.exe';executable.touch();calls=[]
    agent={'id':'codex','name':'Codex','installed':True,'executable':str(executable),'authenticated':True}
    def read(root,agent):
        calls.append(str(root));return [models._model('actual-id')],'actual-id','native'
    monkeypatch.setattr(models,'_codex',read)
    assert models.catalog(tmp_path/'first','codex',agent=agent)['models']
    models.catalog(tmp_path/'first','codex',agent=agent)
    models.catalog(tmp_path/'second','codex',agent=agent)
    assert len(calls)==2


def test_unavailable_or_text_only_model_cannot_be_saved_as_a_vision_choice(tmp_path,monkeypatch):
    monkeypatch.setattr(models,'catalog',lambda *a:{'models':[{'id':'vision','vision':True},{'id':'text','vision':False}]})
    models.validate_selection(tmp_path,{'codex':'vision'})
    with pytest.raises(ValueError):models.validate_selection(tmp_path,{'codex':'invented'})
    with pytest.raises(ValueError):models.validate_selection(tmp_path,{'codex':'text'})


@pytest.mark.parametrize('agent_id',local_agents.IDS)
def test_selected_model_reaches_every_native_agent_command(tmp_path,agent_id):
    (tmp_path/'image-inputs.json').write_text('{"images":[]}')
    agent={'id':agent_id,'executable':'native.exe','selected_model':'ACTUAL_SELECTED_MODEL'}
    command=local_agents.analysis_command(agent,tmp_path)
    assert 'ACTUAL_SELECTED_MODEL' in command


def test_vision_receipt_for_one_model_does_not_authorize_another(tmp_path,monkeypatch):
    executable=tmp_path/'codex.exe';executable.touch()
    monkeypatch.setattr(local_agents,'find_executable',lambda *a:str(executable))
    monkeypatch.setattr(local_agents,'_run',lambda *a,**kw:SimpleNamespace(returncode=0,stdout='codex-cli 1.2.3'))
    receipt=local_agents.connection_receipt(tmp_path,'codex','one');receipt.parent.mkdir(parents=True)
    import time
    receipt.write_text(json.dumps({'executable':str(executable),'version':'1.2.3','connected':True,'probe_revision':2,'tested_at':time.time(),'selected_model':'one'}))
    assert local_agents._probe_cli(tmp_path,'codex','one')['ready']
    assert not local_agents._probe_cli(tmp_path,'codex','two')['ready']


def test_busy_doubao_uses_recent_native_catalog_without_interrupting_upload(tmp_path,monkeypatch):
    import os,time
    executable=tmp_path/'doubao.exe';executable.touch()
    agent={'id':'doubao','name':'豆包','installed':True,'executable':str(executable)}
    path=tmp_path/'runtime/agent-config/model-catalogs/doubao.json';path.parent.mkdir(parents=True)
    path.write_text(json.dumps({'id':'doubao','status':'ready','models':[{'id':'actual-expert'}]}))
    def busy(*args):raise RuntimeError('Another supervisor owns this installation')
    monkeypatch.setattr(models,'_doubao',busy)
    result=models.catalog(tmp_path,'doubao',refresh=True,agent=agent)
    assert result['status']=='ready' and result['cached'] and result['models'][0]['id']=='actual-expert'
    os.utime(path,(time.time()-901,time.time()-901))
    assert models.catalog(tmp_path,'doubao',refresh=True,agent=agent)['status']=='unavailable'
    path.write_text(json.dumps({'id':'doubao','status':'ready','models':[{'id':'actual-expert'}],'native_identity':['other-installation']}))
    assert models.catalog(tmp_path,'doubao',refresh=True,agent=agent)['status']=='unavailable'


def test_accio_selected_model_is_sent_to_the_native_gateway(tmp_path,monkeypatch):
    import websocket
    monkeypatch.setattr(native,'accio_gateway',lambda:{'url':'http://localhost:4097'})
    monkeypatch.setattr(native,'gateway_request',lambda config,path,body=None:{'data':[{'id':'agent','name':'assistant'}]} if path=='/agent' else {'data':{'id':'conversation'}})
    (tmp_path/'transport-result.json').write_text(json.dumps({'type':'vision_probe','path':str(tmp_path/'vision-probe.txt')}))
    sent=[]
    class Connection:
        def send(self,message):sent.append(json.loads(message))
        def recv(self):return json.dumps({'method':'finished','payload':{'role':'assistant','conversationId':'conversation','content':'123456'}})
        def close(self):pass
    monkeypatch.setattr(websocket,'create_connection',lambda *a,**kw:Connection())
    native.accio_run(tmp_path,tmp_path,'read the image',timeout=1,model='opaque-selected-code')
    assert sent[0]['params']['model']=='opaque-selected-code'
    assert (tmp_path/'vision-probe.txt').read_text()=='123456'


def test_uncertain_native_task_cannot_be_resubmitted_with_a_different_model(tmp_path,monkeypatch):
    monkeypatch.setattr(native,'accio_gateway',lambda:{'url':'http://localhost:4097'})
    native.save(tmp_path/'native-session.json',{'agent':'acciowork','model':'original','state':'send_intended'})
    with pytest.raises(RuntimeError,match='模型不同'):native.accio_run(tmp_path,tmp_path,'ignored',model='different')
