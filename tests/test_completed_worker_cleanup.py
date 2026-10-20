import json
import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
import cleanup_completed_worker as cleanup
import replace_proxyshare_worker_environment as api
import pytest

@pytest.mark.parametrize('status', ['RUNNING','HUMAN_ACTION_REQUIRED','WORKER_COMPLETE'])
def test_only_completed_lane_is_deleted(tmp_path,monkeypatch,status):
    root=tmp_path; run=root/'run'; state=run/'temu/supervisors/w.json'
    state.parent.mkdir(parents=True); state.write_text(json.dumps({'status':status}))
    import selection
    monkeypatch.setattr(selection,'identity',lambda:'fixture-machine')
    (run/'temu/keyword_plan.json').write_text(json.dumps({'finalized':True}))
    (root/'config').mkdir(); (root/'config/sessions.json').write_text(json.dumps({'task_key':run.name,'machine_id':'fixture-machine','workers':[{'name':'w','profile_id':'target'}]}))
    (state.parent/'assignments.json').write_text(json.dumps({'jobs':[]}))
    calls=[]
    monkeypatch.setattr(api,'api_base',lambda:'http://fixture')
    monkeypatch.setattr(api,'api_call',lambda base,path,**kw:calls.append(path) or {'data':{'list':[{'user_id':'target'}]}})
    monkeypatch.setattr(api,'delete_profile_after_release',lambda base,p:calls.append(('delete',p)))
    assert cleanup.cleanup_completed_worker(root,run,'w') == (status=='WORKER_COMPLETE')
    if status=='WORKER_COMPLETE':
        assert calls[-1]==('delete','target')
        assert json.loads(state.read_text())['status']=='WORKER_COMPLETE'
        count=len(calls)
        assert cleanup.cleanup_completed_worker(root,run,'w')
        assert len(calls)==count
    else: assert not calls


def test_provisional_plan_preserves_profile(tmp_path):
    state=tmp_path/'temu/supervisors/w.json';state.parent.mkdir(parents=True)
    state.write_text('{"status":"WORKER_COMPLETE"}')
    assert not cleanup.cleanup_completed_worker(tmp_path,tmp_path,'w')


def test_failed_delete_stays_retryable_and_wrong_task_never_deletes(tmp_path,monkeypatch):
    import selection
    monkeypatch.setattr(selection,'identity',lambda:'machine')
    root=tmp_path;run=root/'task';state=run/'temu/supervisors/w.json'
    state.parent.mkdir(parents=True);state.write_text('{"status":"WORKER_COMPLETE"}')
    (run/'temu/keyword_plan.json').write_text('{"finalized":true}')
    (state.parent/'assignments.json').write_text(json.dumps({'jobs':[{'owner':'w','started':True,'complete':True}]}))
    config=root/'config/sessions.json';config.parent.mkdir()
    manifest={'machine_id':'machine','task_key':'task','workers':[{'name':'w','profile_id':'p'}]}
    config.write_text(json.dumps(manifest))
    calls=[]
    monkeypatch.setattr(api,'api_base',lambda:'fixture')
    monkeypatch.setattr(api,'api_call',lambda *a,**k:{'data':{'list':[{'user_id':'p'}]}})
    def fail(*args): calls.append(args);raise RuntimeError('occupied')
    monkeypatch.setattr(api,'delete_profile_after_release',fail)
    monkeypatch.setattr(cleanup.time,'sleep',lambda _:None)
    assert not cleanup.cleanup_completed_worker(root,run,'w')
    assert len(calls)==3 and json.loads(state.read_text())['environment_cleanup']=='RETRY_REQUIRED'
    manifest['task_key']='someone-else';config.write_text(json.dumps(manifest))
    with pytest.raises(RuntimeError,match='another task'):cleanup.cleanup_completed_worker(root,run,'w')
    assert len(calls)==3


def test_lane_with_remaining_dispatch_is_not_retired(tmp_path,monkeypatch):
    import selection
    monkeypatch.setattr(selection,'identity',lambda:'machine')
    run=tmp_path/'task';state=run/'temu/supervisors/w.json';state.parent.mkdir(parents=True)
    state.write_text('{"status":"WORKER_COMPLETE"}')
    (run/'temu/keyword_plan.json').write_text('{"finalized":true}')
    (state.parent/'assignments.json').write_text(json.dumps({'jobs':[{'owner':'w','started':True,'complete':False}]}))
    config=tmp_path/'config/sessions.json';config.parent.mkdir()
    config.write_text(json.dumps({'task_key':'task','machine_id':'machine','workers':[{'name':'w','profile_id':'p'}]}))
    assert not cleanup.cleanup_completed_worker(tmp_path,run,'w')


def test_other_lanes_blocked_sales_does_not_retain_finished_profile(tmp_path,monkeypatch):
    import selection
    monkeypatch.setattr(selection,'identity',lambda:'machine')
    run=tmp_path/'task';state=run/'temu/supervisors/w.json';state.parent.mkdir(parents=True)
    state.write_text('{"status":"WORKER_COMPLETE"}')
    (run/'temu/keyword_plan.json').write_text('{"finalized":true}')
    dispatch={'jobs':[{'query':'blocked','owner':'other','started':True,'complete':False,
        'sort_passes':{'sales':{'owner':'other','started':True,'complete':False},
                       'recent':{'owner':'w','started':False,'complete':False}}}]}
    (state.parent/'assignments.json').write_text(json.dumps(dispatch))
    config=tmp_path/'config/sessions.json';config.parent.mkdir()
    config.write_text(json.dumps({'task_key':'task','machine_id':'machine','workers':[{'name':'w','profile_id':'p'}]}))
    calls=[]
    monkeypatch.setattr(api,'api_base',lambda:'fixture')
    monkeypatch.setattr(api,'api_call',lambda *a,**k:{'data':{'list':[{'user_id':'p'}]}})
    monkeypatch.setattr(api,'delete_profile_after_release',lambda *a:calls.append(a[1]))
    assert cleanup.cleanup_completed_worker(tmp_path,run,'w')
    assert calls==['p']
    dispatch['jobs'][0]['sort_passes']['sales']['complete']=True
    (state.parent/'assignments.json').write_text(json.dumps(dispatch))
    assert not cleanup.cleanup_completed_worker(tmp_path,run,'w')


def test_completed_lane_stops_only_its_proxy_chain_before_profile_delete(tmp_path,monkeypatch):
    import selection
    import retain_proxy_connections
    monkeypatch.setattr(selection,'identity',lambda:'machine')
    monkeypatch.setattr(retain_proxy_connections,'owned_chains',lambda root:{1:(500,1.0)})
    monkeypatch.setattr(cleanup.shutil,'which',lambda name:'pwsh')
    events=[]
    monkeypatch.setattr(cleanup.subprocess,'run',lambda *a,**k:events.append(('chain',a[0][-1])))
    monkeypatch.setattr(api,'api_base',lambda:'fixture')
    monkeypatch.setattr(api,'api_call',lambda *a,**k:events.append(('api',a[1])) or {'data':{'list':[{'user_id':'p'}]}})
    monkeypatch.setattr(api,'delete_profile_after_release',lambda *a:events.append(('delete',a[1])))
    run=tmp_path/'task';state=run/'temu/supervisors/w.json';state.parent.mkdir(parents=True)
    state.write_text('{"status":"WORKER_COMPLETE"}')
    (run/'temu/keyword_plan.json').write_text('{"finalized":true}')
    (state.parent/'assignments.json').write_text('{"jobs":[]}')
    config=tmp_path/'config/sessions.json';config.parent.mkdir()
    config.write_text(json.dumps({'task_key':'task','machine_id':'machine','workers':[{'name':'w','profile_id':'p'}]}))
    assert cleanup.cleanup_completed_worker(tmp_path,run,'w')
    assert events[0]==('chain','1') and events[-1]==('delete','p')
