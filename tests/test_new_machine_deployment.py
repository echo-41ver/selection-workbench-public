import importlib.util
import json
import os
from pathlib import Path
import sys
from types import SimpleNamespace
import zipfile

import pytest

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'scripts'))
import company_setup
import deployment_smoke
import verify_deployment
from package_company_wizard import write_bundle
from selection_automation.company_bundle import import_bundle
from selection_automation.runtime_environment import activate
from selection_automation import secrets,adspower_kernel,network_readiness,stage3_credentials


def company_fixture():
    return {'adspower_api_key':'TEST_ONLY_COMPANY_KEY','proxy_architecture':'LOCAL_VPN',
        'preproxy_mode':'AUTO','local_proxy_endpoint':{'host':'127.0.0.1','port':17999,'protocol':'HTTP'},
        'preproxy_candidates':[{'host':'127.0.0.1','port':18999}],
        'liquid_d1':{'key':'TEST_ONLY_D1_KEY','base_url':stage3_credentials.D1_URL,'model':'d1'}}


def test_full_internal_package_keeps_company_keys_and_rediscovers_target_vpn(tmp_path):
    base=tmp_path/'base.zip';complete=tmp_path/'complete.zip'
    with zipfile.ZipFile(base,'w') as archive:
        archive.writestr('BUILD_INFO.json',json.dumps({'channel':'internal','contains_company_credentials':False}))
        archive.writestr('deploy.cmd',b'TEST_ONLY_ENTRY')
    original=company_fixture()
    write_bundle(base,complete,original)
    with zipfile.ZipFile(complete) as archive:
        exported=json.loads(archive.read('company.local.json'))
        info=json.loads(archive.read('BUILD_INFO.json'))
    assert info['unattended_company_import'] is True
    assert 'local_proxy_endpoint' not in exported
    assert exported['preproxy_candidates']==[] and exported['preproxy_mode']=='AUTO'
    assert exported['adspower_api_key']=='TEST_ONLY_COMPANY_KEY'
    assert exported['liquid_d1']['key']=='TEST_ONLY_D1_KEY'
    assert original['local_proxy_endpoint']['port']==17999
    assert 'deepseek' not in exported
    stored=[]
    import_bundle(tmp_path/'target',exported,lambda path,value:(stored.append((path.name,value)),path.write_bytes(b'TEST_ONLY_CIPHER')))
    assert len(stored)==2
    assert {p.name for p in (tmp_path/'target/runtime/secrets').glob('*.dpapi')}=={'company.dpapi','liquid_d1.dpapi'}


def test_mcp_and_background_process_find_portable_tools_with_an_empty_machine_path(tmp_path):
    for directory,binary in [('node','node.exe'),('powershell','pwsh.exe')]:
        path=tmp_path/'deployment-assets/tools'/directory/binary
        path.parent.mkdir(parents=True,exist_ok=True);path.write_bytes(b'TEST_ONLY_BINARY')
    (tmp_path/'deployment-assets/browsers').mkdir()
    target=activate(tmp_path,{'PATH':'C:\\TEST_ONLY_OLD_TOOLCHAIN'})
    assert target['PATH'].split(os.pathsep)[:2]==[str(tmp_path/'deployment-assets/tools/node'),str(tmp_path/'deployment-assets/tools/powershell')]
    assert target['PLAYWRIGHT_BROWSERS_PATH']==str(tmp_path/'deployment-assets/browsers')


def test_company_authentication_is_set_before_the_first_adspower_probe(monkeypatch):
    import replace_proxyshare_worker_environment as api_module
    checked=[]
    monkeypatch.delenv('ADSPOWER_API_KEY',raising=False)
    def api(base,path,**kwargs):
        checked.append(path)
        assert os.environ['ADSPOWER_API_KEY']=='TEST_ONLY_COMPANY_KEY'
        return {'code':0}
    monkeypatch.setattr(api_module,'api_call',api)
    company_setup.ensure_adspower_api(company_fixture())
    assert checked==['/status','/api/v1/user/list?page=1&page_size=1']


def test_fresh_headless_adspower_starts_in_its_installation_directory(tmp_path,monkeypatch):
    import replace_proxyshare_worker_environment as api_module
    calls=[];launched=[]
    executable=tmp_path/'AdsPower Global.exe'
    def api(base,path,**kwargs):
        calls.append(path)
        if len(calls)==1:raise RuntimeError('TEST_ONLY_NOT_RUNNING')
        return {'code':0}
    monkeypatch.setattr(api_module,'api_call',api)
    monkeypatch.setattr(company_setup,'find_adspower_executable',lambda company:str(executable))
    monkeypatch.setattr(company_setup.subprocess,'Popen',lambda args,**kwargs:launched.append((args,kwargs)))
    company_setup.ensure_adspower_api(company_fixture())
    assert len(launched)==1 and launched[0][1]['cwd']==str(executable.parent)
    assert '--headless=true' in launched[0][0]
    assert launched[0][1]['env']['ADSPOWER_API_KEY']=='TEST_ONLY_COMPANY_KEY'


def test_default_api_deployment_does_not_require_an_optional_agent_login(tmp_path,monkeypatch):
    secret=tmp_path/'runtime/secrets/company.dpapi';secret.parent.mkdir(parents=True);secret.write_bytes(b'TEST_ONLY_CIPHER')
    connections=tmp_path/'runtime/agent-config/connection.json';connections.parent.mkdir()
    connections.write_text(json.dumps({'mcp_ready':True,'agents':[{'id':'codex','ready':False,'authenticated':False}]}))
    proof=tmp_path/'runtime/deployment-smoke.json'
    proof.write_text(json.dumps({'ready':True,'installation':deployment_smoke.installation_token(tmp_path)}))
    monkeypatch.setattr(verify_deployment.shutil,'which',lambda name:name)
    monkeypatch.setattr(verify_deployment.subprocess,'run',lambda *a,**k:SimpleNamespace(returncode=0))
    monkeypatch.setattr(importlib.util,'find_spec',lambda name:SimpleNamespace())
    monkeypatch.setattr(secrets,'load',lambda path:company_fixture())
    monkeypatch.setattr(company_setup,'find_adspower_executable',lambda company:'TEST_ONLY_ADSPOWER.exe')
    monkeypatch.setattr(company_setup,'ensure_adspower_api',lambda company:None)
    monkeypatch.setattr(network_readiness,'select_transport',lambda company:({}, {'message':'TEST_ONLY_ROUTE'}))
    monkeypatch.setattr(adspower_kernel,'inventory',lambda *a:{'installed':True})
    monkeypatch.setattr(stage3_credentials,'saved_key',lambda *a:'TEST_ONLY_D1_KEY')
    result=verify_deployment.inspect(tmp_path)
    assert result['ready'] and result['parity_ready']
    assert result['agent_login_optional'] and not result['analysis_readiness']['codex_connected']
    assert result['analysis_readiness']['d1_connection_state']=='CONFIGURED_NOT_INVOKED'
    proof.write_text(json.dumps({'ready':True,'installation':'TEST_ONLY_OTHER_COMPUTER'}))
    assert not verify_deployment.inspect(tmp_path)['parity_ready']


def test_install_command_fails_when_only_basic_readiness_passes(monkeypatch,capsys):
    monkeypatch.setattr(sys,'argv',['verify_deployment.py','--require-parity'])
    monkeypatch.setattr(verify_deployment,'inspect',lambda:{'ready':True,'parity_ready':False})
    assert verify_deployment.main()==1
    assert json.loads(capsys.readouterr().out)['ready'] is True


def test_failed_actual_smoke_never_becomes_a_success_record(tmp_path,monkeypatch):
    def broken(root):raise RuntimeError('TEST_ONLY_MODEL_FAILURE')
    monkeypatch.setattr(deployment_smoke,'check_model',broken)
    monkeypatch.setattr(deployment_smoke,'check_chromium',lambda root:pytest.fail('must stop before browser checks'))
    result=deployment_smoke.run(tmp_path)
    assert not result['ready'] and result['paid_analysis_requests']==0
    assert result['failed_stage']=='image_model'
    assert not deployment_smoke.verified(tmp_path)
