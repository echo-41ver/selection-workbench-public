import json,pathlib,sys,pytest
from types import SimpleNamespace
sys.path.insert(0,str(pathlib.Path(__file__).resolve().parents[1]/'scripts'))
from selection_automation.company_bundle import normalize
from selection_automation.local_proxy_prepare import prepare
from selection_automation import local_proxy
import web_console,verify_deployment

def candidate(protocol='SOCKS5'):
    return {'host':'127.0.0.1','port':45678,'protocol':protocol,'pid':101,'created':100.0,'name':'clash-meta.exe'}
def company(**extra):
    return {'adspower_api_key':'TEST_ONLY_ADSPOWER','proxy':{'host':'proxy.example','port':5959,
        'username':'TEST_ONLY_USER_session-fixture','password':'TEST_ONLY_PASSWORD'},**extra}

@pytest.mark.parametrize('host,protocol',[('127.0.0.1','AUTO'),('::1','HTTP'),('localhost','SOCKS5')])
def test_new_endpoint_roundtrip_keeps_legacy_bundle_compatible(host,protocol):
    endpoint={'host':host,'port':45678,'protocol':protocol}
    assert normalize(company(local_proxy_endpoint=endpoint))['local_proxy_endpoint']==endpoint
    assert 'local_proxy_endpoint' not in normalize(company())

@pytest.mark.parametrize('endpoint',[
    {'host':'outside.example','port':45678},
    {'host':'127.0.0.1','port':True},
    {'host':'127.0.0.1','port':0},
    {'host':'127.0.0.1','port':45678,'password':'TEST_ONLY_SECRET'},
    {'host':'127.0.0.1','port':45678,'protocol':'UNKNOWN'},
])
def test_endpoint_contract_cannot_import_remote_or_new_credentials(endpoint):
    with pytest.raises(ValueError):normalize(company(local_proxy_endpoint=endpoint))

def test_idle_adoption_preserves_gateway_and_checkpoint_configuration(tmp_path):
    saved={'proxy_host':'proxy.example','proxy_port':5959,'preproxy_port':0,'preproxy_host':'127.0.0.1',
           'chain_base_port':17891,'worker_count':4}
    writes={}
    def read(path,default):return dict(saved) if path.name=='proxyshare.settings.json' else default
    transport,status=prepare(tmp_path,company(),read,lambda p,v:writes.update({p.name:v}),
        observation=lambda:{'status':'READY','candidates':[candidate()]},business_active=lambda root:False,check_owner=lambda c:True)
    result=writes['proxyshare.settings.json']
    assert result['proxy_host']==saved['proxy_host'] and result['worker_count']==4 and result['chain_base_port']==17891
    assert result['preproxy_port']==45678 and result['local_proxy_owner']['pid']==101
    assert status['target_access']=='UNVERIFIED'

def test_new_owner_failure_never_overwrites_existing_route(tmp_path):
    writes={}
    with pytest.raises(RuntimeError):prepare(tmp_path,company(),lambda p,d:{},lambda p,v:writes.update({p.name:v}),
        observation=lambda:{'status':'READY','candidates':[candidate()]},business_active=lambda root:False,check_owner=lambda c:False)
    assert 'proxyshare.settings.json' not in writes

def test_console_choice_does_not_start_or_change_actual_proxy(tmp_path,monkeypatch):
    console=web_console.Console(tmp_path)
    monkeypatch.setattr(console,'owner',lambda:None)
    monkeypatch.setattr(local_proxy,'discover',lambda:{'status':'READY','candidates':[candidate('HTTP')]})
    monkeypatch.setattr(local_proxy,'owned',lambda c:True)
    result=console.select_local_proxy({'host':'127.0.0.1','port':45678,'protocol':'HTTP'})
    assert result['ok']
    assert json.loads((tmp_path/'runtime/local-proxy-choice.json').read_text())['protocol']=='HTTP'
    assert not (tmp_path/'runtime/secrets/proxyshare.settings.json').exists()

def test_console_running_task_rejects_choice_before_discovery(tmp_path,monkeypatch):
    console=web_console.Console(tmp_path);console.busy=True
    monkeypatch.setattr(local_proxy,'discover',lambda:pytest.fail('must not probe during collection'))
    with pytest.raises(web_console.ConsoleError):console.select_local_proxy({'host':'127.0.0.1','port':45678,'protocol':'HTTP'})
    assert not (tmp_path/'runtime/local-proxy-choice.json').exists()

def test_deployment_preflight_reports_auto_discovery_failure_without_external_proxy_probe(tmp_path,monkeypatch):
    secret=tmp_path/'runtime/secrets/company.dpapi';secret.parent.mkdir(parents=True);secret.write_text('TEST_ONLY_STUB')
    from selection_automation import secrets,network_readiness
    import company_setup
    monkeypatch.setattr(secrets,'load',lambda p:company())
    monkeypatch.setattr(verify_deployment.importlib.util,'find_spec',lambda name:object())
    monkeypatch.setattr(verify_deployment.shutil,'which',lambda name:'TEST_ONLY_EXECUTABLE')
    monkeypatch.setattr(verify_deployment.subprocess,'run',lambda *a,**kw:SimpleNamespace(returncode=0))
    monkeypatch.setattr(company_setup,'find_adspower_executable',lambda company:'TEST_ONLY_ADSPOWER')
    monkeypatch.setattr(company_setup,'ensure_adspower_api',lambda company:pytest.fail('preflight missing network must stop before API'))
    monkeypatch.setattr(local_proxy,'discover',lambda:{'status':'NO_COMPATIBLE_PROXY','candidates':[],'running_client_count':0})
    result=verify_deployment.inspect(tmp_path)
    assert not result['ready'] and result['network_preflight']['target_access']=='UNVERIFIED'
    assert any('先启动' in message for message in result['missing'])
