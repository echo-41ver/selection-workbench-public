import json
from pathlib import Path
from types import SimpleNamespace
import sys

import pytest

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
from selection_automation import local_agents
from selection_automation.analysis_concurrency import ThroughputTuner
from selection_automation.company_bundle import import_bundle
from selection_automation.secrets import load, store
from selection_automation.stage3_credentials import saved_key, save_key
from selection_automation.stage3_provider import settings,from_category
from selection_automation.category_contract import category_contract


def test_new_tasks_default_to_d1_but_legacy_frozen_tasks_keep_service():
    stage=category_contract()['template']['stage3']
    assert stage['provider']['mode']=='liquid_d1' and stage['batch_size']==1
    assert settings()['mode']=='liquid_d1' and settings()['concurrency_mode']=='auto'
    old={'stage3':{'base_url':'https://api.deepseek.com','model':'deepseek-flash','max_concurrency':6}}
    assert from_category(old)['mode']=='multimodal_api'
    assert from_category(old)['workers']==6
    assert from_category(old)['concurrency_mode']=='manual'


def test_saved_d1_survives_restart_and_never_crosses_endpoint(tmp_path,monkeypatch):
    for name in ['LIQUID_API_KEY','D1_API_KEY','SELECTION_STAGE3_API_KEY','SELECTION_STAGE3_ENDPOINT']:
        monkeypatch.delenv(name,raising=False)
    provider=settings();save_key(tmp_path,provider,'TEST_ONLY_D1_CREDENTIAL')
    ciphertext=(tmp_path/'runtime/secrets/liquid_d1.dpapi').read_bytes()
    assert b'TEST_ONLY_D1_CREDENTIAL' not in ciphertext
    assert saved_key(tmp_path,settings())=='TEST_ONLY_D1_CREDENTIAL'
    assert not saved_key(tmp_path,settings({'mode':'liquid_d1','base_url':'https://another.example/v1'}))


def test_import_d1_failure_restores_all_three_credentials(tmp_path,monkeypatch):
    config={'schema_version':'company-bundle-v2','adspower_api_key':'TEST_ONLY_ADS','proxy_architecture':'LOCAL_VPN',
            'deepseek':{'key':'TEST_ONLY_DEEPSEEK'},'liquid_d1':{'key':'TEST_ONLY_D1'}}
    import_bundle(tmp_path,config,store)
    folder=tmp_path/'runtime/secrets';before={p.name:p.read_bytes() for p in folder.glob('*.dpapi')}
    assert load(folder/'liquid_d1.dpapi')['key']=='TEST_ONLY_D1'
    assert 'liquid_d1' not in load(folder/'company.dpapi')
    import selection_automation.company_bundle as bundle
    replace=bundle.os.replace;failed=False
    def fail_d1(source,target):
        nonlocal failed
        if Path(target).name=='liquid_d1.dpapi' and not failed:
            failed=True;raise OSError('test publication failure')
        return replace(source,target)
    monkeypatch.setattr(bundle.os,'replace',fail_d1)
    config['liquid_d1']['key']='TEST_ONLY_NEW_D1'
    with pytest.raises(RuntimeError,match='restored'):import_bundle(tmp_path,config,store)
    assert all((folder/name).read_bytes()==data for name,data in before.items())


def test_auth_status_is_not_a_connected_vision_agent(tmp_path,monkeypatch):
    exe=tmp_path/'codex.exe';exe.touch()
    monkeypatch.setattr(local_agents,'find_executable',lambda root,agent:str(exe) if agent=='codex' else None)
    monkeypatch.setattr(local_agents,'_run',lambda prefix,args,root,**kw:SimpleNamespace(returncode=0,stdout='codex-cli 0.162.0',stderr=''))
    rows=local_agents.discover(tmp_path,refresh=True)
    codex=rows[0]
    assert codex['installed'] and codex['authenticated']
    assert not codex['connected'] and not codex['ready']
    assert not local_agents.ready_agents(tmp_path)


def test_agent_adapters_bind_images_and_avoid_cross_vendor_model(tmp_path):
    job=tmp_path/'job with spaces';job.mkdir()
    (job/'image-inputs.json').write_text(json.dumps({'images':[{'path':str(job/'original image.jpg')}]}))
    for agent_id in local_agents.IDS:
        command=local_agents.analysis_command({'id':agent_id,'executable':'tool.exe'},job)
        assert 'gpt-6.1-sol' in command if agent_id=='codex' else 'gpt-6.1-sol' not in command
        assert any(str(job) in part for part in command)
        if agent_id=='codex':assert str(job/'original image.jpg') in command and '--skip-git-repo-check' in command
        if agent_id=='claude':assert '--add-dir' in command and '--permission-mode' in command
        if agent_id=='gemini':assert '--include-directories' in command


def test_local_agent_environment_never_receives_d1_keys(monkeypatch):
    for key in ['LIQUID_API_KEY','D1_API_KEY','SELECTION_STAGE3_API_KEY']:
        monkeypatch.setenv(key,'TEST_ONLY_SECRET')
    assert not {'LIQUID_API_KEY','D1_API_KEY','SELECTION_STAGE3_API_KEY'} & local_agents.environment().keys()


def test_concurrency_follows_measured_throughput_and_reverts_flat_or_failure():
    tuner=ThroughputTuner(4,16)
    assert tuner.observe(8,0,4)==8
    assert tuner.observe(16,0,4)==16
    assert tuner.observe(32,0,10)==8  # 16 workers was slower than 8.
    assert tuner.best_width==8
    assert tuner.observe(7,1,2)==4
    assert tuner.width<=tuner.maximum


def test_bundled_cli_wins_over_machine_specific_path(tmp_path,monkeypatch):
    exe=tmp_path/'runtime/tools/codex/codex.exe';exe.parent.mkdir(parents=True);exe.touch()
    monkeypatch.setattr(local_agents.shutil,'which',lambda name:'C:/unrelated/codex.exe')
    assert local_agents.find_executable(tmp_path,'codex')==str(exe.resolve())
