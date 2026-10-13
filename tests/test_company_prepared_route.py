import io,json,pathlib,sys
from types import SimpleNamespace
import pytest
sys.path.insert(0,str(pathlib.Path(__file__).resolve().parents[1]/"scripts"))
import company_setup as company
from selection_automation.local_proxy_prepare import reuse_prepared,prepare
from selection_automation.network_readiness import NetworkNotReady
from selection_automation.proxy_activity import ProxyActivityLease


def route_fixture(root):
    saved={"proxy_host":"gateway.example","proxy_port":5959,"preproxy_host":"127.0.0.1",
           "preproxy_port":7890,"preproxy_protocol":"SOCKS5","preproxy_mode":"AUTO",
           "local_proxy_owner":{"pid":123,"created":100,"name":"clash-meta.exe"}}
    config={"proxy":{"host":"gateway.example","port":5959},"preproxy_mode":"AUTO"}
    def read(path,default=None):
        return saved if path.name=="proxyshare.settings.json" else default
    return saved,config,read


def test_active_lease_reuses_identical_owned_route_without_discovery(tmp_path):
    saved,config,read=route_fixture(tmp_path)
    owners=[]
    with ProxyActivityLease(tmp_path,1,purpose="manual_verification"):
        with pytest.raises(NetworkNotReady):
            prepare(tmp_path,config,read,lambda *a:pytest.fail("write"),observation=lambda:pytest.fail("discover"))
        transport,status=reuse_prepared(tmp_path,config,read,check_owner=lambda owner:owners.append(owner) or True)
    assert transport["preproxy_port"]==7890 and status["route_reused"]
    assert owners[0]["pid"]==123 and owners[0]["created"]==100
    assert saved["preproxy_port"]==7890


@pytest.mark.parametrize("change",["gateway","endpoint","owner","mode"])
def test_changed_route_is_refused_without_adoption(tmp_path,change):
    saved,config,read=route_fixture(tmp_path)
    if change=="gateway":config["proxy"]["host"]="other.example"
    if change=="endpoint":config["local_proxy_endpoint"]={"host":"127.0.0.1","port":7891,"protocol":"SOCKS5"}
    if change=="mode":config["preproxy_mode"]="DIRECT"
    with pytest.raises(NetworkNotReady):
        reuse_prepared(tmp_path,config,read,check_owner=lambda _:change!="owner")
    assert saved["preproxy_port"]==7890


@pytest.mark.parametrize("task,machine,used,ready,expected",[
    ("task","machine",False,True,True),("other","machine",False,True,False),
    ("task","other",False,True,False),("task","machine",True,True,True),
    ("task","machine",False,False,False)])
def test_reuse_requires_same_installation_task_and_prepared_checkpoint(tmp_path,task,machine,used,ready,expected):
    journal={"task_key":task,"machine_id":machine,"used":used,"proxy_sessions_ready":ready,"workers":[{}]}
    assert company.prepared_for_task(tmp_path,"task","machine",lambda p,d:journal if p.name=="bootstrap.json" else d)==expected


@pytest.mark.parametrize("task,machine,expected",[
    ("task","machine",True),("other","machine",False),("task","other",False)])
def test_used_manifest_authorizes_only_same_task_route_validation(tmp_path,task,machine,expected):
    manifest={"task_key":task,"machine_id":machine,"used":True,"workers":[{"profile_id":"old"}]}
    assert company.prepared_for_task(tmp_path,"task","machine",lambda p,d:manifest if p.name=="sessions.json" else d)==expected


def test_deleted_lane_profile_is_terminal_for_task_restarts(tmp_path):
    import selection
    path=tmp_path/'runs/task/temu/supervisors/worker-01-new.json'
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({'deleted_profile_id':'profile-1'}))
    with pytest.raises(RuntimeError,match='环境已退役'):
        company.reject_retired_task_profiles(tmp_path,'task')


def fake_bindings(tmp_path,monkeypatch,count=4,fail=False):
    import urllib.request
    workers=[];urls=[]
    for i in range(count):
        sid=tmp_path/f"sid{i}.txt";sid.write_text(f"test-{i}")
        workers.append({"name":f"worker-{i}","session_id_file":sid.name,"webdriver_url":f"http://127.0.0.1:{4451+i}"})
    def open_url(url,timeout):
        urls.append(url)
        return io.StringIO(json.dumps({"value":{} if fail else "about:blank"}))
    def build(handler):
        assert handler.proxies=={}  # Local binding probes cannot use system proxy.
        return SimpleNamespace(open=open_url)
    monkeypatch.setattr(urllib.request,"build_opener",build)
    return {"workers":workers},urls


def test_complete_bindings_are_independently_verified(tmp_path,monkeypatch):
    manifest,urls=fake_bindings(tmp_path,monkeypatch)
    assert company.verify_prepared_bindings(tmp_path,lambda p,d:manifest)==manifest
    assert len(urls)==4


def test_stale_binding_fails_without_recreating_environment(tmp_path,monkeypatch):
    manifest,urls=fake_bindings(tmp_path,monkeypatch,fail=True)
    with pytest.raises(RuntimeError,match="worker-0"):
        company.verify_prepared_bindings(tmp_path,lambda p,d:manifest)
    assert len(urls)==1


def test_completed_bootstrap_does_not_recurse_retire_or_prepare_again(tmp_path,monkeypatch):
    import selection,replace_proxyshare_worker_environment as replace
    import setup_proxyshare_adspower_workers as setup
    import selection_automation.local_proxy_prepare as routes
    import selection_automation.adspower_kernel as kernel
    import selection_automation.worker_runtime as worker_runtime
    import selection_automation.flower_background as background
    monkeypatch.setattr(background,"install_defaults",lambda *a:{"existing_browsers_changed":False})
    secret=tmp_path/"runtime/secrets/company.dpapi";secret.parent.mkdir(parents=True);secret.touch()
    checkpoint={"task_key":"task","machine_id":"machine","used":False,"proxy_sessions_ready":True,"workers":[{}]}
    completed=[False];events=[]
    def read(path,default=None):
        if path.name=="bootstrap.json":return checkpoint
        if path.name=="sessions.json":return {"workers":[{}]} if completed[0] else {}
        return default
    monkeypatch.setattr(company,"ROOT",tmp_path)
    monkeypatch.setattr(company,"load",lambda _: {"adspower_api_key":"test","proxy":{}})
    monkeypatch.setattr(selection,"identity",lambda:"machine")
    monkeypatch.setattr(selection,"read",read)
    monkeypatch.setattr(company,"ensure_adspower_api",lambda _:None)
    monkeypatch.setattr(kernel,"ensure_kernel",lambda *a:None)
    monkeypatch.setattr(routes,"prepare",lambda *a:pytest.fail("cannot probe active route"))
    monkeypatch.setattr(routes,"reuse_prepared",lambda *a:(events.append("reuse") or {},{}))
    monkeypatch.setattr(setup,"retire_for_task",lambda _:events.append("retire"))
    monkeypatch.setattr(worker_runtime,"clear_stale_stop_requests",lambda *a:None)
    def run(*args,**kwargs):
        events.append("setup");completed[0]=True
        (tmp_path/"config").mkdir();(tmp_path/"config/sessions.json").touch()
    monkeypatch.setattr(company.subprocess,"run",run)
    monkeypatch.setattr(company,"verify_prepared_bindings",lambda *a:events.append("verify"))
    company.ensure_company(True,new_task="task")
    assert events==["reuse","retire","setup","verify"]


def continuation_fixture(tmp_path,monkeypatch,owner_valid=True):
    import selection,replace_proxyshare_worker_environment as replace
    import setup_proxyshare_adspower_workers as setup
    import selection_automation.local_proxy_prepare as routes
    import selection_automation.adspower_kernel as kernel
    import selection_automation.worker_runtime as worker_runtime
    import selection_automation.flower_background as background
    import selection_automation.profile_capacity as capacity
    saved,config,_=route_fixture(tmp_path)
    config['adspower_api_key']='test'
    config['proxy'].update(username='TEST_ONLY_USER_session-resume',password='TEST_ONLY_PASSWORD')
    sessions=tmp_path/'config/sessions.json'
    sessions.parent.mkdir()
    original={'task_key':'task','machine_id':'machine','used':True,
              'workers':[{'name':'worker-01-new','profile_id':'old'}]}
    sessions.write_text(json.dumps(original))
    secret=tmp_path/'runtime/secrets/company.dpapi'
    secret.parent.mkdir(parents=True);secret.touch()
    events=[]
    def read(path,default=None):
        if path.name=='proxyshare.settings.json':return saved
        if path==tmp_path/'config/proxy_policy.json':return {}
        return json.loads(path.read_text()) if path.exists() else default
    def save(path,value):
        path.parent.mkdir(parents=True,exist_ok=True)
        path.write_text(json.dumps(value))
    monkeypatch.setattr(company,'ROOT',tmp_path)
    monkeypatch.setattr(company,'load',lambda _:config)
    monkeypatch.setattr(selection,'identity',lambda:'machine')
    monkeypatch.setattr(selection,'read',read)
    monkeypatch.setattr(selection,'save',save)
    monkeypatch.setattr(selection,'live_owner',lambda:{'category_key':'task'})
    monkeypatch.setattr(company,'ensure_adspower_api',lambda _:events.append('api'))
    monkeypatch.setattr(kernel,'ensure_kernel',lambda *a:None)
    monkeypatch.setattr(background,'install_defaults',lambda *a:{})
    monkeypatch.setattr(routes,'prepare',lambda *a:pytest.fail('must not discover an active route'))
    def reuse(*args):
        events.append('reuse')
        return reuse_prepared(*args,check_owner=lambda _:owner_valid)
    monkeypatch.setattr(routes,'reuse_prepared',reuse)
    monkeypatch.setattr(setup,'ROOT',tmp_path)
    monkeypatch.setattr(setup,'SESSIONS',sessions)
    monkeypatch.setattr(setup,'api_base',lambda:'local')
    monkeypatch.setattr(capacity,'list_profiles',lambda *a:[{'user_id':'old'}])
    monkeypatch.setattr(setup,'api_call',lambda *a,**k:events.append('stop-old') or {})
    monkeypatch.setattr(replace,'delete_profile_after_release',lambda *a:events.append('delete-old'))
    monkeypatch.setattr(worker_runtime,'clear_stale_stop_requests',lambda *a:None)
    monkeypatch.setattr(company,'free_block',lambda start,count:list(range(start,start+count)))
    monkeypatch.setattr(company,'rebind_existing_worker',lambda *a:pytest.fail('must not reopen an old profile'))
    def run(command,**kwargs):
        if any(str(part).endswith('setup_proxyshare_adspower_workers.py') for part in command):
            assert command[-2:]==['--new-task','task']
            assert not sessions.exists()
            events.append('create-new')
            save(sessions,{**original,'used':False,'workers':[{'name':'worker-01-new','profile_id':'new'}]})
    monkeypatch.setattr(company.subprocess,'run',run)
    monkeypatch.setattr(company,'verify_prepared_bindings',lambda *a:events.append('verify-new'))
    return sessions,events,original


@pytest.mark.parametrize('task_argument',['new_task','resume_task'])
def test_same_task_continuation_reuses_active_route_and_creates_new_profiles(tmp_path,monkeypatch,task_argument):
    sessions,events,_=continuation_fixture(tmp_path,monkeypatch)
    with ProxyActivityLease(tmp_path,1,purpose='manual_verification'):
        company.ensure_company(True,worker_count=1,**{task_argument:'task'})
    assert events==['reuse','api','stop-old','delete-old','create-new','verify-new']
    assert json.loads(sessions.read_text())['workers'][0]['profile_id']=='new'


def test_changed_owner_on_continuation_refuses_before_retiring_or_creating_profiles(tmp_path,monkeypatch):
    sessions,events,original=continuation_fixture(tmp_path,monkeypatch,owner_valid=False)
    with ProxyActivityLease(tmp_path,1,purpose='manual_verification'):
        with pytest.raises(NetworkNotReady):
            company.ensure_company(True,worker_count=1,resume_task='task')
    assert events==['reuse']
    assert json.loads(sessions.read_text())==original


@pytest.mark.parametrize('changed_field',['task_key','machine_id'])
def test_other_task_or_installation_cannot_reuse_active_route(tmp_path,monkeypatch,changed_field):
    import selection_automation.local_proxy_prepare as routes
    sessions,events,original=continuation_fixture(tmp_path,monkeypatch)
    original[changed_field]='other'
    sessions.write_text(json.dumps(original))
    def guarded_prepare(*args):
        events.append('prepare')
        return prepare(*args,observation=lambda:pytest.fail('must not discover an active route'))
    monkeypatch.setattr(routes,'prepare',guarded_prepare)
    with ProxyActivityLease(tmp_path,1,purpose='manual_verification'):
        with pytest.raises(NetworkNotReady):
            company.ensure_company(True,worker_count=1,resume_task='task')
    assert events==['prepare']
    assert json.loads(sessions.read_text())==original
