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
    def slow(paths):
        deadline=time.monotonic()+2
        while not (base/'union/watch_state.json').exists():
            assert time.monotonic()<deadline;time.sleep(.005)
        initial=json.loads((base/'union/watch_state.json').read_text())
        time.sleep(.05)
        current=json.loads((base/'union/watch_state.json').read_text())
        assert current['heartbeat_at']>initial['heartbeat_at']
        assert w.watch_health(tmp_path)['merge_in_progress']
        current['merge_started_at']=current['heartbeat_at']-601
        (base/'union/watch_state.json').write_text(json.dumps(current))
        assert w.watch_health(tmp_path,now=current['heartbeat_at'])['stale']
        return actual(paths)
    monkeypatch.setattr(w,'merge',slow)
    result=w.publish_once(base)
    assert result['status']=='PUBLISHED'
    assert not w.watch_health(tmp_path)['merge_in_progress']
    assert not w.watch_health(tmp_path)['stale']

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
