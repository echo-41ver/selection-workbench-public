"""Manual-gate diagnostics use fake replies only, never a live browser or provider."""
import json
from pathlib import Path
import sys
from types import SimpleNamespace
import urllib.error
import urllib.request

import pytest

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'scripts'))
import selection

SECRET='fixture-private-query-cookie-session-token'


@pytest.fixture
def probe(tmp_path,monkeypatch):
    monkeypatch.setattr(selection,'ROOT',tmp_path)
    (tmp_path/'sid').write_text(SECRET,encoding='utf-8')
    replies=[]; requests=[]
    class Reply:
        def __init__(self,body): self.body=body
        def __enter__(self): return self
        def __exit__(self,*a): pass
        def read(self): return self.body
    class Opener:
        def open(self,request,**kwargs):
            requests.append((request,kwargs))
            result=replies.pop(0)
            if isinstance(result,Exception): raise result
            return Reply(result)
    monkeypatch.setattr(urllib.request,'build_opener',lambda *a:Opener())
    session={'name':'worker1','session_id_file':'sid','webdriver_url':'http://127.0.0.1:1/'+SECRET}
    return SimpleNamespace(root=tmp_path,session=session,replies=replies,requests=requests)


@pytest.mark.parametrize('ready',[False,True])
def test_normal_page_result_is_not_a_failed_check_and_keeps_boolean_gate(probe,ready):
    probe.replies.append(json.dumps({'value':ready}).encode())
    diagnostic={}
    assert selection.ready_after_verification(probe.session,SECRET,allow_empty_search=True,diagnostic=diagnostic) is ready
    assert diagnostic['ready'] is ready and diagnostic['check_ok'] is True
    assert diagnostic['code']==('PAGE_READY' if ready else 'PAGE_NOT_READY')
    assert diagnostic['checked_at']>0 and diagnostic['elapsed_seconds']>=0
    assert 'error_type' not in diagnostic and SECRET not in json.dumps(diagnostic)
    assert len(probe.requests)==1 and probe.requests[0][1]=={'timeout':5}


@pytest.mark.parametrize('reply,code,error_type',[
    (urllib.error.HTTPError('http://127.0.0.1/'+SECRET,403,SECRET,None,None),'DRIVER_HTTP_ERROR','HTTPError'),
    (urllib.error.URLError(SECRET),'DRIVER_CONNECTION_ERROR','URLError'),
    (urllib.error.URLError(TimeoutError(SECRET)),'DRIVER_TIMEOUT','URLError'),
    (TimeoutError(SECRET),'DRIVER_TIMEOUT','TimeoutError'),
    (ConnectionRefusedError(SECRET),'DRIVER_CONNECTION_ERROR','ConnectionRefusedError'),
    ((SECRET+' invalid json').encode(),'DRIVER_RESPONSE_JSON_ERROR','JSONDecodeError'),
    (b'[]','DRIVER_RESPONSE_FORMAT_ERROR','AttributeError'),
    (RuntimeError(SECRET),'DRIVER_CHECK_ERROR','RuntimeError'),
    (type(SECRET,(Exception,),{})(SECRET),'DRIVER_CHECK_ERROR','Exception'),
])
def test_failed_driver_checks_are_classified_without_exception_content(probe,reply,code,error_type):
    probe.replies.append(reply); diagnostic={}
    assert selection.ready_after_verification(probe.session,SECRET,diagnostic=diagnostic) is False
    assert diagnostic['ready'] is False and diagnostic['check_ok'] is False
    assert diagnostic['code']==code and diagnostic['error_type']==error_type
    assert SECRET not in json.dumps(diagnostic)
    assert set(diagnostic)<={'checked_at','elapsed_seconds','ready','check_ok','code','error_type','http_status'}
    if code=='DRIVER_HTTP_ERROR': assert diagnostic['http_status']==403


@pytest.mark.parametrize('value,code',[
    ({'error':'invalid session id','message':SECRET,'stacktrace':SECRET},'DRIVER_PROTOCOL_ERROR'),
    (None,'DRIVER_RESPONSE_FORMAT_ERROR'),
    (1,'DRIVER_RESPONSE_FORMAT_ERROR'),
])
def test_http_success_with_driver_error_is_not_reported_as_a_normal_blocked_page(probe,value,code):
    probe.replies.append(json.dumps({'value':value}).encode()); diagnostic={}
    assert selection.ready_after_verification(probe.session,diagnostic=diagnostic) is False
    assert diagnostic['code']==code and diagnostic['check_ok'] is False
    assert SECRET not in json.dumps(diagnostic)


def test_session_file_error_does_not_request_a_driver_or_record_its_path(probe):
    probe.session['session_id_file']='missing-'+SECRET; diagnostic={}
    assert selection.ready_after_verification(probe.session,diagnostic=diagnostic) is False
    assert diagnostic['code']=='SESSION_FILE_ERROR' and diagnostic['error_type']=='FileNotFoundError'
    assert probe.requests==[] and SECRET not in json.dumps(diagnostic)


def test_failed_check_is_visible_then_clears_to_normal_wait_and_resumes_same_session(probe):
    output=probe.root/'temu/workers/worker1/query'
    output.mkdir(parents=True)
    selection.save(output/'progress.json',{'status':'WAITING_SEARCH_VERIFICATION','scroll_collection_started':False})
    checkpoint=output/'products.jsonl'; checkpoint.write_text('{"goods_id":"retained"}\n',encoding='utf-8')
    saved=checkpoint.read_bytes(); sid=(probe.root/'sid').read_bytes()
    path=probe.root/'temu/supervisors/worker1.json'
    selection.save(path,{'status':'HUMAN_ACTION_REQUIRED','query_status':'WAITING_SEARCH_VERIFICATION',
                         'output':str(output),'current_query':SECRET,'completed_queries':['retained'],
                         'manual_action_required':True,'browser_preserved':True})
    attempts={'worker1':2}; next_at={'worker1':999}; state={}; children={}; finished={'worker1'}
    args=dict(children=children,finished=finished,attempts=attempts,next_at=next_at,state=state)
    probe.replies.extend([TimeoutError(SECRET),b'{"value":false}',b'{"value":true}'])
    assert selection.hold_blocked_worker(probe.root,probe.session,**args) is True
    worker,view,_=selection.worker_snapshot(probe.root,'worker1')
    assert worker['verification_probe']['code']=='DRIVER_TIMEOUT'
    assert '未确认验证状态' in view['状态'] and '控制检查超时' in view['建议动作']
    assert state['worker1']['恢复检查']==worker['verification_probe']
    assert attempts=={'worker1':2} and next_at=={'worker1':999} and children=={} and not finished
    assert selection.hold_blocked_worker(probe.root,probe.session,**args) is True
    worker,view,_=selection.worker_snapshot(probe.root,'worker1')
    assert worker['verification_probe']['code']=='PAGE_NOT_READY' and worker['verification_probe']['check_ok']
    assert view['状态']=='等待人工验证，保留浏览器'
    assert '控制检查超时' not in state['worker1']['建议动作']
    assert selection.hold_blocked_worker(probe.root,probe.session,**args) is False
    worker=selection.read(path)
    assert worker['status']=='RESUMING' and worker['manual_action_required'] is False
    assert worker['verification_probe']['code']=='PAGE_READY'
    assert worker['completed_queries']==['retained'] and worker['current_query']==SECRET
    assert attempts==next_at=={'worker1':0} and children=={}
    assert checkpoint.read_bytes()==saved and (probe.root/'sid').read_bytes()==sid
    assert len(probe.requests)==3
    assert len({request.full_url for request,_ in probe.requests})==1
    assert SECRET not in json.dumps(worker['verification_probe'])
