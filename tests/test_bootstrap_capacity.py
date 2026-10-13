"""Account capacity adaptation uses local fake APIs and never deletes profiles."""
import importlib.util
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
CAPACITY = "If the number of imported accounts exceeds the limit of 12, please delete some accounts and try again."


def load_script(name):
    spec = importlib.util.spec_from_file_location(name + "_capacity_test", ROOT / "scripts" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def setup_fixture(tmp_path, monkeypatch):
    import selection
    module = load_script("setup_proxyshare_adspower_workers")
    monkeypatch.setattr(module, "ROOT", tmp_path)
    monkeypatch.setattr(module, "SESSIONS", tmp_path / "config/sessions.json")
    monkeypatch.setattr(module, "api_base", lambda: "http://fixture")
    monkeypatch.setattr(module, "powershell", lambda: "fake-pwsh")
    monkeypatch.setattr(selection, "identity", lambda: "fixture-installation")
    monkeypatch.setattr(sys, "argv", ["setup"])
    policy = tmp_path / "runtime/proxy_policy.json"
    policy.parent.mkdir()
    policy.write_text(json.dumps({"worker_chain_ports": [22001, 22002, 22003, 22004],
                                  "webdriver_ports": [23001, 23002, 23003, 23004]}), encoding="utf-8")
    driver = tmp_path / "driver.exe"
    driver.write_bytes(b"fixture")
    calls, commands = [], []
    monkeypatch.setattr(module, "run_background_launcher", lambda command, **_: (
        commands.append(command) or SimpleNamespace(returncode=0, stdout="", stderr="")))
    return module, calls, commands, driver, policy


def seed_bound(module, count):
    workers = [{"name": f"worker-{number:02d}-new", "profile_id": f"owned-{number}",
                "bootstrap_status": "BOUND", "webdriver_url": f"http://127.0.0.1:{23000 + number}",
                "session_id_file": f"runtime/worker-{number:02d}-new/sid", "bidi_url_file": f"runtime/worker-{number:02d}-new/bidi"}
               for number in range(1, count + 1)]
    module.atomic_json(module.ROOT / "runtime/bootstrap.json", {"machine_id": "fixture-installation", "workers": workers})


@pytest.mark.parametrize("resume", [False, True])
def test_capacity_never_publishes_three_workers(setup_fixture, monkeypatch, resume):
    module,calls,commands,driver,policy=setup_fixture
    if resume: seed_bound(module,3)
    created=[3 if resume else 0]
    def api(base,path,**kwargs):
        calls.append(path)
        if '/user/list' in path: return {'data':{'list':[]}}
        if 'create' in path:
            assert kwargs['body']['tabs'] == ['https://www.temu.com/']
            if created[0]==3: raise RuntimeError(CAPACITY)
            created[0]+=1; return {'data':{'profile_id':f'owned-{created[0]}'}}
        return {'data':{'webdriver':str(driver),'marionette_port':24001}}
    monkeypatch.setattr(module,'api_call',api)
    with pytest.raises(RuntimeError,match='ADSPOWER_CAPACITY_BLOCKED'): module.main()
    assert not module.SESSIONS.exists()
    assert len(json.loads(policy.read_text('utf-8'))['worker_chain_ports'])==4
    assert len(json.loads((module.ROOT/'runtime/bootstrap.json').read_text('utf-8'))['workers'])==3
    assert not any('delete' in call for call in calls)


@pytest.mark.parametrize("error", ["AdsPower Local API 无法连接", "HTTP 429 rate limit", "permission denied"])
def test_network_and_other_api_failures_do_not_publish_partial_capacity(setup_fixture, monkeypatch, error):
    module, calls, _, _, policy = setup_fixture
    seed_bound(module, 3)
    def api(base, path, **kwargs):
        calls.append(path)
        raise RuntimeError(error)
    monkeypatch.setattr(module, "api_call", api)
    with pytest.raises(RuntimeError, match=error):
        module.main()
    assert not module.SESSIONS.exists()
    assert len(json.loads(policy.read_text("utf-8"))["worker_chain_ports"]) == 4
    assert "capacity_limit" not in json.loads((module.ROOT / "runtime/bootstrap.json").read_text("utf-8"))


def test_pipeline_distributes_keywords_over_actual_three_workers(tmp_path, monkeypatch):
    module = load_script("pipeline")
    monkeypatch.setattr(module, "ROOT", tmp_path)
    category = {"category_key": "fixture"}
    sessions = tmp_path / "sessions.json"
    sessions.write_text(json.dumps({"workers": [{"name": f"worker-{n:02d}-new", "webdriver_url": f"http://localhost:{23000+n}",
        "session_id_file": "sid", "bidi_url_file": "bidi"} for n in range(1, 4)]}), encoding="utf-8")
    policy = tmp_path / "runtime/proxy_policy.json"
    policy.parent.mkdir()
    policy.write_text(json.dumps({"worker_chain_ports": [22001, 22002, 22003],
        "session_rotation_policy": "NEW_ADSPOWER_PROFILE_AND_NEW_PROXY_SESSION", "delete_old_profile_first_via_adspower": True}), encoding="utf-8")
    plan = module.keyword_plan_path(category)
    plan.parent.mkdir(parents=True)
    plan.write_text(json.dumps({"keywords": [{"query": f"query {n}", "max_products": 2} for n in range(5)]}), encoding="utf-8")
    class Connection:
        def __enter__(self): return self
        def __exit__(self, *_): pass
    monkeypatch.setattr(module.socket, "create_connection", lambda *_a, **_k: Connection())
    calls = []
    def collect(command, **kwargs):
        calls.append(command[command.index("--query") + 1])
        output = Path(command[command.index("--output") + 1])
        (output / "progress.json").write_text('{"status":"KEYWORD_LIMIT_REACHED"}', encoding="utf-8")
        return 0
    monkeypatch.setattr(module, "run_collector", collect)
    module.collect_temu(tmp_path / "category.json", category, sessions)
    assert len(calls) == 10 and len(set(calls)) == 5
    assert all(calls.count(query) == 2 for query in set(calls))
    supervisors = list((module.run_root(category) / "temu/supervisors").glob("worker-*.json"))
    assert len(supervisors) == 3
    assert all(json.loads(path.read_text("utf-8"))["status"] == "WORKER_COMPLETE" for path in supervisors)


def test_capacity_reclaims_one_and_retries_to_four(setup_fixture, monkeypatch):
    from selection_automation import profile_capacity
    module,calls,commands,driver,policy=setup_fixture
    seed_bound(module,3)
    creates=[]; reclaimed=[]
    def api(base,path,**kwargs):
        calls.append(path)
        if 'create' in path:
            creates.append(path)
            if len(creates)==1: raise RuntimeError(CAPACITY)
            return {'data':{'profile_id':'owned-4'}}
        return {'data':{'webdriver':str(driver),'marionette_port':24001}}
    def reclaim(api,base,machine,protected,*args,**kwargs):
        assert protected=={'owned-1','owned-2','owned-3'}
        reclaimed.append('old'); return 'old'
    monkeypatch.setattr(module,'api_call',api)
    monkeypatch.setattr(profile_capacity,'reclaim_one',reclaim)
    module.main()
    assert len(creates)==2 and reclaimed==['old']
    manifest=json.loads(module.SESSIONS.read_text('utf-8'))
    assert manifest['worker_count']==4 and manifest['capacity_limited'] is False


@pytest.mark.parametrize('failure_count',[1,2])
def test_bootstrap_binding_failures_never_restart_opened_profile(setup_fixture,monkeypatch,failure_count):
    module,calls,commands,driver,policy=setup_fixture
    created=[];failures=[failure_count]
    def api(base,path,**kwargs):
        calls.append(path)
        if 'create' in path:
            created.append(f'new-{len(created)+1}');return {'data':{'profile_id':created[-1]}}
        return {'data':{'webdriver':str(driver),'marionette_port':24001}}
    def launch(command,**kwargs):
        commands.append(command)
        if any('bind_flower' in str(x) for x in command) and failures[0]:
            failures[0]-=1;return SimpleNamespace(returncode=1,stdout='',stderr='Maximum number of active sessions')
        return SimpleNamespace(returncode=0,stdout='',stderr='')
    monkeypatch.setattr(module,'api_call',api);monkeypatch.setattr(module,'run_background_launcher',launch)
    for _ in range(failure_count):
        with pytest.raises(RuntimeError,match='绑定失败'):module.main()
        journal=json.loads((module.ROOT/'runtime/bootstrap.json').read_text('utf-8'))
        assert journal['workers'][0]['bootstrap_status']=='BIND_FAILED'
        assert 'Maximum number' in journal['workers'][0]['bind_error']
    module.main()
    assert len(created)==4
    assert not any('stop?' in p or 'delete' in p for p in calls)
    assert len(json.loads(module.SESSIONS.read_text('utf-8'))['workers'])==4



@pytest.mark.parametrize('count', [1, 8, 32])
def test_requested_worker_count_creates_exact_isolated_profiles(setup_fixture, monkeypatch, count):
    module,calls,commands,driver,policy=setup_fixture
    policy.write_text(json.dumps({'worker_count':count,'worker_chain_ports':list(range(22001,22001+count)),
                                  'webdriver_ports':list(range(23001,23001+count))}),encoding='utf-8')
    created=[]
    def api(base,path,**kwargs):
        if '/user/list' in path:return {'data':{'list':[]}}
        if 'create' in path:
            created.append(kwargs['body'])
            return {'data':{'profile_id':f'owned-{len(created)}'}}
        return {'data':{'webdriver':str(driver),'marionette_port':24001}}
    monkeypatch.setattr(module,'api_call',api)
    module.main()
    manifest=json.loads(module.SESSIONS.read_text('utf-8'))
    assert manifest['worker_count']==manifest['desired_worker_count']==count
    assert len(created)==len(manifest['workers'])==count
    assert len({p['user_proxy_config']['proxy_port'] for p in created})==count
    assert manifest['workers'][-1]['name']==f'worker-{count:02d}-new'


def test_explicit_retry_can_rebind_open_profile_without_restart(setup_fixture, monkeypatch):
    module,calls,commands,driver,policy=setup_fixture
    created=[];bind_count=[]
    def api(base,path,**kwargs):
        calls.append(path)
        if 'create' in path:
            created.append(f'owned-{len(created)+1}')
            return {'data':{'profile_id':created[-1]}}
        return {'data':{'webdriver':str(driver),'marionette_port':24001}}
    def launch(command,**kwargs):
        commands.append(command)
        if any('bind_flower' in str(x) for x in command):
            bind_count.append(1)
            if len(bind_count)<=3:
                return SimpleNamespace(returncode=1,stdout='',stderr='Maximum number of active sessions')
        return SimpleNamespace(returncode=0,stdout='',stderr='')
    monkeypatch.setattr(module,'api_call',api)
    monkeypatch.setattr(module,'run_background_launcher',launch)
    for _ in range(3):
        with pytest.raises(RuntimeError,match='绑定失败'):module.main()
    module.main()
    assert len(created)==4
    assert sum('stop?user_id=owned-1' in p for p in calls)==0
    assert not any('delete' in p for p in calls)
    assert len(json.loads(module.SESSIONS.read_text('utf-8'))['workers'])==4
