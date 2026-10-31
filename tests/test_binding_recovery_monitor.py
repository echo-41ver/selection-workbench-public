import io
import json
import sys
import urllib.error
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
import resume_worker_binding as binding


def test_current_binding_can_recover_after_bootstrap_replaced_old_journal_profile(tmp_path, monkeypatch):
    from contextlib import nullcontext
    import selection
    monkeypatch.setattr(binding.r, 'ROOT', tmp_path)
    monkeypatch.setattr(selection, 'identity', lambda: 'machine')
    manifest = tmp_path / 'sessions.json'
    row = {'name': 'worker-01-new', 'profile_id': 'bootstrap-profile',
           'webdriver_url': 'http://127.0.0.1:4451'}
    manifest.write_text(json.dumps({'machine_id': 'machine', 'task_key': 'task', 'workers': [row]}))
    monkeypatch.setattr(binding.r, 'SESSIONS', manifest)
    path = tmp_path / 'runtime/proxyshare-chains/replacement-worker-1.json'
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({'phase': 'REPLACED', 'new_profile_id': 'previous-profile'}))
    driver = tmp_path / 'driver.exe'
    driver.touch()
    monkeypatch.setattr(binding, 'replacement_guard', lambda *a: nullcontext())
    monkeypatch.setattr(binding.r, 'start_profile_with_retry', lambda base, profile, **kw:
                        {'webdriver': str(driver)} if profile == 'bootstrap-profile' else None)
    monkeypatch.setattr(binding.r, 'api_base', lambda: 'http://127.0.0.1:50325')
    monkeypatch.setattr(binding.r, 'powershell', lambda: 'powershell')
    commands = []
    monkeypatch.setattr(binding.r, 'run_background_launcher', lambda command, **kw:
                        commands.append(command) or SimpleNamespace(returncode=0))
    monkeypatch.setattr(binding, 'mark_rebound_worker', lambda current: None)
    binding.resume(1, allow_rebind=True, expected_task='task', expected_profile='bootstrap-profile')
    journal = json.loads(path.read_text())
    assert journal['new_profile_id'] == 'bootstrap-profile' and journal['local_binding_recovered']
    assert len(commands) == 1 and '-RecoverOwnedDriver' in commands[0]


def test_rotation_after_local_binding_failure_starts_fresh_environment(tmp_path, monkeypatch):
    import pytest
    import selection
    r = binding.r
    monkeypatch.setattr(r, 'ROOT', tmp_path)
    monkeypatch.setattr(selection, 'identity', lambda: 'machine')
    policy = tmp_path / 'policy.json'
    policy.write_text(json.dumps({'session_rotation_policy': 'NEW_ADSPOWER_PROFILE_AND_NEW_PROXY_SESSION',
                                  'webdriver_ports': [4451]}))
    monkeypatch.setattr(r, 'POLICY', policy)
    manifest = tmp_path / 'sessions.json'
    manifest.write_text(json.dumps({'machine_id': 'machine', 'workers': [
        {'name': 'worker-01-new', 'profile_id': 'current-profile'}]}))
    monkeypatch.setattr(r, 'SESSIONS', manifest)
    journal = tmp_path / 'runtime/proxyshare-chains/replacement-worker-1.json'
    journal.parent.mkdir(parents=True)
    journal.write_text(json.dumps({'phase': 'BIND_FAILED', 'new_profile_id': 'current-profile'}))
    (tmp_path / 'runtime/last_run.json').write_text('{"category_key":"task"}')
    monkeypatch.setattr(r, 'stop_owned_collectors', lambda *a: None)
    monkeypatch.setattr(r, 'powershell', lambda: 'powershell')
    monkeypatch.setattr(r, 'api_base', lambda: 'http://127.0.0.1:50325')
    calls = []
    monkeypatch.setattr(r, 'api_call', lambda base, endpoint, **kw: calls.append(endpoint))
    def reached_fresh_deletion(base, profile):
        calls.append(('delete', profile))
        raise RuntimeError('fresh environment deletion reached')
    monkeypatch.setattr(r, 'delete_profile_after_release', reached_fresh_deletion)
    with pytest.raises(RuntimeError, match='fresh environment deletion reached'):
        r.replace_worker(SimpleNamespace(worker=1, task_key='task'))
    assert calls == ['/api/v1/browser/stop?user_id=current-profile', ('delete', 'current-profile')]


def test_binding_handoff_rechecks_target_after_owner_finishes_replacement(tmp_path, monkeypatch):
    from contextlib import contextmanager
    import pytest
    import selection
    from selection_automation.process_lock import ProcessLock
    monkeypatch.setattr(binding.r, 'ROOT', tmp_path)
    monkeypatch.setattr(selection, 'identity', lambda: 'machine')
    manifest = tmp_path / 'sessions.json'
    row = {'name': 'worker-01-new', 'profile_id': 'old-profile'}
    document = {'machine_id': 'machine', 'task_key': 'task', 'workers': [row]}
    manifest.write_text(json.dumps(document), encoding='utf-8')
    monkeypatch.setattr(binding.r, 'SESSIONS', manifest)
    journal = tmp_path / 'runtime/proxyshare-chains/replacement-worker-1.json'
    journal.parent.mkdir(parents=True)
    completed = {'phase': 'REPLACED', 'new_profile_id': 'new-profile'}

    @contextmanager
    def owner_finishes_before_yielding(*args):
        # The current owner still needs this real OS lock to finish handing off.
        with ProcessLock(tmp_path / 'runtime/replacement-1.lock'):
            document['workers'][0]['profile_id'] = 'new-profile'
            manifest.write_text(json.dumps(document), encoding='utf-8')
            journal.write_text(json.dumps(completed), encoding='utf-8')
        yield

    monkeypatch.setattr(binding, 'replacement_guard', owner_finishes_before_yielding)
    monkeypatch.setattr(binding.r, 'start_profile_with_retry', lambda *a, **k: pytest.fail('stale profile must not be opened'))
    with pytest.raises(RuntimeError, match='target changed during handoff'):
        binding.resume(1, allow_rebind=True, expected_task='task', expected_profile='old-profile')
    assert json.loads(manifest.read_text('utf-8'))['workers'][0]['profile_id'] == 'new-profile'
    assert json.loads(journal.read_text('utf-8')) == completed


def test_replacement_entry_allows_pending_owner_to_finish_before_transaction_lock(tmp_path, monkeypatch):
    from contextlib import contextmanager
    from selection_automation.process_lock import ProcessLock
    monkeypatch.setattr(binding.r, 'ROOT', tmp_path)
    monkeypatch.setattr(sys, 'argv', ['replace_proxyshare_worker_environment.py', '--worker', '1'])
    finished = []

    @contextmanager
    def owner_finishes_before_yielding(*args):
        with ProcessLock(tmp_path / 'runtime/replacement-1.lock'):
            finished.append('owner transaction completed')
        yield

    monkeypatch.setattr(binding.r, 'replacement_guard', owner_finishes_before_yielding)
    monkeypatch.setattr(binding.r, 'replace_worker', lambda args: {'worker': args.worker})
    binding.r.main()
    assert finished == ['owner transaction completed']


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


def test_exhausted_control_recovery_waits_for_explicit_lane_restart(tmp_path,monkeypatch):
    folder=tmp_path/'temu/supervisors';folder.mkdir(parents=True)
    path=folder/'worker-03-new.json'
    path.write_text(json.dumps({'status':'BINDING_RETRY_WAIT',
        'query_status':'STOPPED_BROWSER_CONTROL_RECOVERY_BUDGET',
        'recovery_budget_exhausted':True,'current_query':'silicone spatula'}))
    before=path.read_bytes();attempts={}
    def must_not_repair(*args,**kwargs):
        raise AssertionError('automatic recovery must stop when its budget is exhausted')
    monkeypatch.setattr(binding,'driver_connected',must_not_repair)
    monkeypatch.setattr(binding,'resume',must_not_repair)
    assert not binding.repair_waiting(tmp_path,{'name':'worker-03-new'},attempts,100)
    assert path.read_bytes()==before and not attempts


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
