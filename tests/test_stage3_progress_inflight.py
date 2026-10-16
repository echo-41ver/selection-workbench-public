import json
import psutil
import pytest
from selection_automation import stage3_progress as progress
from selection_automation.stage3_billing import billing_hold


def write(path,rows):
    path.parent.mkdir(parents=True,exist_ok=True)
    path.write_text(''.join(json.dumps(r)+'\n' for r in rows),encoding='utf-8')


def fixture(base):
    folder=base/'stage123'
    write(folder/'request_intents.jsonl',[
        {'request_id':'old','status':'started','time':10,'pairs':1,'vision_revision_keys':['old']},
        {'request_id':'live','status':'started','time':101,'pairs':2,'vision_revision_keys':['new1','new2']}])
    write(folder/'results.jsonl',[
        {'asin':'A','goods_id':'held','vision_revision_key':'old','api_status':'failed','billing_state':'UNKNOWN'}])
    return folder


def test_live_inflight_preserves_unknown_isolation_and_paid_guard(tmp_path,monkeypatch):
    folder=fixture(tmp_path)
    monkeypatch.setattr(progress,'live_consumer_started',lambda base:100)
    s=progress.progress_billing_hold(tmp_path)
    assert s['pairs']=={('A','held')} and s['request_ids']=={'old'}
    assert (s['inflight_requests'],s['inflight_pairs'],s['fail_closed'])==(1,2,False)
    assert billing_hold(folder/'request_intents.jsonl',folder/'results.jsonl')['fail_closed']


def test_dead_consumer_or_old_orphan_still_fails_closed(tmp_path,monkeypatch):
    fixture(tmp_path)
    for started in (None,200):
        monkeypatch.setattr(progress,'live_consumer_started',lambda base:started)
        s=progress.progress_billing_hold(tmp_path)
        assert s['fail_closed'] and s['inflight_requests']==0


def test_malformed_live_keys_not_treated_as_inflight(tmp_path,monkeypatch):
    folder=fixture(tmp_path)
    write(folder/'request_intents.jsonl',[{'request_id':'live','status':'started','time':101,'pairs':1}])
    monkeypatch.setattr(progress,'live_consumer_started',lambda base:100)
    assert progress.progress_billing_hold(tmp_path)['fail_closed']


@pytest.mark.parametrize('phase,created,owned,expected',[
    ('分析中',10,True,100),('等待新增候选',10,True,None),
    ('分析中',11,True,None),('分析中',10,False,None)])
def test_live_identity_requires_phase_owner_creation_and_task_bound_runner(tmp_path,monkeypatch,phase,created,owned,expected):
    folder=tmp_path/'stage123'
    folder.mkdir()
    (folder/'controller.json').write_text(json.dumps({'pid':7,'created':10,'phase':phase}),encoding='utf-8')
    class Child:
        def cmdline(self):return ['python','scripts/run_category_layer3.py','--results',str(folder/'results.jsonl' if owned else tmp_path/'other/results.jsonl')]
        def is_running(self):return True
        def create_time(self):return 100
    class Owner:
        def is_running(self):return True
        def create_time(self):return created
        def children(self,recursive):assert recursive;return [Child()]
    monkeypatch.setattr(psutil,'Process',lambda pid:Owner())
    assert progress.live_consumer_started(tmp_path)==expected
