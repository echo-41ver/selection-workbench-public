import json,sys,time
from pathlib import Path
from types import SimpleNamespace
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
import watch_temu_union as w
import selection as s
import pytest

def test_long_merge_has_live_heartbeat_but_hard_deadline(tmp_path,monkeypatch):
    base=tmp_path/'temu';p=base/'workers/w/q/products.jsonl';p.parent.mkdir(parents=True)
    p.write_text('{"goods_id":"1"}\n')
    monkeypatch.setattr(w,'HEARTBEAT_INTERVAL',.01)
    actual=w.merge
    observed={}
    def slow(paths):
        deadline=time.monotonic()+2
        initial=None
        while True:
            try:
                current=json.loads((base/'union/watch_state.json').read_text())
            except (OSError,ValueError):
                current={}
            health=w.watch_health(tmp_path)
            if isinstance(current,dict) and current.get('status')=='MERGING' and 'heartbeat_at' in current:
                if initial is None:initial=current
                if current['heartbeat_at']>initial['heartbeat_at'] and health.get('merge_in_progress') is True:
                    break
            assert time.monotonic()<deadline, f'No advanced live heartbeat and valid merging health: {health!r}'
            time.sleep(.005)
        assert current['heartbeat_at']>initial['heartbeat_at']
        assert health['merge_in_progress']
        observed.update(current)
        return actual(paths)
    monkeypatch.setattr(w,'merge',slow)
    result=w.publish_once(base)
    assert result['status']=='PUBLISHED'
    assert not w.watch_health(tmp_path)['merge_in_progress']
    assert not w.watch_health(tmp_path)['stale']
    # Exercise the deadline using an actual snapshot without a live writer
    # overwriting the fixture's clock fields; keep its heartbeat fresh.
    deadline_base=tmp_path/'hard-deadline'
    deadline_state=deadline_base/'temu/union/watch_state.json'
    deadline_state.parent.mkdir(parents=True)
    for seconds,expected_stale in ((600,False),(601,True)):
        expired={**observed,'merge_started_at':observed['heartbeat_at']-seconds}
        deadline_state.write_text(json.dumps(expired))
        health=w.watch_health(deadline_base,now=observed['heartbeat_at'])
        assert health['merge_in_progress'] and health['heartbeat_age_seconds']==0
        assert health['merge_age_seconds']==seconds and health['stale'] is expected_stale


def test_success_resets_exit_budget_only_for_current_launch(tmp_path):
    (tmp_path/'logs').mkdir();child=SimpleNamespace(pid=123)
    kw=dict(children={'temu_union':child},finished=set(),attempts={'temu_union':2},
            state={'temu_union':{'launched_wall_at':1000}},terminate=lambda c:None,now=1005)
    s.union_supervision(tmp_path,{'last_success_at':999},**kw)
    assert kw['attempts']['temu_union']==2
    s.union_supervision(tmp_path,{'last_success_at':1001},**kw)
    assert kw['attempts']['temu_union']==0

def test_stale_kill_records_source_and_start_grace(tmp_path):
    (tmp_path/'logs').mkdir();calls=[];child=SimpleNamespace(pid=123)
    kw=dict(children={'temu_union':child},finished=set(),attempts={},
            state={'temu_union':{'launched_wall_at':1000}},terminate=calls.append)
    s.union_supervision(tmp_path,{'stale':True},now=1050,**kw)
    assert not calls
    s.union_supervision(tmp_path,{'stale':True,'heartbeat_age_seconds':95},now=1100,**kw)
    assert calls==[child]
    event=json.loads((tmp_path/'logs/union_lifecycle.jsonl').read_text())
    assert event['source']=='union_health_deadline' and event['pid']==123

def test_exhausted_budget_is_not_advertised_as_automatic_recovery(tmp_path):
    result=s.union_supervision(tmp_path,{'stale':True},children={},finished={'temu_union'},
        attempts={'temu_union':3},state={},terminate=lambda c:None)
    assert result['recovery_exhausted'] and not result['自动恢复']
    assert '单独恢复' in result['状态']

@pytest.mark.parametrize('success',[None,'not-a-time',float('nan'),float('inf'),True,999,2000])
def test_invalid_old_or_future_success_does_not_reset_budget(tmp_path,success):
    kw=dict(children={'temu_union':SimpleNamespace(pid=123)},finished=set(),attempts={'temu_union':2},
        state={'temu_union':{'launched_wall_at':1000}},terminate=lambda c:None,now=1005)
    s.union_supervision(tmp_path,{'last_success_at':success},**kw)
    assert kw['attempts']['temu_union']==2

def test_missing_health_times_out_only_owned_child_after_grace(tmp_path):
    (tmp_path/'logs').mkdir();calls=[];child=SimpleNamespace(pid=123)
    kw=dict(children={'temu_union':child},finished=set(),attempts={},
        state={'temu_union':{'launched_wall_at':1000}},terminate=calls.append)
    s.union_supervision(tmp_path,{},now=1090,**kw);assert not calls
    result=s.union_supervision(tmp_path,{},now=1091,**kw)
    assert calls==[child] and result['missing_health']
    calls.clear()
    s.union_supervision(tmp_path,{},children={},finished=set(),attempts={},state={},terminate=calls.append,now=2000)
    assert not calls

def test_foreign_owner_success_does_not_reset_or_kill_foreign_process(tmp_path):
    (tmp_path/'logs').mkdir();calls=[];child=SimpleNamespace(pid=123)
    kw=dict(children={'temu_union':child},finished=set(),attempts={'temu_union':2},
        state={'temu_union':{'launched_wall_at':1000}},terminate=calls.append,owner_matches=lambda child,pid:False)
    s.union_supervision(tmp_path,{'owner_pid':999,'last_success_at':1001},now=1005,**kw)
    assert not calls and kw['attempts']['temu_union']==2
    s.union_supervision(tmp_path,{'owner_pid':999,'last_success_at':1100},now=1100,**kw)
    assert calls==[child]

@pytest.mark.parametrize('payload',[[],None,{'heartbeat_at':None},{'heartbeat_at':'bad'},{'heartbeat_at':float('inf')},
    {'heartbeat_at':1000,'status':'MERGING','merge_started_at':None}])
def test_bad_health_file_is_missing_health_without_exception(tmp_path,payload):
    p=tmp_path/'temu/union/watch_state.json';p.parent.mkdir(parents=True);p.write_text(json.dumps(payload))
    assert w.watch_health(tmp_path,now=1005)=={}


def union_service_fixture(tmp_path,health,*,child=None,live=True):
    log=tmp_path/'logs';log.mkdir(exist_ok=True)
    launches=[];terminations=[]
    args=dict(children={'temu_union':child} if child is not None else {},
        finished={'temu_union'} if child is None else set(),attempts={'temu_union':3},next_at={},
        state={'temu_union':{'recovery_exhausted':True,'退出码':1,'阻断':True}},
        launch=lambda *args:launches.append(args),log=log,terminate=terminations.append,
        now=1010,existing_owner=lambda *args:live)
    return args,launches,terminations


def current_union_health(**changes):
    return {'owner_pid':123,'owner_created':1000,'last_success_at':1005,
        'stale':False,'状态':'汇总已更新','阻断':False,**changes}


def test_supervisor_reuses_verified_independent_current_owner_not_failed_duplicates(tmp_path):
    health=current_union_health();args,launches,kills=union_service_fixture(tmp_path,health)
    result=s.union_service_step(tmp_path,['watcher'],health,**args)
    assert not launches and not kills and result['external_service'] and result['PID']==123
    assert args['attempts']['temu_union']==0 and 'temu_union' not in args['finished']
    assert 'recovery_exhausted' not in args['state']['temu_union'] and '退出码' not in result
    assert result['状态']=='汇总已更新' and result['阻断'] is False


@pytest.mark.parametrize('success,stale',[(999,False),(None,False),(True,False),(float('nan'),False),(1005,True),(2000,False)])
def test_existing_owner_does_not_reset_failure_budget_without_current_fresh_success(tmp_path,success,stale):
    health=current_union_health(last_success_at=success,stale=stale)
    args,launches,kills=union_service_fixture(tmp_path,health)
    result=s.union_service_step(tmp_path,['watcher'],health,**args)
    assert not launches and not kills and args['attempts']['temu_union']==3
    assert 'temu_union' in args['finished']
    if stale:assert result['阻断'] is True


def test_unknown_owner_permissions_do_not_spawn_or_reset_exhausted_budget(tmp_path):
    health=current_union_health();args,launches,kills=union_service_fixture(tmp_path,health,live=None)
    result=s.union_service_step(tmp_path,['watcher'],health,**args)
    assert result['阻断'] and result['owner_verification_pending']
    assert not launches and not kills and args['attempts']['temu_union']==3


def test_current_running_child_keeps_existing_supervision_instead_of_adopting(tmp_path):
    child=SimpleNamespace(pid=321,poll=lambda:None)
    health=current_union_health();args,launches,kills=union_service_fixture(tmp_path,health,child=child)
    args['state']={'temu_union':{'launched_wall_at':1008}}
    args['existing_owner']=lambda *a:pytest.fail('Current live child retains ownership supervision')
    s.union_service_step(tmp_path,['watcher'],{},**args)
    assert args['children']['temu_union'] is child and not launches and not kills


def test_dead_duplicate_exit_is_preserved_before_adopting_actual_published_owner(tmp_path):
    child=SimpleNamespace(pid=321,poll=lambda:1,returncode=1)
    health=current_union_health();args,launches,kills=union_service_fixture(tmp_path,health,child=child)
    result=s.union_service_step(tmp_path,['watcher'],health,**args)
    event=json.loads((tmp_path/'logs/union_lifecycle.jsonl').read_text())
    assert event['event']=='PROCESS_EXIT_OBSERVED' and event['pid']==321 and event['returncode']==1
    assert not launches and not kills and result['PID']==123 and args['attempts']['temu_union']==0


@pytest.mark.parametrize('failure',['wrong_task','wrong_root','reused_pid','denied','changed_record'])
def test_union_adoption_checks_exact_task_root_ctime_and_owner_record(tmp_path,monkeypatch,failure):
    import local_service_recovery as recovery
    import restart_web,psutil
    root=tmp_path/'root';base=root/'runs/task';path=base/'temu/union/watch_state.json';path.parent.mkdir(parents=True)
    health=current_union_health();path.write_text(json.dumps(health))
    monkeypatch.setattr(s,'ROOT',root)
    script=root/'scripts/watch_temu_union.py';category=base/'category.json'
    if failure=='wrong_task':category=root/'runs/another/category.json'
    if failure=='wrong_root':script=tmp_path/'other/scripts/watch_temu_union.py'
    class Process:
        def create_time(self):return 1001 if failure=='reused_pid' else 1000
        def cmdline(self):
            if failure=='denied':raise psutil.AccessDenied(123)
            return ['python',str(script),'--category',str(category)]
    monkeypatch.setattr(psutil,'Process',lambda pid:Process())
    monkeypatch.setattr(restart_web,'native_process_active',lambda pid:True)
    if failure=='changed_record':path.write_text(json.dumps({**health,'owner_created':1001}))
    result=s.union_existing_owner(base,health)
    assert result is (None if failure in ('denied','changed_record') else False)


def test_union_adoption_accepts_exact_native_owner_not_first_matching_process(tmp_path,monkeypatch):
    import restart_web,psutil
    root=tmp_path/'root';base=root/'runs/task';path=base/'temu/union/watch_state.json';path.parent.mkdir(parents=True)
    health=current_union_health();path.write_text(json.dumps(health));seen=[]
    monkeypatch.setattr(s,'ROOT',root)
    class Process:
        def create_time(self):return 1000
        def cmdline(self):return [str(root/'.venv/Scripts/python.exe'),str(root/'scripts/watch_temu_union.py'),'--category',str(base/'category.json')]
    monkeypatch.setattr(psutil,'Process',lambda pid:(seen.append(pid) or Process()))
    monkeypatch.setattr(restart_web,'native_process_active',lambda pid:True)
    assert s.union_existing_owner(base,health) is True and seen==[123]


def test_owner_identity_exception_preserves_failure_budget_and_blocks_duplicate_launch(tmp_path):
    health=current_union_health();args,launches,kills=union_service_fixture(tmp_path,health)
    def unavailable(*args):raise PermissionError('cannot inspect identity')
    args['existing_owner']=unavailable
    result=s.union_service_step(tmp_path,['watcher'],health,**args)
    assert result['阻断'] and result['owner_verification_error_type']=='PermissionError'
    assert args['attempts']['temu_union']==3 and 'temu_union' in args['finished']
    assert args['state']['temu_union']['recovery_exhausted'] and not launches and not kills


def test_adopted_independent_watcher_observes_the_same_normal_stop_marker(tmp_path,monkeypatch):
    root=tmp_path/'root';base=root/'runs/task';base.mkdir(parents=True)
    health=current_union_health();args,launches,kills=union_service_fixture(base,health)
    assert s.union_service_step(base,['watcher'],health,**args)['external_service']
    stop=base/'STOP';stop.touch()
    monkeypatch.setattr(w,'ROOT',root)
    monkeypatch.setattr(w,'load_category',lambda path:{'category_key':'task'})
    w.watch(base/'category.json')
    assert stop.exists() and not (base/'temu/union/watcher.lock').exists()
    started=time.monotonic();assert w.wait_for_task_interval(stop,30) is False
    assert time.monotonic()-started<.5 and not launches and not kills


@pytest.mark.parametrize('success',[None,999])
def test_duplicate_exit_zero_does_not_erase_budget_without_new_owner_publication(tmp_path,success):
    child=SimpleNamespace(pid=321,poll=lambda:0,returncode=0)
    health=current_union_health(last_success_at=success)
    args,launches,kills=union_service_fixture(tmp_path,health,child=child)
    s.union_service_step(tmp_path,['watcher'],health,**args)
    event=json.loads((tmp_path/'logs/union_lifecycle.jsonl').read_text())
    assert event['returncode']==0 and event['pid']==321
    assert args['attempts']['temu_union']==3 and 'temu_union' in args['finished']
    assert not launches and not kills
