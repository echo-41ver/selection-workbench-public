"""A Windows scheduler launch must not occupy the HTTP request or Stop lock."""
import http.client
import json
from pathlib import Path
import sys
import threading
import time
from types import SimpleNamespace

import pytest

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
import web_console as web


def fixture_console(tmp_path,monkeypatch):
    console=web.Console(tmp_path);console.key='original_task';console.draft_used=True
    base=tmp_path/'runs/original_task';base.mkdir(parents=True)
    web.selection.save(base/'category.json',{'category_key':'original_task','category_name_zh':'浴帘','execution_mode':'collection'})
    web.selection.save(base/'status.json',{'状态':'已停止'})
    monkeypatch.setattr(console,'owner',lambda:None)
    return console,base


def finished(console):
    deadline=time.monotonic()+3
    while console.busy and time.monotonic()<deadline:time.sleep(.01)
    assert not console.busy and not console.resume_launch_active


def test_http_resume_returns_before_launch_and_status_duplicate_stop_remain_available(tmp_path,monkeypatch):
    console,base=fixture_console(tmp_path,monkeypatch)
    entered=threading.Event();release=threading.Event()
    def invoke(argv,**kwargs):
        assert kwargs=={'timeout':90,'cancellable':True}
        entered.set();assert release.wait(4)
        if console.cancel.is_set():raise web.ConsoleError('CANCELLED','已停止')
        return SimpleNamespace(returncode=0)
    monkeypatch.setattr(console,'invoke',invoke)
    monkeypatch.setattr(console,'monitor_status',lambda:{'ok':True,'console_busy':console.busy,'phase':console.phase})
    server=web.ThreadingHTTPServer(('127.0.0.1',0),web.Handler)
    server.console=console;server.release_lock=threading.RLock()
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    headers={'Content-Type':'application/json','Origin':f'http://127.0.0.1:{server.server_port}'}
    client=http.client.HTTPConnection('127.0.0.1',server.server_port,timeout=2)
    try:
        client.request('POST','/api/resume','{}',headers);response=client.getresponse();queued=json.loads(response.read())
        assert response.status==202 and queued['launch_pending'] and queued['launch_id']
        assert entered.wait(2) and console.busy and '尚未确认运行' in queued['phase']
        assert console.lock.acquire(timeout=.1);console.lock.release()
        client.request('GET','/api/status');response=client.getresponse();state=json.loads(response.read())
        assert response.status==200 and state['console_busy']
        client.request('POST','/api/resume','{}',headers);response=client.getresponse();duplicate=json.loads(response.read())
        assert response.status==409 and duplicate['error']['code']=='TASK_BUSY'
        client.request('POST','/api/stop','{}',headers);response=client.getresponse();response.read()
        assert response.status==200 and console.cancel.is_set()
        assert (base/'STOP').exists() and (base/'stage12/STOP').exists() and (base/'stage123/STOP').exists()
        release.set();finished(console)
        assert web.selection.read(base/'resume_launch.json')['state']=='cancelled'
    finally:
        release.set();client.close();server.shutdown();server.server_close();thread.join()


@pytest.mark.parametrize('failure',['timeout','nonzero','no_owner','unexpected'])
def test_failed_resume_persists_error_stops_cooperatively_and_preserves_billing(tmp_path,monkeypatch,failure):
    console,base=fixture_console(tmp_path,monkeypatch)
    paid=base/'stage123';paid.mkdir()
    for name,data in [('request_intents.jsonl','{"billing":"UNKNOWN"}\n'),('schema_retry_claim.json','{"consumed":true}')]:
        (paid/name).write_text(data)
    original={p.name:p.read_bytes() for p in paid.iterdir()}
    def invoke(argv,**kwargs):
        if failure=='timeout':raise web.ConsoleError('TIMEOUT','当前阶段超时，请检查网络或本地服务')
        if failure=='unexpected':raise OSError('token-that-must-never-be-recorded')
        return SimpleNamespace(returncode=7 if failure=='nonzero' else 0)
    monkeypatch.setattr(console,'invoke',invoke)
    monkeypatch.setattr(console,'registered_owner',lambda key:None)
    console.resume_current({});finished(console)
    record=web.selection.read(base/'resume_launch.json')
    assert record['state']=='failed' and record['error_code']
    assert 'owner' not in record and 'token-that-must-never-be-recorded' not in json.dumps(record)
    assert (base/'STOP').exists() and (base/'stage12/STOP').exists() and (paid/'STOP').exists()
    assert web.selection.read(base/'status.json')['状态'].startswith('继续启动失败')
    assert all((paid/name).read_bytes()==data for name,data in original.items())
    state=console.status()
    assert state['severity']=='red' and state['preparation_error']==record['message']
    assert state['phase']==record['message'] and state['can_resume']


def test_running_label_requires_registered_owner_and_keeps_original_task(tmp_path,monkeypatch):
    console,base=fixture_console(tmp_path,monkeypatch)
    data=base/'real_input.jsonl';data.write_bytes(b'real saved checkpoint\n')
    calls=[]
    monkeypatch.setattr(console,'invoke',lambda argv,**kw:calls.append((argv,kw)) or SimpleNamespace(returncode=0))
    owner={'pid':987,'created':12345,'category_key':'original_task'}
    monkeypatch.setattr(console,'registered_owner',lambda key:owner)
    console.cancel.set();queued=console.resume_current({});finished(console)
    record=web.selection.read(base/'resume_launch.json')
    assert not console.cancel.is_set() and queued['launch_pending']
    assert record['state']=='running' and record['owner']=={'pid':987,'created':12345}
    assert calls[0][0][1:]==['start','--preserve-prepared','--category',str(base/'category.json')]
    assert '--paid' not in calls[0][0] and not (base/'stage123/paid_enabled.json').exists()
    assert data.read_bytes()==b'real saved checkpoint\n'


def test_thread_start_failure_releases_busy_and_leaves_durable_failure(tmp_path,monkeypatch):
    console,base=fixture_console(tmp_path,monkeypatch)
    class BrokenThread:
        def __init__(self,**kwargs):pass
        def start(self):raise RuntimeError('cannot start thread')
    monkeypatch.setattr(web.threading,'Thread',BrokenThread)
    with pytest.raises(web.ConsoleError) as error:console.resume_current({})
    assert error.value.code=='RESUME_FAILED'
    assert not console.busy and not console.resume_launch_active
    assert web.selection.read(base/'resume_launch.json')['error_code']=='THREAD_START_FAILED'


def test_explicit_resume_cancellation_terminates_only_its_owned_child_without_env(tmp_path,monkeypatch):
    console,_=fixture_console(tmp_path,monkeypatch)
    child=SimpleNamespace(poll=lambda:None)
    def launch(*args,**kwargs):
        console.cancel.set()
        return child
    monkeypatch.setattr(web.subprocess,'Popen',launch)
    stopped=[];monkeypatch.setattr(web.selection,'terminate_owned',lambda p:stopped.append(p))
    with pytest.raises(web.ConsoleError) as error:console.invoke(['selection.py','start'],cancellable=True)
    assert error.value.code=='CANCELLED' and stopped==[child]


def test_already_cancelled_resume_never_spawns_a_child(tmp_path,monkeypatch):
    console,_=fixture_console(tmp_path,monkeypatch);console.cancel.set()
    monkeypatch.setattr(web.subprocess,'Popen',lambda *a,**k:pytest.fail('must not launch after Stop'))
    monkeypatch.setattr(web.selection,'terminate_owned',lambda *a:pytest.fail('there is no owned child to stop'))
    with pytest.raises(web.ConsoleError) as error:console.invoke(['selection.py','start'],cancellable=True)
    assert error.value.code=='CANCELLED'


def test_stop_write_error_cannot_leave_resume_busy_forever(tmp_path,monkeypatch):
    console,base=fixture_console(tmp_path,monkeypatch)
    console.busy=True;console.resume_launch_active=True;console.cancel.set()
    monkeypatch.setattr(web.selection,'request_task_stop',lambda base:(_ for _ in ()).throw(PermissionError('disk unavailable')))
    with pytest.raises(PermissionError):console._resume_current(base,{'id':'fixture','category_key':'original_task'})
    assert not console.busy and not console.resume_launch_active


def test_completed_collection_does_not_hide_a_pending_resume_launch(tmp_path,monkeypatch):
    console,base=fixture_console(tmp_path,monkeypatch)
    category=web.selection.read(base/'category.json')
    category.update(amazon={'target_count':1},temu={'max_products_per_keyword':1})
    web.selection.save(base/'category.json',category)
    web.selection.save(base/'amazon/amazon_frontend_latest.json',{'status':'COMPLETE','details_complete':True,'records':[{'asin':'A'}]})
    passes=[{'query':'shower curtain','sort':'sales'},{'query':'shower curtain','sort':'recent'}]
    web.selection.save(base/'temu/supervisors/assignments.json',{'workers':[{'name':'worker-01-new','passes':passes}]})
    web.selection.save(base/'temu/supervisors/worker-01-new.json',{'worker':'worker-01-new','status':'WORKER_COMPLETE','completed_passes':passes})
    for order in ('sales','recent'):
        web.selection.save(base/f'temu/workers/worker-01-new/shower-curtain-{order}/progress.json',
            {'search_query':'shower curtain','sales_sort':{'requested':order},'raw_collected_count':1,
             'unique_goods':1,'status':'KEYWORD_LIMIT_REACHED','updated_at':'2026-10-21T09:00:00+08:00'})
    entered=threading.Event();release=threading.Event()
    def invoke(argv,**kwargs):
        entered.set();release.wait(4)
        return SimpleNamespace(returncode=0)
    monkeypatch.setattr(console,'invoke',invoke)
    monkeypatch.setattr(console,'registered_owner',lambda key:None)
    try:
        console.resume_current({});assert entered.wait(2)
        state=console.status()
        assert state['collection_complete'] and state['console_busy']
        assert '尚未确认运行' in state['phase'] and state['phase']!='全部采集已完成'
        assert state['resume_launch']['state']=='launching'
    finally:release.set();finished(console)
