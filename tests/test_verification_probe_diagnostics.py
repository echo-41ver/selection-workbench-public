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
    if not ready: probe.replies.append(b'{"value":"https://www.temu.com/search_result.html"}')
    diagnostic={}
    assert selection.ready_after_verification(probe.session,SECRET,allow_empty_search=True,diagnostic=diagnostic) is ready
    assert diagnostic['ready'] is ready and diagnostic['check_ok'] is True
    assert diagnostic['code']==('PAGE_READY' if ready else 'PAGE_NOT_READY')
    assert diagnostic['checked_at']>0 and diagnostic['elapsed_seconds']>=0
    assert 'error_type' not in diagnostic and SECRET not in json.dumps(diagnostic)
    assert len(probe.requests)==(1 if ready else 2) and all(options=={'timeout':5} for _,options in probe.requests)


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
    probe.replies.extend([TimeoutError(SECRET),b'{"value":false}',b'{"value":"https://www.temu.com/search_result.html"}',b'{"value":true}'])
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
    assert len(probe.requests)==4
    assert len({request.full_url for request,_ in probe.requests})==2
    assert SECRET not in json.dumps(worker['verification_probe'])



@pytest.mark.parametrize('started',[False,True])
def test_recovered_bootstrap_network_page_can_resume_empty_search_only_before_collection(probe,started):
    output=probe.root/'temu/workers/worker1/query'
    output.mkdir(parents=True)
    selection.save(output/'progress.json',{'status':'STOPPED_PROXY_OR_NETWORK_FAILURE','scroll_collection_started':started})
    checkpoint=output/'products.jsonl'; checkpoint.write_text('{"goods_id":"retained"}\n',encoding='utf-8')
    path=probe.root/'temu/supervisors/worker1.json'
    selection.save(path,{'status':'HUMAN_ACTION_REQUIRED','query_status':'STOPPED_PROXY_OR_NETWORK_FAILURE',
                         'output':str(output),'current_query':SECRET,'completed_passes':['retained'],
                         'manual_action_required':True,'browser_preserved':True})
    probe.replies.append(b'{"value":true}')
    assert selection.hold_blocked_worker(probe.root,probe.session,children={},finished=set(),
        attempts={'worker1':2},next_at={'worker1':999},state={}) is False
    body=json.loads(probe.requests[0][0].data)
    assert body['args']==[SECRET,not started]
    worker=selection.read(path)
    assert worker['status']=='RESUMING' and worker['completed_passes']==['retained']
    assert checkpoint.read_text(encoding='utf-8')=='{"goods_id":"retained"}\n'


def values(probe, *rows):
    probe.replies.extend(json.dumps({'value':value}).encode() for value in rows)


def paths(probe):
    return [(request.get_method(),request.full_url.split('/session/'+SECRET,1)[1])
            for request,_ in probe.requests]


def test_stopped_manual_auxiliary_tab_selects_only_same_session_unique_empty_temu_and_keeps_strict_dom(probe):
    values(probe,False,'https://passport.geekbi.com/login', 'aux-'+SECRET,
           ['aux-'+SECRET,'temu-'+SECRET],None,'https://www.temu.com/search_result.html',True)
    diagnostic={}
    assert selection.ready_after_verification(probe.session,SECRET,allow_empty_search=True,diagnostic=diagnostic) is True
    assert diagnostic['code']=='PAGE_READY' and diagnostic['window_selection']=='SELECTED_SAME_SESSION'
    assert diagnostic['window_kind']=='EMPTY_SEARCH' and len(diagnostic['selected_handle_sha256'])==64
    assert SECRET not in json.dumps(diagnostic)
    assert paths(probe)==[('POST','/execute/sync'),('GET','/url'),('GET','/window'),('GET','/window/handles'),
                          ('POST','/window'),('GET','/url'),('POST','/execute/sync')]
    bodies=[json.loads(request.data) for request,_ in probe.requests if request.full_url.endswith('/execute/sync')]
    assert bodies[0]==bodies[1] and bodies[1]['args']==[SECRET,True]
    assert all(request.full_url.startswith(probe.session['webdriver_url']+'/session/'+SECRET+'/') for request,_ in probe.requests)
    assert all(0<options['timeout']<=5 for _,options in probe.requests)


def test_selected_temu_captcha_is_still_false_not_a_resume(probe):
    values(probe,False,'https://passport.geekbi.com/login','aux',['aux','temu'],None,
           'https://www.temu.com/search_result.html',False)
    diagnostic={}
    assert not selection.ready_after_verification(probe.session,SECRET,allow_empty_search=True,diagnostic=diagnostic)
    assert diagnostic['code']=='PAGE_NOT_READY' and diagnostic['check_ok'] is True
    assert diagnostic['window_selection']=='SELECTED_SAME_SESSION'


@pytest.mark.parametrize('url',[
    'https://www.temu.com/search_result.html?search_key='+SECRET,
    'https://temu.com/search_result.html?search_key='+SECRET,
])
def test_same_query_temu_candidate_requires_original_dom_gate(probe,url):
    values(probe,False,'https://passport.geekbi.com/login','aux',['aux','temu'],None,url,True)
    diagnostic={}
    assert selection.ready_after_verification(probe.session,SECRET,allow_empty_search=True,diagnostic=diagnostic)
    assert diagnostic['window_kind']=='CURRENT_QUERY' and SECRET not in json.dumps(diagnostic)


@pytest.mark.parametrize('url',[
    'https://passport.geekbi.com/login','about:blank','http://www.temu.com/search_result.html',
    'https://www.temu.com/login','https://www.temu.com/bgn_verification.html',
    'https://www.temu.com/','https://www.temu.com/search_result.html?search_key=OTHER',
    'https://www.temu.com.evil.example/search_result.html','https://evil.temu.com/search_result.html',
    'https://user:secret@www.temu.com/search_result.html','https://www.temu.com:444/search_result.html',
    'https://www.temu.com/search_result.html?search_key=&search_key=OTHER',
])
def test_non_candidate_documents_are_not_passed_and_original_context_is_restored(probe,url):
    values(probe,False,'https://passport.geekbi.com/login','aux',['aux','other'],None,url,None)
    diagnostic={}
    assert not selection.ready_after_verification(probe.session,SECRET,allow_empty_search=True,diagnostic=diagnostic)
    assert diagnostic['window_selection']=='NO_MATCHING_TEMU_WINDOW' and diagnostic['code']=='PAGE_NOT_READY'
    assert json.loads(probe.requests[-1][0].data)=={'handle':'aux'}
    assert sum(request.full_url.endswith('/execute/sync') for request,_ in probe.requests)==1


def test_multiple_temu_tabs_are_ambiguous_even_if_only_one_is_current_empty_search(probe):
    values(probe,False,'https://passport.geekbi.com/login','aux',['aux','temu1','temu2'],
           None,'https://www.temu.com/search_result.html',None,'https://www.temu.com/bgn_verification.html',None)
    diagnostic={}
    assert not selection.ready_after_verification(probe.session,SECRET,allow_empty_search=True,diagnostic=diagnostic)
    assert diagnostic['window_selection']=='AMBIGUOUS_TEMU_WINDOWS'
    assert json.loads(probe.requests[-1][0].data)=={'handle':'aux'}


@pytest.mark.parametrize('handles',[[str(i) for i in range(9)],['aux','aux'],[],{},['aux',None],['different']])
def test_window_set_is_strict_and_bounded_before_any_switch(probe,handles):
    values(probe,False,'https://passport.geekbi.com/login','aux',handles)
    diagnostic={}
    assert not selection.ready_after_verification(probe.session,SECRET,allow_empty_search=True,diagnostic=diagnostic)
    assert diagnostic['window_selection']=='WINDOW_SET_REJECTED'
    assert ('POST','/window') not in paths(probe)


def test_current_temu_blocked_page_does_not_switch_to_a_different_tab(probe):
    values(probe,False,'https://www.temu.com/bgn_verification.html')
    diagnostic={}
    assert not selection.ready_after_verification(probe.session,SECRET,allow_empty_search=True,diagnostic=diagnostic)
    assert diagnostic['window_selection']=='CURRENT_TEMU_NOT_READY'
    assert len(probe.requests)==2


def test_normal_non_manual_probe_keeps_single_old_request_without_tab_selection(probe):
    values(probe,False)
    diagnostic={}
    assert not selection.ready_after_verification(probe.session,SECRET,allow_empty_search=False,diagnostic=diagnostic)
    assert len(probe.requests)==1 and 'window_selection' not in diagnostic


def test_switch_timeout_does_not_issue_more_commands_or_report_success(probe):
    values(probe,False,'https://passport.geekbi.com/login','aux',['aux','temu'])
    probe.replies.append(TimeoutError(SECRET))
    diagnostic={}
    assert not selection.ready_after_verification(probe.session,SECRET,allow_empty_search=True,diagnostic=diagnostic)
    assert diagnostic['code']=='DRIVER_TIMEOUT' and diagnostic['window_selection']=='WINDOW_CHECK_FAILED'
    assert paths(probe)[-1]==('POST','/window') and len(probe.requests)==5
    assert SECRET not in json.dumps(diagnostic)


def test_switch_protocol_error_is_not_treated_as_a_successful_context_change(probe):
    values(probe,False,'https://passport.geekbi.com/login','aux',['aux','temu'],
           {'error':'no such window','message':SECRET,'stacktrace':SECRET})
    diagnostic={}
    assert not selection.ready_after_verification(probe.session,SECRET,allow_empty_search=True,diagnostic=diagnostic)
    assert diagnostic['code']=='DRIVER_PROTOCOL_ERROR' and not diagnostic['check_ok']
    assert len(probe.requests)==5 and SECRET not in json.dumps(diagnostic)


def test_total_probe_deadline_prevents_starting_another_request(probe,monkeypatch):
    ticks=iter([0,0,1,2,16,17])
    monkeypatch.setattr(selection.time,'monotonic',lambda:next(ticks))
    values(probe,False,'https://passport.geekbi.com/login','aux')
    diagnostic={}
    assert not selection.ready_after_verification(probe.session,SECRET,allow_empty_search=True,diagnostic=diagnostic)
    assert len(probe.requests)==3 and diagnostic['code']=='DRIVER_TIMEOUT'


def test_live_collector_guard_does_not_probe_or_switch_context(probe,monkeypatch):
    worker={'status':'HUMAN_ACTION_REQUIRED','current_query':SECRET,'output':str(probe.root/'out')}
    view={'采集状态':'WAITING_SEARCH_VERIFICATION'}
    monkeypatch.setattr(selection,'worker_snapshot',lambda *a:(worker.copy(),view.copy(),{'verify_to_resume':True}))
    monkeypatch.setattr(selection,'ready_after_verification',lambda *a,**kw:pytest.fail('live collector owns its context'))
    child=SimpleNamespace(poll=lambda:None,pid=123)
    assert selection.hold_blocked_worker(probe.root,probe.session,children={'worker1':child},finished=set(),
        attempts={'worker1':0},next_at={'worker1':0},state={},verify=selection.ready_after_verification) is True
    assert not probe.requests


def test_command_timeout_branch_preserves_existing_fence_and_skips_new_selector(probe,monkeypatch):
    worker={'status':'HUMAN_ACTION_REQUIRED','current_query':SECRET,'output':str(probe.root/'out')}
    view={'采集状态':'STOPPED_BROWSER_COMMAND_TIMEOUT'}
    monkeypatch.setattr(selection,'worker_snapshot',lambda *a:(worker.copy(),view.copy(),{'verify_to_resume':True}))
    monkeypatch.setattr(selection,'control_command_finished',lambda *a:False)
    assert selection.hold_blocked_worker(probe.root,probe.session,children={},finished=set(),
        attempts={'worker1':0},next_at={'worker1':0},state={}) is True
    assert not probe.requests



def test_http_current_temu_gate_remains_owned_and_does_not_switch_to_another_tab(probe):
    values(probe,False,'http://www.temu.com/bgn_verification.html')
    diagnostic={}
    assert not selection.ready_after_verification(probe.session,SECRET,allow_empty_search=True,diagnostic=diagnostic)
    assert diagnostic['window_selection']=='CURRENT_TEMU_NOT_READY'
    assert len(probe.requests)==2 and ('POST','/window') not in paths(probe)


def test_http_temu_document_counts_as_ambiguity_next_to_https_empty_candidate(probe):
    values(probe,False,'https://passport.geekbi.com/login','aux',['aux','https-temu','http-temu'],
           None,'https://www.temu.com/search_result.html',None,'http://www.temu.com/bgn_verification.html',None)
    diagnostic={}
    assert not selection.ready_after_verification(probe.session,SECRET,allow_empty_search=True,diagnostic=diagnostic)
    assert diagnostic['window_selection']=='AMBIGUOUS_TEMU_WINDOWS' and diagnostic['code']=='PAGE_NOT_READY'
    assert json.loads(probe.requests[-1][0].data)=={'handle':'aux'}
    assert sum(request.full_url.endswith('/execute/sync') for request,_ in probe.requests)==1
