import json
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'scripts'))
from selection_automation import agent_models,local_agents
from selection_automation.analysis_concurrency import profile
from selection_automation.stage3_provider import settings,apply,from_category


def test_reasoning_settings_survive_frozen_category_and_separate_speed_profiles():
    provider=settings({'mode':'local_agent','agent_ids':['codex'],'agent_models':{'codex':'native'},'agent_efforts':{'codex':'high'}})
    category={'stage3':{}};apply(category,provider)
    assert from_category(category)['agent_efforts']=={'codex':'high'}
    assert profile(provider)!=profile({**provider,'agent_efforts':{'codex':'low'}})
    assert settings({'mode':'local_agent'})['agent_efforts']=={}


@pytest.mark.parametrize('value',[None,[],{'unknown':'high'},{'codex':True},{'codex':'invented'},{'codex':'HIGH'}])
def test_invalid_reasoning_configuration_is_rejected(value):
    with pytest.raises(ValueError):settings({'mode':'local_agent','agent_efforts':value})


def test_only_current_native_model_levels_can_be_selected(tmp_path,monkeypatch):
    monkeypatch.setattr(agent_models,'catalog',lambda *a:{'default':'native','models':[
        {'id':'native','efforts':['low','medium','high'],'default_effort':'medium'},
        {'id':'other','efforts':['low']}]})
    agent_models.validate_efforts(tmp_path,{}, {'codex':'high'})
    agent_models.validate_efforts(tmp_path,{}, {'doubao':'default'})
    for models,efforts in [({'codex':'other'},{'codex':'high'}),({}, {'codex':'ultra'}),({}, {'doubao':'high'})]:
        with pytest.raises(ValueError):agent_models.validate_efforts(tmp_path,models,efforts)


@pytest.mark.parametrize('effort',['default','low','high','xhigh'])
def test_selected_effort_reaches_native_codex_and_default_leaves_it_unset(tmp_path,effort):
    (tmp_path/'image-inputs.json').write_text('{"images":[]}',encoding='utf8')
    command=local_agents.analysis_command({'id':'codex','executable':'codex.exe'},tmp_path,'native-model',effort)
    overrides=[part for part in command if part.startswith('model_reasoning_effort=')]
    assert overrides==([] if effort=='default' else [f'model_reasoning_effort="{effort}"'])
    assert command[command.index('-m')+1]=='native-model'


@pytest.mark.parametrize('effort,required',[('high',None),('default',None),('high','low')])
def test_pool_binds_each_group_effort_and_retains_required_effort(tmp_path,monkeypatch,effort,required):
    import psutil
    import run_codex_visual_pool as pool
    from test_codex_visual_pool import configured,fixture
    category=tmp_path/'category.json';category.write_text('{}')
    monkeypatch.setattr(pool,'load_category',lambda _:configured())
    monkeypatch.setattr(pool,'_contact_sheets',lambda *a,**k:None)
    monkeypatch.setattr(pool,'bind_original_images',lambda job,rows:(job/'image-inputs.json').write_text('{"images":[]}'))
    class Input:
        def write(self,data):pass
        def close(self):pass
    commands=[]
    monkeypatch.setattr(pool.subprocess,'Popen',lambda args,**k:commands.append(args) or SimpleNamespace(pid=12345,stdin=Input()))
    monkeypatch.setattr(psutil,'Process',lambda _:SimpleNamespace(create_time=lambda:1.0))
    worker=pool.Pool(category,{'workers':1,'agent_models':{'codex':'native-model'},'agent_efforts':{'codex':effort},
        'agent_backends':[{'id':'codex','name':'Codex','executable':'codex.exe'}]})
    task={'task_id':'one'}
    if required:task['required_effort']=required
    pair,_=fixture();worker.launch(task,[pair])
    chosen=required or effort
    assert next(iter(worker.jobs.values()))['effort']==chosen
    assert [part for part in commands[0] if part.startswith('model_reasoning_effort=')]==([] if chosen=='default' else [f'model_reasoning_effort="{chosen}"'])


@pytest.mark.parametrize('configure_only',[True,False])
def test_console_persists_effort_and_delivers_it_to_pool_without_rewriting_results(tmp_path,monkeypatch,configure_only):
    import web_console as web
    import run_codex_visual_pool as pool
    from test_stage3_local_preflight import task
    base,folder=task(tmp_path,'local_agent');before=(folder/'results.jsonl').read_bytes()
    console=web.Console(tmp_path)
    monkeypatch.setattr(console,'current_run_key',lambda:('fixture',True))
    monkeypatch.setattr(console,'stage3_snapshot',lambda key:{'total':12,'pending':12})
    monkeypatch.setattr('selection_automation.stage3_progress.controller_alive',lambda _:False)
    monkeypatch.setattr(web,'codex_review_control',lambda _:{'running':False})
    agent={'id':'codex','installed':True,'name':'Codex','executable':'codex.exe'}
    monkeypatch.setattr(local_agents,'discover',lambda *a,**k:[agent])
    monkeypatch.setattr(local_agents,'ready_agents',lambda *a,**k:[agent])
    monkeypatch.setattr(agent_models,'catalog',lambda *a:{'default':'native','models':[{'id':'native','vision':True,'efforts':['medium','high']}]})
    started=[];monkeypatch.setattr(pool,'ensure_pool',lambda *a:started.append(True))
    provider=settings({'mode':'local_agent','workers':1,'agent_ids':['codex'],'agent_models':{'codex':'native'},'agent_efforts':{'codex':'high'}})
    result=console.start_stage3({'provider':provider},configure_only=configure_only)
    assert result['ok'] and from_category(web.selection.read(base/'category.json'))['agent_efforts']=={'codex':'high'}
    assert (folder/'results.jsonl').read_bytes()==before
    assert bool(started) is not configure_only
    if not configure_only:assert web.selection.read(folder/'codex_pool_config.json')['agent_efforts']=={'codex':'high'}


def test_web_reasoning_dropdown_and_payload():
    subprocess.run(['node','--test',str(ROOT/'tests/web_agent_reasoning.test.mjs')],check=True,cwd=ROOT,timeout=20)
