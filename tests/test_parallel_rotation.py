"""Exercise actual scheduling with independent real child processes, no AdsPower writes."""
import json
import subprocess
import sys
import time
from pathlib import Path
import pytest

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'scripts'))
import selection
from selection_automation import rotation_queue as q

OWNER={'pid':123,'created':1,'category_key':'fixture'}


def test_binding_child_publishes_failure_for_an_already_running_supervisor(tmp_path):
    from replace_proxyshare_worker_environment import record_binding_failure
    path=tmp_path/'runs/fixture/temu/supervisors/worker-03-new.json'
    selection.save(path,{'status':'RUNNING','current_query':'pump','unique_goods':30})
    record_binding_failure(tmp_path,'fixture','worker-03-new')
    state=selection.read(path)
    assert state['status']=='BINDING_FAILED' and state['manual_action_required']
    assert state['current_query']=='pump' and state['unique_goods']==30
    assert not (path.parent/'worker-02-new.json').exists()


def test_failed_new_profile_binding_enters_local_recovery_instead_of_stale_running(tmp_path,monkeypatch):
    from types import SimpleNamespace
    monkeypatch.setattr(selection,'ROOT',tmp_path)
    base=tmp_path/'runs/fixture'
    sessions=[{'name':f'worker-{i:02d}-new'} for i in range(1,4)]
    selection.save(tmp_path/'config/sessions.json',{'workers':sessions})
    selection.save(tmp_path/'runtime/proxyshare-chains/replacement-worker-3.json',{'phase':'BIND_FAILED'})
    selection.save(base/'temu/supervisors/worker-03-new.json',{'status':'RUNNING','current_query':'pump'})
    request=q.enqueue(base,[3],OWNER)
    request=q.claim(base,OWNER)
    result=base/'commands/rotation-results'/f"{request['id']}.json"
    selection.save(result,{'workers':[3],'results':[{'worker':3,'status':'FAILED'}]})
    jobs={request['id']:{'request':request,'child':'rotation','result':result}}
    children={'rotation':SimpleNamespace(poll=lambda:0,returncode=0)}
    attempts={}
    selection.rotation_step(base,base/'category.json',sessions,OWNER,jobs=jobs,children=children,
        state={},finished=set(),attempts=attempts,next_at={},manual_leases={},
        launch=lambda *a:pytest.fail('unexpected new replacement'),py=lambda *a:a)
    worker=selection.read(base/'temu/supervisors/worker-03-new.json')
    assert worker['status']=='BINDING_FAILED' and worker['manual_action_required']
    assert worker['current_query']=='pump'
    assert not jobs and not children


def test_four_and_twelve_replace_concurrently_and_finish_independently(tmp_path,monkeypatch):
    monkeypatch.setattr(selection,'ROOT',tmp_path)
    base=tmp_path/'runs/fixture'
    sessions=[{'name':f'worker-{i:02d}-new'} for i in range(1,13)]
    selection.save(tmp_path/'config/sessions.json',{'workers':sessions})
    jobs={};children={};state={};finished=set();attempts={};next_at={}
    helper=tmp_path/'replacement_fixture.py'
    helper.write_text('''import json,sys,time
from pathlib import Path
number=int(sys.argv[1]);output=Path(sys.argv[2]);control=Path(sys.argv[3])
(control/f'{number}.started').write_text('started')
deadline=time.monotonic()+15
while not (control/f'{number}.release').exists():
 if time.monotonic()>deadline:raise SystemExit(3)
 time.sleep(.02)
output.parent.mkdir(parents=True,exist_ok=True)
output.write_text(json.dumps({'workers':[number],'results':[{'worker':number,'status':'REPLACED' if number==12 else 'FAILED'}]}))
''')
    commands=[]
    def launch(name,command):
        commands.append(command)
        number=command[command.index('--workers')+1]
        result=command[command.index('--result')+1]
        children[name]=subprocess.Popen([sys.executable,str(helper),number,str(result),str(tmp_path)],
            creationflags=subprocess.CREATE_NO_WINDOW if sys.platform=='win32' else 0)
    def step():
        return selection.rotation_step(base,base/'category.json',sessions,OWNER,jobs=jobs,children=children,
            state=state,finished=finished,attempts=attempts,next_at=next_at,manual_leases={},launch=launch,
            py=lambda *args:list(map(str,args)))
    def wait_for(predicate):
        deadline=time.monotonic()+6
        while not predicate():
            assert time.monotonic()<deadline,'fixture did not reach expected state'
            time.sleep(.02)
    try:
        r4=q.enqueue(base,[4],OWNER);step()
        wait_for(lambda:(tmp_path/'4.started').exists())
        r12=q.enqueue(base,[12],OWNER);_,rotating=step()
        wait_for(lambda:(tmp_path/'12.started').exists())
        assert rotating=={4,12}
        assert all(child.poll() is None for child in children.values())
        assert q.worker_requests(base)['worker-04-new']['status']=='RUNNING'
        assert q.worker_requests(base)['worker-12-new']['status']=='RUNNING'
        with pytest.raises(ValueError,match='其他路可提交排队'):q.enqueue(base,[4],OWNER)
        (tmp_path/'4.release').touch()
        wait_for(lambda:children[jobs[r4['id']]['child']].poll() is not None)
        _,rotating=step()
        wait_for(lambda:(tmp_path/'12.started').exists())
        assert rotating=={12}
        assert q.worker_requests(base)['worker-04-new']['status']=='FAILED'
        assert q.worker_requests(base)['worker-12-new']['status']=='RUNNING'
        assert commands[0][-1]!=commands[1][-1] # no shared batch_latest file
        (tmp_path/'12.release').touch()
        wait_for(lambda:children[jobs[r12['id']]['child']].poll() is not None)
        _,rotating=step()
        assert rotating==set()
        assert q.worker_requests(base)['worker-04-new']['status']=='FAILED'
        assert q.worker_requests(base)['worker-12-new']['status']=='COMPLETE'
        assert selection.read(base/'temu/supervisors/worker-12-new.json')['status']=='SESSION_REPLACED'
        assert attempts['worker-04-new']==3 and attempts['worker-12-new']==0
    finally:
        for child in children.values():
            if child.poll() is None:child.terminate()
            child.wait(timeout=5)


def test_same_worker_concurrent_submissions_are_deduplicated(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    def submit(_):
        try:return q.enqueue(tmp_path,[4],OWNER)['id']
        except ValueError:return None
    with ThreadPoolExecutor(max_workers=8) as pool:values=list(pool.map(submit,range(8)))
    assert sum(v is not None for v in values)==1
    assert len(q.read_requests(tmp_path))==1


def test_batch_writes_only_its_requested_result_file(tmp_path,monkeypatch):
    from types import SimpleNamespace
    import replace_proxyshare_workers_batch as batch
    monkeypatch.setattr(batch,'ROOT',tmp_path)
    monkeypatch.setattr(batch,'run_background_launcher',lambda command,**kw:
        SimpleNamespace(returncode=0,stdout=json.dumps({'worker':int(command[command.index('--worker')+1]),'status':'REPLACED'})))
    result=tmp_path/'requests/isolated-result.json'
    monkeypatch.setattr(sys,'argv',['replace','--workers','4,12','--result',str(result)])
    batch.main()
    assert json.loads(result.read_text())['workers']==[4,12]
    assert not (tmp_path/'runtime/proxyshare-chains/batch_latest.json').exists()
