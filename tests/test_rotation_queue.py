import sys
from pathlib import Path
import pytest

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
import selection
import web_console as web
from selection_automation import rotation_queue as q

OWNER={'pid':123,'created':1,'category_key':'fixture'}

def test_failed_rotation_exposes_specific_worker_reason(tmp_path):
    import json
    request=q.enqueue(tmp_path,[1,3],OWNER)
    q.finish(tmp_path,request,{1})
    result=tmp_path/'commands/rotation-results'/f"{request['id']}.json"
    result.parent.mkdir(parents=True)
    result.write_text(json.dumps({'results':[{'worker':3,'status':'FAILED','reason':'Traceback\nRuntimeError: route busy'}]}),encoding='utf-8')
    controls=q.worker_requests(tmp_path)
    assert controls['worker-03-new']['message']=='RuntimeError: route busy'
    assert controls['worker-01-new']['message'] is None


def test_four_retries_start_distinct_lanes_without_losing_requests(tmp_path):
    queued=[q.enqueue(tmp_path,[worker],OWNER) for worker in range(1,5)]
    in_flight = set()
    for index,expected in enumerate(queued):
        running=q.claim(tmp_path,OWNER,in_flight=in_flight)
        assert running['id']==expected['id']
        in_flight.add(running['id'])
        assert all(r['status']=='QUEUED' for r in q.read_requests(tmp_path)[index+1:])
    assert q.claim(tmp_path,OWNER,in_flight=in_flight) is None
    for index,running in enumerate(queued):
        q.finish(tmp_path,running,set() if index==1 else set(running['workers']))
    assert q.claim(tmp_path,OWNER) is None
    assert [r['status'] for r in q.read_requests(tmp_path)]==['COMPLETE','FAILED','COMPLETE','COMPLETE']


def test_other_worker_requests_survive_busy_rotation(tmp_path):
    first=q.enqueue(tmp_path,[1],OWNER)
    running=q.claim(tmp_path,OWNER)
    second=q.enqueue(tmp_path,[2],OWNER)
    third=q.enqueue(tmp_path,[3],OWNER)
    q.finish(tmp_path,running,{1})
    assert q.claim(tmp_path,OWNER)['id']==second['id']
    q.finish(tmp_path,second,{2})
    assert q.claim(tmp_path,OWNER)['id']==third['id']
    q.finish(tmp_path,third,set())
    assert q.worker_requests(tmp_path)['worker-03-new']['status']=='FAILED'
    assert [r['id'] for r in q.read_requests(tmp_path)]==[first['id'],second['id'],third['id']]


def test_no_owner_rejects_without_writing_command(tmp_path,monkeypatch):
    monkeypatch.setattr(selection,'live_owner',lambda:None)
    console=web.Console(tmp_path);console.key='fixture'
    with pytest.raises(web.ConsoleError,match='监督器'):
        console.action('rotate',{'worker':1})
    assert q.read_requests(tmp_path/'runs/fixture')==[]


def test_old_owner_requests_not_executed_in_new_run(tmp_path):
    q.enqueue(tmp_path,[1],OWNER)
    assert q.claim(tmp_path,{**OWNER,'created':2}) is None
    assert q.read_requests(tmp_path)[0]['status']=='INTERRUPTED'


def test_concurrent_http_requests_are_not_overwritten(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(lambda n:q.enqueue(tmp_path,[n],OWNER),range(1,9)))
    assert len(q.read_requests(tmp_path))==8
    q.interrupt_pending(tmp_path)
    assert all(r['status']=='INTERRUPTED' for r in q.read_requests(tmp_path))
