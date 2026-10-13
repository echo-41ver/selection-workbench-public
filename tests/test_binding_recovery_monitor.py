import io
import json
import sys
import urllib.error
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
import resume_worker_binding as binding


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
    assert calls==[(2,{'allow_rebind':True})]
    assert not binding.repair_waiting(tmp_path,row,attempts,110)
    path.write_text(json.dumps({'status':'RUNNING'}))
    assert not binding.repair_waiting(tmp_path,row,attempts,200)


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
