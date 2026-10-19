import json
from types import SimpleNamespace
import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/"scripts"))
import selection


def test_incomplete_binding_launches_local_repair_once(tmp_path,monkeypatch):
    monkeypatch.setattr(selection,'ROOT',tmp_path)
    monkeypatch.setattr(selection,'identity',lambda:'local')
    base=tmp_path/'runs'/'task';name='worker-06-new'
    selection.save(tmp_path/'config/sessions.json',{'machine_id':'local','workers':[{'name':name,'profile_id':'existing'}]})
    journal=tmp_path/'runtime/proxyshare-chains/replacement-worker-6.json'
    selection.save(journal,{'phase':'PROFILE_CREATED','new_profile_id':'existing'})
    path=base/'temu/supervisors'/f'{name}.json'
    selection.save(path,{'status':'NETWORK_RECOVERY_FAILED','current_query':'remote'})
    child=SimpleNamespace(poll=lambda:None,pid=123)
    calls=[];children={};state={};attempts={};next_at={};finished=set()
    def launch(name,cmd):calls.append(cmd);return child
    args=dict(children=children,state=state,attempts=attempts,next_at=next_at,finished=finished,bind_launch=launch)
    assert selection.hold_blocked_worker(base,{'name':name},**args)
    assert 'resume_worker_binding.py' in calls[0][1]
    assert selection.hold_blocked_worker(base,{'name':name},**args)
    assert len(calls)==1
    child.poll=lambda:0;child.returncode=0
    assert not selection.hold_blocked_worker(base,{'name':name},**args)
    assert selection.read(path)['status']=='RESUMING'
    assert name not in children


def test_local_bind_failures_are_bounded(tmp_path,monkeypatch):
    monkeypatch.setattr(selection,'ROOT',tmp_path);monkeypatch.setattr(selection,'identity',lambda:'local')
    name='worker-06-new';base=tmp_path/'run'
    selection.save(tmp_path/'config/sessions.json',{'machine_id':'local','workers':[{'name':name,'profile_id':'existing'}]})
    selection.save(tmp_path/'runtime/proxyshare-chains/replacement-worker-6.json',{'phase':'BIND_FAILED','new_profile_id':'existing','local_bind_attempts':2})
    selection.save(base/'temu/supervisors'/f'{name}.json',{'status':'BINDING_FAILED'})
    state={}
    assert selection.hold_blocked_worker(base,{'name':name},children={},state=state,attempts={},next_at={},finished=set(),bind_launch=lambda *a: (_ for _ in ()).throw(AssertionError('must not launch')))
    assert not state[name]['需人工介入']
    assert '自动重连' in state[name]['状态']


def test_rebound_lane_resets_exhausted_supervisor_attempts(tmp_path):
    name='worker-04-new';path=tmp_path/'temu/supervisors'/f'{name}.json'
    selection.save(path,{'status':'RESUMING'})
    finished={name};attempts={name:3};next_at={name:float('inf')};state={}
    assert not selection.hold_blocked_worker(tmp_path,{'name':name},children={},finished=finished,
        attempts=attempts,next_at=next_at,state=state)
    assert not finished and attempts[name]==0 and next_at[name]==0
