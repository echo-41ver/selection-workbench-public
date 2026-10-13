import json
from types import SimpleNamespace
import pytest
from selection_automation import browser_control_recovery as recovery
from selection_automation.worker_runtime import WorkerLease, WorkerStopRequested


@pytest.mark.parametrize('profile,expected',[('old-profile',True),('new-profile',False)])
def test_control_stop_is_scoped_to_its_browser_profile(tmp_path,profile,expected):
    progress={'status':'STOPPED_BROWSER_CONTROL_RECOVERY_TIMEOUT','control_recovery_profile_id':profile}
    assert recovery.control_stop_was_replaced(tmp_path,{'profile_id':'new-profile'},progress) is expected


@pytest.mark.parametrize('phase,new_profile,updated,expected',[
    ('REPLACED','new-profile','2026-10-13T07:52:41Z',True),
    ('BIND_FAILED','new-profile','2026-10-13T07:52:41Z',False),
    ('REPLACED','different-profile','2026-10-13T07:52:41Z',False),
    ('REPLACED','new-profile','2026-10-13T08:00:00Z',False),
])
def test_legacy_control_stop_requires_a_newer_completed_replacement(tmp_path,phase,new_profile,updated,expected):
    journal=tmp_path/'runtime/proxyshare-chains/replacement-worker-1.json'
    journal.parent.mkdir(parents=True)
    journal.write_text(json.dumps({'phase':phase,'new_profile_id':new_profile,
        'replaced_at':'2026-10-13T15:58:30+08:00'}),encoding='utf-8')
    progress={'status':'STOPPED_BROWSER_CONTROL_RECOVERY_TIMEOUT','updated_at':updated}
    assert recovery.control_stop_was_replaced(tmp_path,{'name':'worker-01-new','profile_id':'new-profile'},progress) is expected


def test_wait_probes_until_fenced_and_usable_without_replaying_commands(tmp_path):
    sequence=iter([False,True,True]); calls=[]; waits=[]; observations=[]
    lease=SimpleNamespace(check_stop=lambda:None)
    def fence(root,session):
        calls.append('read_url'); return next(sequence)
    def ready(root,session,query):
        calls.append('read_dom'); return calls.count('read_dom')==2
    checkpoint=tmp_path/'products.jsonl';checkpoint.write_text('{"goods_id":"saved"}\n')
    before=checkpoint.read_bytes()
    recovery.wait_until_ready(tmp_path,{},'fuel pump',lease=lease,stopped=lambda:False,
        observe=lambda *args:observations.append(args),fence=fence,ready=ready,
        wait=lambda seconds,lease:waits.append(seconds))
    assert calls==['read_url','read_url','read_dom','read_url','read_dom']
    assert waits==[10,10]
    assert observations==[(1,False,False),(2,True,False),(3,True,True)]
    assert checkpoint.read_bytes()==before


def test_explicit_task_stop_ends_recovery_before_any_browser_command(tmp_path):
    def forbidden(*args):pytest.fail('stop must precede browser checks')
    with pytest.raises(WorkerStopRequested):
        recovery.wait_until_ready(tmp_path,{},'pump',lease=SimpleNamespace(check_stop=lambda:None),
            stopped=lambda:True,observe=forbidden,fence=forbidden,ready=forbidden)
    stop=tmp_path/'STOP';stop.touch()
    with pytest.raises(WorkerStopRequested):
        WorkerLease(tmp_path/'leases','worker1',task_stop_path=stop).check_stop()

def test_gate_after_command_timeout_is_reported_without_replaying_or_waiting(tmp_path):
    calls=[]
    with pytest.raises(recovery.BrowserGateDetected) as error:
        recovery.wait_until_ready(tmp_path,{},'pump',lease=SimpleNamespace(check_stop=lambda:None),
            stopped=lambda:False,observe=lambda *a:calls.append('observe'),
            fence=lambda *a:True,ready=lambda *a:False,
            gate_probe=lambda *a:'WAITING_HUMAN_VERIFICATION',
            wait=lambda *a:pytest.fail('a gate must leave automatic command recovery'))
    assert error.value.status=='WAITING_HUMAN_VERIFICATION'
    assert calls==[]


@pytest.mark.parametrize('url,expected',[
    ('https://www.temu.com/',True),
    ('https://www.temu.com/search_result.html',True),
    ('https://www.temu.com/search_result.html?search_key=other',True),
    ('about:neterror',False),('https://elsewhere.test/',False),
    (None,False),({'error':'invalid session id'},False),
])
def test_responsive_control_read_fences_command_even_on_empty_entry(tmp_path,monkeypatch,url,expected):
    (tmp_path/'sid').write_text('owned-session')
    requests=[]
    class Response:
        def __enter__(self):return self
        def __exit__(self,*args):pass
        def read(self,*args):return json.dumps({'value':url}).encode()
    def open_request(request,timeout):
        requests.append((request.get_method(),request.full_url,timeout));return Response()
    monkeypatch.setattr(recovery.urllib.request,'build_opener',lambda *args:SimpleNamespace(open=open_request))
    assert recovery.control_fenced(tmp_path,{'session_id_file':'sid','webdriver_url':'http://127.0.0.1:4444'}) is expected
    assert requests==[('GET','http://127.0.0.1:4444/session/owned-session/url',5)]


def test_waiting_control_view_keeps_count_and_reports_automatic_resume(tmp_path,monkeypatch):
    import selection
    from test_supervision_status import worker_fixture
    monkeypatch.setattr(selection,'ROOT',tmp_path)
    worker,output=worker_fixture(tmp_path,'STOPPED_BROWSER_COMMAND_TIMEOUT',
        worker_status='WAITING_BROWSER_CONTROL_RECOVERY',unique_goods=634)
    _,view,blocked=selection.worker_snapshot(tmp_path,'worker1')
    assert view['唯一商品数']==634
    assert view['采集状态']=='WAITING_BROWSER_CONTROL_RECOVERY'
    assert '自动检查' in view['状态']
    assert not view['需人工介入'] and blocked is None


@pytest.mark.parametrize('owned',[True,False])
def test_live_owned_collector_supersedes_stale_resuming_view(tmp_path,monkeypatch,owned):
    import selection,psutil
    from test_supervision_status import worker_fixture,write
    monkeypatch.setattr(selection,'ROOT',tmp_path)
    worker,output=worker_fixture(tmp_path,'RUNNING',worker_status='RESUMING',unique_goods=634)
    write(tmp_path/'runtime/worker-leases/worker1.owner.json',
        {'pid':123,'worker':'worker1','purpose':'collector'})
    args=['python',str(tmp_path/'scripts/pipeline.py'),'--worker-name','worker1' if owned else 'worker2']
    monkeypatch.setattr(psutil,'Process',lambda pid:SimpleNamespace(cmdline=lambda:args))
    _,view,_=selection.worker_snapshot(tmp_path,'worker1')
    assert view['采集状态']==('RUNNING' if owned else 'RESUMING')
    if owned:assert view['唯一商品数']==634


@pytest.mark.parametrize('path,text,enabled,expected',[
    ('/','',True,True),('/search_result.html','',True,True),
    ('/search_result.html?search_key=other','',True,True),
    ('/bgn_verification.html','',True,False),
    ('/search_result.html','Verify you are human',True,False),
    ('/','',False,False),('/','Access Denied',True,False),
    ('/search_result.html','请输入验证码',True,False),
    ('/search_result.html','安全验证',True,False),
])
def test_actual_readiness_javascript_accepts_recovered_entry_and_rejects_blocks(path,text,enabled,expected):
    import subprocess
    fixture={'path':path,'text':text,'enabled':enabled}
    stub=r"""
const fixture=FIXTURE;
global.location={href:'https://www.temu.com'+fixture.path,protocol:'https:'};
const input={disabled:!fixture.enabled,readOnly:false,getBoundingClientRect:()=>({width:300,height:40})};
global.getComputedStyle=()=>({display:'block',visibility:'visible'});
global.document={readyState:'complete',body:{innerText:fixture.text},querySelector:()=>input,querySelectorAll:()=>[]};
""".replace('FIXTURE',json.dumps(fixture))
    result=subprocess.run(['node','-e',stub+'console.log(new Function('+json.dumps(recovery.READY_SCRIPT)+')("fuel pump"))'],
        capture_output=True,text=True,timeout=10)
    assert result.returncode==0,result.stderr
    assert result.stdout.strip()==str(expected).lower()


@pytest.mark.parametrize('blocked',[False,True])
def test_pipeline_waits_then_resumes_same_query_sort_and_checkpoint(tmp_path,monkeypatch,blocked):
    import pipeline
    from pathlib import Path
    from test_supervision_status import write
    monkeypatch.setattr(pipeline,'ROOT',tmp_path)
    category={'category_key':'fixture'};base=pipeline.run_root(category)
    sessions=tmp_path/'sessions.json'
    write(sessions,{'workers':[{'name':'worker1','profile_id':'same-profile','webdriver_url':'http://127.0.0.1:4444',
        'session_id_file':'sid','bidi_url_file':'bidi'}]})
    write(tmp_path/'config/proxy_policy.json',{'session_rotation_policy':'NEW_ADSPOWER_PROFILE_AND_NEW_PROXY_SESSION',
        'delete_old_profile_first_via_adspower':True,'worker_chain_ports':[19081]})
    write(pipeline.keyword_plan_path(category),{'keywords':[{'query':'fuel pump','max_products':2}]})
    monkeypatch.setattr(pipeline,'ensure_proxy_chain',lambda *args:False)
    calls=[];observed=[]
    def collect(command,**kwargs):
        calls.append(command.copy());out=Path(command[command.index('--output')+1])
        order=command[command.index('--sort')+1]
        evidence={'requested':order,'state':'UI_CONFIRMED','query':'fuel pump'}
        if len(calls)==1:
            write(out/'progress.json',{'status':'STOPPED_BROWSER_COMMAND_TIMEOUT','unique_goods':1,
                'manual_action_required':True,'automatic_retry':False,'sales_sort':evidence})
            (out/'products.jsonl').write_text('{"goods_id":"saved"}\n')
        else:
            write(out/'progress.json',{'status':'KEYWORD_LIMIT_REACHED','unique_goods':2,'sales_sort':evidence})
            (out/'products.jsonl').write_text(''.join(json.dumps({'goods_id':str(i),'search_sort':evidence})+'\n' for i in range(2)))
        return 1 if len(calls)==1 else 0
    def wait(root,session,query,**kwargs):
        checkpoint=base/'temu/workers/worker1/fuel-pump/products.jsonl'
        before=checkpoint.read_bytes()
        if blocked:raise recovery.BrowserGateDetected('WAITING_HUMAN_VERIFICATION')
        kwargs['observe'](1,False,False);kwargs['observe'](2,True,True)
        assert checkpoint.read_bytes()==before
        observed.append((session['profile_id'],query))
    monkeypatch.setattr(pipeline,'run_collector',collect)
    monkeypatch.setattr(recovery,'wait_until_ready',wait)
    monkeypatch.setattr(pipeline.subprocess,'run',lambda *args,**kwargs:pytest.fail('must not replace browser'))
    if blocked:
        with pytest.raises(RuntimeError,match='incomplete'):
            pipeline.collect_temu(tmp_path/'category.json',category,sessions,'worker1')
        worker=json.loads((base/'temu/supervisors/worker1.json').read_text())
        assert worker['status']=='HUMAN_ACTION_REQUIRED'
        assert worker['unique_goods']==1 and worker['browser_preserved']
        assert len(calls)==1
        assert (base/'temu/workers/worker1/fuel-pump/products.jsonl').read_text()=='{"goods_id":"saved"}\n'
        return
    pipeline.collect_temu(tmp_path/'category.json',category,sessions,'worker1')
    assert observed==[('same-profile','fuel pump')]
    assert calls[0]==calls[1]
    assert len(calls)==3
    worker=json.loads((base/'temu/supervisors/worker1.json').read_text())
    assert worker['status']=='WORKER_COMPLETE' and worker['completed_queries']==['fuel pump']


def test_recovery_deadline_is_bounded_even_when_driver_never_finishes(tmp_path):
    clock=[0]; waits=[]; calls=[]
    def wait(seconds,lease):
        waits.append(seconds);clock[0]+=seconds
    with pytest.raises(recovery.BrowserControlRecoveryTimeout):
        recovery.wait_until_ready(tmp_path,{},'mouse',lease=SimpleNamespace(check_stop=lambda:None),
            stopped=lambda:False,observe=lambda *a:None,fence=lambda *a:calls.append('url') or False,
            ready=lambda *a:pytest.fail('unfenced DOM must never be read'),wait=wait,
            interval=10,timeout_seconds=25,clock=lambda:clock[0])
    assert waits==[10,10,5] and calls==['url']*3 and clock[0]==25


def test_slow_fence_cannot_start_more_probes_past_deadline(tmp_path):
    clock=[0]
    def fence(*a):clock[0]=120;return True
    with pytest.raises(recovery.BrowserControlRecoveryTimeout):
        recovery.wait_until_ready(tmp_path,{},'mouse',lease=SimpleNamespace(check_stop=lambda:None),
            stopped=lambda:False,observe=lambda *a:None,fence=fence,
            gate_probe=lambda *a:pytest.fail('deadline must precede the next command'),
            ready=lambda *a:pytest.fail('deadline must precede the next command'),clock=lambda:clock[0])


@pytest.mark.parametrize('stop_at',['fence','gate','wait'])
def test_stop_during_control_recovery_prevents_all_following_browser_operations(tmp_path,stop_at):
    state={'stop':False};calls=[]
    def call(name,result):
        calls.append(name)
        if name==stop_at:state['stop']=True
        return result
    with pytest.raises(WorkerStopRequested):
        recovery.wait_until_ready(tmp_path,{},'mouse',lease=SimpleNamespace(check_stop=lambda:None),
            stopped=lambda:state['stop'],observe=lambda *a:None,
            fence=lambda *a:call('fence',True),gate_probe=lambda *a:call('gate',None),
            ready=lambda *a:call('ready',False),wait=lambda *a:call('wait',None))
    assert calls=={'fence':['fence'],'gate':['fence','gate'],
                   'wait':['fence','gate','ready','wait']}[stop_at]


def test_gate_is_checked_before_a_surviving_search_input_can_claim_ready(tmp_path):
    calls=[]
    with pytest.raises(recovery.BrowserGateDetected):
        recovery.wait_until_ready(tmp_path,{},'mouse',lease=SimpleNamespace(check_stop=lambda:None),
            stopped=lambda:False,observe=lambda *a:pytest.fail('gate cannot claim readiness'),
            fence=lambda *a:calls.append('url') or True,
            gate_probe=lambda *a:calls.append('gate') or 'WAITING_HUMAN_VERIFICATION',
            ready=lambda *a:pytest.fail('search input under gate is not usable'))
    assert calls==['url','gate']


def test_control_budget_only_resets_above_saved_unique_goods_high_water(tmp_path):
    journal=tmp_path/'recovery.json'
    for count,expected in [(10,1),(0,2),(10,3)]:
        allowed,record=recovery.reserve_control_recovery(journal,count)
        assert allowed and record['attempts_without_progress']==expected
    before=journal.read_bytes()
    allowed,record=recovery.reserve_control_recovery(journal,10)
    assert not allowed and record['attempts_without_progress']==3
    assert journal.read_bytes()==before
    allowed,record=recovery.reserve_control_recovery(journal,11)
    assert allowed and record['attempts_without_progress']==1
    assert record['unique_goods_high_water']==11 and record['progress_since_previous_recovery']


def test_control_budget_survives_a_fresh_python_process(tmp_path):
    import subprocess,sys
    journal=tmp_path/'recovery.json'
    for _ in range(3):
        assert recovery.reserve_control_recovery(journal,5)[0]
    code=('from selection_automation.browser_control_recovery import reserve_control_recovery; '
          'import json,sys; print(json.dumps(reserve_control_recovery(sys.argv[1],5)))')
    result=subprocess.run([sys.executable,'-c',code,str(journal)],capture_output=True,text=True,timeout=10)
    assert result.returncode==0,result.stderr
    allowed,record=json.loads(result.stdout)
    assert not allowed and record['attempts_without_progress']==3


def test_invalid_control_budget_never_silently_grants_new_attempts(tmp_path):
    journal=tmp_path/'recovery.json';journal.write_text('{broken')
    allowed,record=recovery.reserve_control_recovery(journal,5)
    assert not allowed and record['reason']=='RECOVERY_RECORD_INVALID'
    assert journal.read_text()=='{broken'


@pytest.fixture
def control_pipeline(tmp_path,monkeypatch):
    import pipeline
    from test_supervision_status import write
    monkeypatch.setattr(pipeline,'ROOT',tmp_path)
    category={'category_key':'fixture'};base=pipeline.run_root(category)
    sessions=tmp_path/'sessions.json'
    write(sessions,{'workers':[{'name':'worker1','profile_id':'same-profile','webdriver_url':'http://127.0.0.1:4444',
        'session_id_file':'sid','bidi_url_file':'bidi'}]})
    write(tmp_path/'config/proxy_policy.json',{'session_rotation_policy':'NEW_ADSPOWER_PROFILE_AND_NEW_PROXY_SESSION',
        'delete_old_profile_first_via_adspower':True,'worker_chain_ports':[19081]})
    write(pipeline.keyword_plan_path(category),{'keywords':[{'query':'mouse','max_products':100} ]})
    monkeypatch.setattr(pipeline,'ensure_proxy_chain',lambda *a:False)
    output=base/'temu/workers/worker1/mouse';output.mkdir(parents=True)
    (output/'products.jsonl').write_text('{"goods_id":"saved"}\n')
    monkeypatch.setattr(pipeline.subprocess,'run',lambda *a,**kw:pytest.fail('control fault cannot replace a browser'))
    return pipeline,category,sessions,base,output


@pytest.mark.parametrize('timeout',[True,False])
def test_pipeline_control_timeout_or_budget_stops_and_restart_preserves_data(control_pipeline,monkeypatch,timeout):
    pipeline,category,sessions,base,output=control_pipeline
    calls=[];waits=[]
    def collect(*a,**kw):
        calls.append('collect')
        (output/'progress.json').write_text(json.dumps({'status':'STOPPED_BROWSER_COMMAND_TIMEOUT',
            'unique_goods':1,'manual_action_required':True,'automatic_retry':False}))
        return 1
    def wait(*a,**kw):
        waits.append('wait')
        if timeout:raise recovery.BrowserControlRecoveryTimeout()
    monkeypatch.setattr(pipeline,'run_collector',collect)
    monkeypatch.setattr(recovery,'wait_until_ready',wait)
    for _ in range(2):
        with pytest.raises(RuntimeError,match='incomplete'):
            pipeline.collect_temu(base/'category.json',category,sessions,'worker1')
    assert len(calls)==(1 if timeout else 4)
    assert len(waits)==(1 if timeout else 3)
    progress=json.loads((output/'progress.json').read_text())
    expected='STOPPED_BROWSER_CONTROL_RECOVERY_'+('TIMEOUT' if timeout else 'BUDGET')
    assert progress['status']==expected
    worker=json.loads((base/'temu/supervisors/worker1.json').read_text(encoding='utf-8'))
    assert worker['query_status']==expected and worker['browser_preserved']
    assert worker['unique_goods']==1 and not worker['automatic_resume_pending']
    assert (output/'products.jsonl').read_text()=='{"goods_id":"saved"}\n'
    import selection
    _,view,blocked=selection.worker_snapshot(base,'worker1')
    assert view['采集状态']==expected and view['需人工介入']
    assert blocked and not blocked['verify_to_resume']


@pytest.mark.parametrize('started',[False,True])
def test_cold_start_gate_uses_existing_collection_phase_policy_before_collector(control_pipeline,monkeypatch,started):
    pipeline,category,sessions,base,output=control_pipeline
    (output/'progress.json').write_text(json.dumps({'status':'STOPPED_BROWSER_COMMAND_TIMEOUT',
        'unique_goods':1,'scroll_collection_started':started,'manual_action_required':True,'automatic_retry':False}))
    def gate(*a,**kw):raise recovery.BrowserGateDetected('WAITING_HUMAN_VERIFICATION')
    monkeypatch.setattr(recovery,'wait_until_ready',gate)
    monkeypatch.setattr(pipeline,'run_collector',lambda *a,**kw:pytest.fail('gate must precede collector startup'))
    replacements=[]
    def replacement(command,**kw):
        replacements.append(command)
        assert command[1].endswith('replace_proxyshare_worker_environment.py')
        raise RuntimeError('existing CAPTCHA policy reached')
    monkeypatch.setattr(pipeline.subprocess,'run',replacement)
    with pytest.raises(RuntimeError,match='existing CAPTCHA policy reached' if started else 'incomplete'):
        pipeline.collect_temu(base/'category.json',category,sessions,'worker1')
    assert len(replacements)==(1 if started else 0)
    assert (output/'products.jsonl').read_text()=='{"goods_id":"saved"}\n'
