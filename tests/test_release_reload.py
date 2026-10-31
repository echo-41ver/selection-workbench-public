import copy
import http.client
import json
import sys
import threading
from pathlib import Path
from types import SimpleNamespace

from selection_automation.release_reload import ReleaseReload,idle_for_reload
from selection_automation.runtime_audit import disk_version,process_version


def idle_console():
    return SimpleNamespace(busy=False,market_busy=False,stage3_busy=False,owner=lambda:None,
        _stage3_refreshing=set(),_frontend_refreshing=set(),_codex_review_refreshing=set())


def idle_state():
    return dict(ok=True,active=False,console_busy=False,market_busy=False,generation={},
                stage3_control={'running':False,'codex_review':{'running':False}})


def test_changed_published_files_detected_without_relabelling_loaded_process(tmp_path):
    (tmp_path/'scripts').mkdir();source=tmp_path/'scripts/app.py';source.write_text('old=1')
    loaded=process_version(str(tmp_path));now=[0]
    release=ReleaseReload(tmp_path,loaded,clock=lambda:now[0])
    assert not release.check(force=True)['pending']
    source.write_text('new=2')
    assert release.check(force=True)['pending']
    assert process_version(str(tmp_path))==loaded
    assert disk_version(tmp_path)['source_sha256']!=loaded['source_sha256']
    assert not release.stable()
    now[0]=6
    assert release.stable()


def test_idle_check_blocks_each_task_type_and_unknown_status():
    console=idle_console();state=idle_state()
    assert idle_for_reload(state,console)
    for field in ('active','console_busy','market_busy'):
        busy=copy.deepcopy(state);busy[field]=True
        assert not idle_for_reload(busy,console)
    for patch in ({'generation':{'stage':'generating'}},
                  {'stage3_control':{'running':True}},
                  {'stage3_control':{'running':False,'codex_review':{'running':True}}},
                  {'ok':False}):
        assert not idle_for_reload({**state,**patch},console)
    for attribute in ('busy','market_busy','stage3_busy','_stage3_refreshing',
                      '_frontend_refreshing','_codex_review_refreshing'):
        busy=idle_console();setattr(busy,attribute,True)
        assert not idle_for_reload(state,busy)
    assert not idle_for_reload({},console)


def test_old_service_rejects_new_tasks_but_still_accepts_stop(tmp_path,monkeypatch):
    sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
    import web_console as web
    from selection_automation import agent_models,local_agents
    monkeypatch.setattr(agent_models,'catalogs',lambda *a,**kw:[{'id':'doubao','status':'ready','models':[{'id':'actual-native-model'}]}])
    monkeypatch.setattr(local_agents,'discover',lambda *a,**kw:[])
    server=web.LocalConsoleServer(('127.0.0.1',0),web.Handler)
    called=[]
    server.console=SimpleNamespace(root=tmp_path,start=lambda body:called.append('start'),
        action=lambda action,body:called.append(action) or {'ok':True},
        set_stage3_concurrency=lambda body:called.append('concurrency') or {'ok':True},
        refresh_market=lambda body:called.append('market') or {'ok':True})
    server.release=ReleaseReload(tmp_path,{'commit':'old','source_sha256':'old'},
        read_version=lambda root:{'commit':'new','source_sha256':'new'})
    thread=threading.Thread(target=server.serve_forever);thread.start()
    try:
        for endpoint,expected in (('/api/generate',409),('/api/resume',409),('/api/stop',200),('/api/change-session',200),('/api/refresh-market',202),
                ('/api/local-agents/models',200),('/api/local-agents/refresh',200),('/api/stage3/concurrency',200)):
            conn=http.client.HTTPConnection(*server.server_address,timeout=3)
            conn.request('POST',endpoint,'{}',{'Content-Type':'application/json',
                'Origin':f'http://127.0.0.1:{server.server_port}'})
            response=conn.getresponse();payload=json.load(response)
            assert response.status==expected
            if expected==409:assert payload['error']['code']=='UPDATE_PENDING'
            conn.close()
        assert called==['stop','rotate','market','concurrency']
    finally:server.shutdown();server.server_close();thread.join(3)


def test_watcher_waits_for_active_task_then_requests_one_graceful_reload(tmp_path):
    sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
    import web_console as web
    release=ReleaseReload(tmp_path,{'commit':'old','source_sha256':'old'},
        read_version=lambda root:{'commit':'new','source_sha256':'new'},clock=lambda:10)
    release.check(force=True);release.changed_at=0
    states=[{**idle_state(),'active':True},idle_state()]
    console=idle_console()
    console.status=lambda:(_ for _ in ()).throw(AssertionError('watcher must reuse background sample'))
    console.monitor=SimpleNamespace(get=lambda:{**states.pop(0),'status_sample':{'age_seconds':0}})
    stopped=[]
    server=SimpleNamespace(release=release,release_lock=threading.RLock(),console=console,
                           shutdown=lambda:stopped.append(True))
    waits=[]
    def wait(_):
        waits.append(True)
        return len(waits)>2
    web.watch_release(server,SimpleNamespace(wait=wait))
    assert len(waits)==2 and stopped==[True] and release.restarting


def test_watcher_never_reloads_from_missing_or_stale_background_status(tmp_path):
    sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
    import web_console as web
    release=ReleaseReload(tmp_path,{},read_version=lambda root:{'commit':'new'},clock=lambda:10)
    release.check(force=True);release.changed_at=0
    states=[{'ok':True,'status_loading':True},
            {**idle_state(),'status_sample':{'age_seconds':20}}]
    console=idle_console();console.monitor=SimpleNamespace(get=lambda:states.pop(0))
    stopped=[];waits=[]
    def wait(_):waits.append(True);return len(waits)>2
    web.watch_release(SimpleNamespace(release=release,release_lock=threading.RLock(),console=console,
        shutdown=lambda:stopped.append(True)),SimpleNamespace(wait=wait))
    assert not stopped and release.waiting
