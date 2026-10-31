import io
import json
import sys
import urllib.error
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
import resume_worker_binding as binding


def test_driver_repair_does_not_erase_exhausted_site_recovery(tmp_path, monkeypatch):
    monkeypatch.setattr(binding.r, 'ROOT', tmp_path)
    manifest=tmp_path/'sessions.json';manifest.write_text('{"task_key":"task"}')
    monkeypatch.setattr(binding.r, 'SESSIONS', manifest)
    folder=tmp_path/'runs/task/temu/supervisors';folder.mkdir(parents=True)
    path=folder/'worker-01-new.json'
    path.write_text(json.dumps({'status':'HUMAN_ACTION_REQUIRED',
        'query_status':'STOPPED_LOGIN_REQUIRED','recovery_budget_exhausted':True,
        'manual_action_required':True,'unique_goods':0}))
    before=path.read_bytes()
    binding.mark_rebound_worker({'name':'worker-01-new'})
    assert path.read_bytes()==before


def test_invalid_session_requires_explicit_driver_error(tmp_path,monkeypatch):
    monkeypatch.setattr(binding.r,'ROOT',tmp_path)
    (tmp_path/'sid').write_text('test-session')
    row={'session_id_file':'sid','webdriver_url':'http://127.0.0.1:9999'}
    response=['invalid session id']
    def request(*args,**kwargs):
        raise urllib.error.HTTPError('local',404,'missing',{},io.BytesIO(json.dumps({'value':{'error':response[0]}}).encode()))
    monkeypatch.setattr(binding.urllib.request,'build_opener',lambda *args:SimpleNamespace(open=request))
    assert binding.invalid_session(row)
    response[0]='unknown command'
    assert not binding.invalid_session(row)


def test_only_stopped_invalid_session_is_repaired_and_rate_limited(tmp_path,monkeypatch):
    folder=tmp_path/'temu/supervisors';folder.mkdir(parents=True)
    path=folder/'worker-02-new.json';path.write_text(json.dumps({'status':'HUMAN_ACTION_REQUIRED'}))
    invalid=[False];calls=[];attempts={};row={'name':'worker-02-new'}
    monkeypatch.setattr(binding,'invalid_session',lambda row:invalid[0])
    monkeypatch.setattr(binding,'profile_closed',lambda row:False)
    monkeypatch.setattr(binding,'resume',lambda worker,**kwargs:calls.append((worker,kwargs)))
    assert not binding.repair_waiting(tmp_path,row,attempts,100)
    assert not calls
    invalid[0]=True
    assert binding.repair_waiting(tmp_path,row,attempts,100)
    assert calls==[(2,{'allow_rebind':True,'expected_task':tmp_path.name,'expected_profile':None})]
    assert not binding.repair_waiting(tmp_path,row,attempts,110)
    path.write_text(json.dumps({'status':'RUNNING'}))
    assert binding.repair_waiting(tmp_path,row,attempts,200) == 'BOUND_EXISTING_PROFILE'


def test_closed_profile_queues_only_its_lane_instead_of_reopening(tmp_path,monkeypatch):
    import selection
    folder=tmp_path/'temu/supervisors';folder.mkdir(parents=True)
    (folder/'worker-02-new.json').write_text(json.dumps({'status':'HUMAN_ACTION_REQUIRED'}))
    monkeypatch.setattr(binding,'invalid_session',lambda row:True)
    monkeypatch.setattr(binding,'profile_closed',lambda row:True)
    calls=[]
    monkeypatch.setattr(selection,'request_rotation',lambda base,workers:calls.append(workers))
    monkeypatch.setattr(binding,'resume',lambda *args,**kwargs:(_ for _ in ()).throw(AssertionError('closed profile reopened')))
    assert binding.repair_waiting(tmp_path,{'name':'worker-02-new'},{},100)=='REPLACEMENT_QUEUED'
    assert calls==[[2]]


def test_hung_control_driver_is_rebound_even_without_invalid_session_error(tmp_path,monkeypatch):
    folder=tmp_path/'temu/supervisors';folder.mkdir(parents=True)
    (folder/'worker-04-new.json').write_text(json.dumps({'status':'HUMAN_ACTION_REQUIRED',
        'query_status':'STOPPED_BROWSER_CONTROL_RECOVERY_TIMEOUT'}))
    monkeypatch.setattr(binding,'invalid_session',lambda row:False)
    monkeypatch.setattr(binding,'driver_connected',lambda row:False)
    monkeypatch.setattr(binding,'profile_closed',lambda row:False)
    calls=[];attempts={};row={'name':'worker-04-new'}
    monkeypatch.setattr(binding,'resume',lambda *args,**kwargs:calls.append((args,kwargs)))
    assert binding.repair_waiting(tmp_path,row,attempts,100)=='BOUND_EXISTING_PROFILE'
    assert len(calls)==1
    assert not binding.repair_waiting(tmp_path,row,attempts,110)


def test_responsive_driver_clears_terminal_progress_and_preserves_raw_count(tmp_path,monkeypatch):
    monkeypatch.setattr(binding.r,'ROOT',tmp_path)
    manifest=tmp_path/'sessions.json';manifest.write_text('{"task_key":"task"}')
    monkeypatch.setattr(binding.r,'SESSIONS',manifest)
    base=tmp_path/'runs/task';folder=base/'temu/supervisors';folder.mkdir(parents=True)
    output=base/'temu/workers/worker-04-new/portable-washer';output.mkdir(parents=True)
    path=folder/'worker-04-new.json'
    path.write_text(json.dumps({'status':'HUMAN_ACTION_REQUIRED','query_status':'STOPPED_BROWSER_CONTROL_RECOVERY_TIMEOUT',
        'current_query':'portable washer','output':str(output),'current_sort':'sales'}))
    (output/'progress.json').write_text(json.dumps({'status':'STOPPED_BROWSER_CONTROL_RECOVERY_TIMEOUT',
        'raw_collected_count':40,'unique_goods':8,'raw_sampling':{'count':40}}))
    checkpoint=output/'products.jsonl';checkpoint.write_text('saved');before=checkpoint.read_bytes()
    monkeypatch.setattr(binding,'driver_connected',lambda row:True)
    monkeypatch.setattr(binding,'resume',lambda *a,**k:(_ for _ in ()).throw(AssertionError('responsive driver must not restart')))
    assert binding.repair_waiting(base,{'name':'worker-04-new','profile_id':'same-profile'},{},100)=='BOUND_EXISTING_PROFILE'
    progress=json.loads((output/'progress.json').read_text())
    assert progress['status']=='RESUMING' and progress['raw_collected_count']==40 and progress['unique_goods']==8
    assert progress['raw_sampling']=={'count':40} and checkpoint.read_bytes()==before
    assert json.loads(path.read_text())['current_sort']=='sales'


def test_monitor_hands_recovered_lane_to_supervisor_without_launching_collector(tmp_path,monkeypatch):
    import selection
    from threading import Event
    monkeypatch.setattr(binding.r,'ROOT',tmp_path)
    monkeypatch.setattr(selection,'live_owner',lambda:{'category_key':'task'})
    monkeypatch.setattr(selection,'identity',lambda:'local')
    manifest=tmp_path/'sessions.json'
    row={'name':'worker-04-new','profile_id':'existing'}
    manifest.write_text(json.dumps({'machine_id':'local','task_key':'task','workers':[row]}))
    monkeypatch.setattr(binding.r,'SESSIONS',manifest)
    base=tmp_path/'runs/task';folder=base/'temu/supervisors';folder.mkdir(parents=True)
    path=folder/'worker-04-new.json'
    path.write_text(json.dumps({'status':'HUMAN_ACTION_REQUIRED','query_status':'STOPPED_BROWSER_CONTROL_RECOVERY_TIMEOUT'}))
    calls=[];repaired=Event();repair=binding.repair_waiting
    monkeypatch.setattr(binding.subprocess,'Popen',lambda command,**kwargs:calls.append(command) or SimpleNamespace(pid=123))
    monkeypatch.setattr(binding,'driver_connected',lambda row:True)
    def observe_repair(*args):
        result=repair(*args);repaired.set();return result
    def stop_after_repair(seconds):
        assert repaired.wait(2)
        (base/'STOP').touch()
    monkeypatch.setattr(binding,'repair_waiting',observe_repair)
    monkeypatch.setattr(binding,'time',SimpleNamespace(monotonic=lambda:100,sleep=stop_after_repair))
    binding.monitor(base/'category.json')
    assert repaired.is_set() and not calls
    assert json.loads(path.read_text())['status']=='RESUMING'
    before=path.read_bytes()
    binding.monitor(base/'category.json')
    assert path.read_bytes()==before and not calls
