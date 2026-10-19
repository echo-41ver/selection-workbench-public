import importlib.util
import json
import os
from pathlib import Path
import sys
from types import SimpleNamespace
import threading
import http.client
import time
import pytest

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'scripts'))
import web_console as web


def test_agent_speed_uses_its_own_successful_groups_and_current_width(tmp_path):
    jobs=tmp_path/'stage123/codex_pool/jobs';jobs.mkdir(parents=True)
    def put(name,**changes):
        folder=jobs/name;folder.mkdir()
        row={'state':'complete','agent_id':'doubao','model':'专家','started_at':940,'finished_at':1000,
             'imported':2,'identities':[['A','1','v'],['A','2','v']],**changes}
        (folder/'job.json').write_text(json.dumps(row),encoding='utf-8')
    put('success');put('retry',state='retry');put('other-model',model='快速')
    put('observation',observation_only=True);put('other-agent',agent_id='codex')
    provider={'agent_ids':['doubao'],'agent_models':{'doubao':'专家'},'workers':4}
    speed=web.agent_review_speed(tmp_path,provider,{'created':100,'workers':4},now=1060)
    assert speed['recent_rate_per_hour']==8 and speed['sample_groups']==1
    assert speed['estimated_rate_per_hour']==480
    assert web.agent_review_speed(tmp_path,provider,{'workers':8},now=1060)['estimated_rate_per_hour']==960
    assert web.agent_review_speed(tmp_path,provider,{},now=2000)['recent_rate_per_hour']==0


def test_live_state_overrides_cached_captcha_without_rescanning_results(tmp_path,monkeypatch):
    console=web.Console(tmp_path);console.key='fixture'
    old={'ok':True,'category_key':'fixture','stage3_runtime':{'paused':True,'agent_blocks':{'doubao':{'code':'HUMAN_VERIFICATION_REQUIRED'}}},
         'stage3_provider':{'mode':'local_agent','agent_ids':['doubao']},
         'stage3_control':{'progress':{'total':100,'completed':12,'pending':88},
                           'codex_review':{'target':96,'imported':12,'pool':{'running_groups':0,'phase':'等待验证码'}}}}
    monkeypatch.setattr(console.monitor,'get',lambda:json.loads(json.dumps(old)))
    monkeypatch.setattr(console.live_monitor,'get',lambda:{'ok':True,'category_key':'fixture',
        'status_sample':{'age_seconds':0,'sampled_at':time.time(),'error':False},
        'stage3_runtime':{'paused':False,'agent_blocks':{},'paused_agents':['acciowork']},
        'stage3_provider':{'mode':'local_agent','agent_ids':['doubao'],'agent_models':{'doubao':'专家'}},
        'stage3_concurrency':{'workers':8},'running':False,'enabled':False,'controller':{},
        'review':{'available':True,'running':True,'phase':'分析中','pool':{'running':True,'running_groups':8,'phase':'分析中'}}})
    monkeypatch.setattr(console,'stage3_snapshot',lambda *a:pytest.fail('live overlay must never rescan results'))
    state=console.monitor_status()
    assert state['stage3_runtime']['agent_blocks']=={}
    assert state['stage3_runtime']['paused_agents']==['acciowork']
    assert state['stage3_control']['codex_review']['pool']['running_groups']==8
    assert state['stage3_control']['codex_review']['imported']==12
    assert state['stage3_control']['progress']['completed']==12
    assert not state['details']['阶段']['stage3'].get('需人工介入')
    # A task switch must never combine another task's current control state.
    console.live_monitor.get=lambda:{'ok':True,'category_key':'other','status_sample':{'age_seconds':0}}
    assert console.monitor_status()['live_loading']
    assert old['stage3_runtime']['paused']


def test_fast_review_reader_does_not_scan_batch_tree(tmp_path,monkeypatch):
    folder=tmp_path/'stage123/codex_pool';folder.mkdir(parents=True)
    (folder/'pool.json').write_text(json.dumps({'workers':8,'running':8,'agents':[{'id':'doubao','running':8}]}))
    (folder.parent/'codex_pool_config.json').write_text('{"enabled":true}')
    monkeypatch.setattr(web,'_codex_pool_process_running',lambda *a:True)
    monkeypatch.setattr(Path,'glob',lambda *a:pytest.fail('fast runtime sampling must not scan batches'))
    state=web.codex_review_control(tmp_path,scan_batches=False)
    assert state['running'] and state['pool']['running_groups']==8


def test_invalid_live_gate_is_an_error_instead_of_a_cleared_captcha(tmp_path):
    console=web.Console(tmp_path);console.key='fixture'
    folder=tmp_path/'runs/fixture/stage123';folder.mkdir(parents=True)
    (folder.parent/'category.json').write_text('{}')
    (folder/'runtime_control.json').write_text('{incomplete')
    with pytest.raises(ValueError):console.live_status()


def test_stopped_task_is_neutral_in_full_status_and_fast_monitor(tmp_path,monkeypatch):
    console=web.Console(tmp_path);console.key='fixture'
    base=tmp_path/'runs/fixture';folder=base/'stage123';folder.mkdir(parents=True)
    web.selection.save(base/'category.json',{'category_name_zh':'测试','stage3':{'mode':'local_model'}})
    web.selection.save(base/'status.json',{'状态':'已停止'})
    (base/'STOP').write_text('用户请求停止',encoding='utf-8')
    web.selection.save(folder/'controller.json',{'error':True,'phase':'分析暂停，请查看错误记录'})
    monkeypatch.setattr(console,'owner',lambda:None)
    monkeypatch.setattr(console,'stage3_snapshot',lambda key:{'total':10,'completed':2,'pending':8})
    state=console.status()
    assert state['task_stopped'] and state['phase']=='已停止' and state['severity']=='neutral'
    assert state['stage3_control']['stopped'] and state['stage3_control']['error']
    assert not state['stage3_control']['can_stop'] and '已停止' in state['stage3_control']['phase']
    assert not state['details']['阶段']['stage3'].get('需人工介入')
    live=console.live_status();live['status_sample']={'age_seconds':0}
    monkeypatch.setattr(console.monitor,'get',lambda:state)
    monkeypatch.setattr(console.live_monitor,'get',lambda:live)
    assert console.monitor_status()['stage3_control']['stopped']
    # Starting again clears the acknowledgement even if an old error remains.
    (base/'STOP').unlink()
    assert not console.live_status()['task_stopped']


def test_health_endpoint_works_without_loading_console_reports(tmp_path):
    server=web.ThreadingHTTPServer(('127.0.0.1',0),web.Handler)
    server.console=SimpleNamespace(status=lambda:pytest.fail('health probe must not read task state'))
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    client=http.client.HTTPConnection('127.0.0.1',server.server_port,timeout=2)
    try:
        client.request('GET','/api/health')
        response=client.getresponse();state=json.loads(response.read())
        assert response.status==200 and state['service']=='selection-workbench' and state['pid']==os.getpid()
    finally:client.close();server.shutdown();server.server_close();thread.join()


def test_status_profile_is_local_one_shot_and_never_returns_status_values(tmp_path,monkeypatch):
    console=web.Console(tmp_path);calls=[]
    monkeypatch.setattr(console,'status',lambda:calls.append(True) or {
        'category_key':None,'api_key':'must-never-appear','private':'must-never-appear',
        'stage3_control':{'progress':{'counting':True}}})
    with pytest.raises(web.ConsoleError,match='未启用'):
        console.profile_status_once()
    assert not calls
    marker=tmp_path/'runtime/web-console/profile-next-status.json'
    marker.write_text('{}',encoding='utf-8')
    result=console.profile_status_once()
    assert result['ok'] and result['stage3_counting']
    assert len(result['functions'])<=20 and len(result['thread_stacks'])<=16
    assert all(len(row['frames'])<=8 for row in result['thread_stacks'])
    assert 'must-never-appear' not in json.dumps(result)
    assert not marker.exists()
    with pytest.raises(web.ConsoleError,match='未启用'):
        console.profile_status_once()
    assert calls==[True]


def test_stage3_same_revision_avoids_periodic_rescan_and_parallel_launches(tmp_path,monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from selection_automation import stage3_progress as progress
    console=web.Console(tmp_path)
    started=threading.Event();release=threading.Event();calls=[];revision=['first']
    monkeypatch.setattr(progress,'snapshot_signature',lambda *args:(revision[0],(),False))
    def count(*args):
        calls.append(revision[0]);started.set();release.wait(5)
        return {'signature':(revision[0],(),False),'category_key':args[0].name,'stable':True,
            'progress':{'total':5,'completed':5,'pending':0}}
    monkeypatch.setattr(console,'_read_stage3_snapshot',count)
    try:
        assert console.stage3_snapshot('fixture')=={'counting':True}
        assert started.wait(2)
        with ThreadPoolExecutor(max_workers=6) as pool:
            results=list(pool.map(lambda _:console.stage3_snapshot('fixture'),range(6)))
        assert len(calls)==1 and all(result.get('counting') for result in results)
    finally:release.set()
    deadline=time.monotonic()+2
    while console._stage3_refreshing and time.monotonic()<deadline:time.sleep(.01)
    cached=console._stage3_cache['fixture']
    console._stage3_cache['fixture']=(cached[0],time.monotonic()-3600,cached[2])
    assert console.stage3_snapshot('fixture')['completed']==5 and len(calls)==1
    revision[0]='new-candidates'
    # Retain the labelled preceding snapshot, never call it current or allow paid work.
    result=console.stage3_snapshot('fixture')
    assert result['completed']==5 and result['refreshing'] and result['current_inputs_pending']
    deadline=time.monotonic()+2
    while console._stage3_refreshing and time.monotonic()<deadline:time.sleep(.01)
    assert len(calls)==2
    assert console.stage3_snapshot('other-task')=={'counting':True}


def test_stage3_changes_during_read_stay_dirty_and_failed_reads_do_not_authorize(tmp_path,monkeypatch):
    from selection_automation import stage3_progress as progress
    console=web.Console(tmp_path);revision=['initial'];calls=[]
    monkeypatch.setattr(progress,'snapshot_signature',lambda *args:(revision[0],(),False))
    def count(*args):
        calls.append(True)
        if len(calls)==1:
            revision[0]='changed-during-read'
            return {'signature':('initial',(),False),'category_key':'fixture','stable':False,
                'progress':{'total':1,'completed':1,'pending':0}}
        raise ValueError('malformed input')
    monkeypatch.setattr(console,'_read_stage3_snapshot',count)
    assert console.stage3_snapshot('fixture')['counting']
    deadline=time.monotonic()+2
    while console._stage3_refreshing and time.monotonic()<deadline:time.sleep(.01)
    assert console.stage3_snapshot('fixture')['refreshing']
    deadline=time.monotonic()+2
    while console._stage3_refreshing and time.monotonic()<deadline:time.sleep(.01)
    result=console.stage3_snapshot('fixture')
    assert result['refreshing'] and result['current_inputs_pending'] and result['completed']==1 and len(calls)==2


def test_stage3_worker_is_separate_readonly_and_removes_paid_credentials(tmp_path,monkeypatch):
    console=web.Console(tmp_path);calls=[]
    monkeypatch.setenv('DEEPSEEK_API_KEY','must-not-leak')
    def run(command,**kwargs):
        calls.append((command,kwargs))
        kwargs['stdout'].write(b'{"category_key":"fixture","progress":{"total":0}}')
        return SimpleNamespace(pid=os.getpid(),returncode=0,wait=lambda timeout:kwargs.update(wait_timeout=timeout))
    monkeypatch.setattr(web.subprocess,'Popen',run)
    assert console._read_stage3_snapshot(tmp_path/'runs/fixture')['progress']['total']==0
    command,kwargs=calls[0]
    assert command[1:3]==['-m','selection_automation.stage3_progress']
    assert '--base' in command and 'DEEPSEEK_API_KEY' not in kwargs['env']
    assert kwargs['wait_timeout']==1800 and kwargs['stdin']==web.subprocess.DEVNULL
    assert console._stage3_readers=={}


def test_readonly_deadline_remains_bounded_and_cleans_owned_worker(tmp_path,monkeypatch):
    console=web.Console(tmp_path);base=tmp_path/'runs/fixture';cleaned=[];waits=[]
    def wait(timeout):
        waits.append(timeout)
        raise web.subprocess.TimeoutExpired(['reader'],timeout)
    monkeypatch.setattr(web.subprocess,'Popen',lambda *args,**kwargs:
        SimpleNamespace(pid=os.getpid(),wait=wait))
    monkeypatch.setattr(console,'_stop_stage3_reader',lambda child,path,created:cleaned.append(path))
    with pytest.raises(web.subprocess.TimeoutExpired) as exc:console._read_stage3_snapshot(base)
    assert waits==[1800] and exc.value.timeout==1800 and cleaned==[base]
    assert console._stage3_readers=={}
    saved=json.loads((base/'stage123/progress_reader_error.json').read_text(encoding='utf-8'))
    assert saved['error']['kind']=='TimeoutExpired'


@pytest.mark.parametrize('fault',[None,'dead','gone','termination_pending','created','parent','owner_created','task','installation'])
def test_reader_phase_requires_live_owned_identity(tmp_path,monkeypatch,fault):
    import psutil
    console=web.Console(tmp_path);base=tmp_path/'runs/fixture'
    console._stage3_refreshing.add('fixture');console._stage3_readers['fixture']=(10,100)
    web.selection.save(base/'stage123/progress_reader_state.json',{'pid':11,'created':101,
        'phase':'逐对核验当前图片与结果版本','counts':{'scanned':3000},'updated_at':102})
    owner=SimpleNamespace(pid=10,is_running=lambda:True,create_time=lambda:99 if fault=='owner_created' else 100)
    process=SimpleNamespace(pid=11,is_running=lambda:fault!='dead',status=lambda:'running',
        create_time=lambda:999 if fault=='created' else 101,ppid=lambda:9 if fault=='parent' else 10,
        cwd=lambda:str(tmp_path/'foreign' if fault=='installation' else tmp_path),
        cmdline=lambda:['python','-m','selection_automation.stage3_progress','--base',
            str(tmp_path/'runs/foreign' if fault=='task' else base)])
    def lookup(pid):
        if pid==11 and fault=='gone':raise psutil.NoSuchProcess(pid)
        return owner if pid==10 else process
    monkeypatch.setattr(psutil,'Process',lookup)
    monkeypatch.setattr(console,'_reader_native_active',lambda pid:fault!='termination_pending')
    result=console._stage3_reader_progress('fixture')
    if fault:assert result=={}
    else:
        assert result['reader_live'] and result['reader_scanned']==3000
        assert '已核对 3,000 项候选' in result['message']
    console._stage3_refreshing.clear()
    assert console._stage3_reader_progress('fixture')=={}


@pytest.mark.skipif(os.name!='nt',reason='Windows native process exit state')
@pytest.mark.parametrize('exit_code,expected',[(259,True),(15,False)])
def test_reader_native_exit_state_rejects_termination_pending(tmp_path,monkeypatch,exit_code,expected):
    import ctypes
    console=web.Console(tmp_path);closed=[]
    def read(handle,pointer):pointer._obj.value=exit_code;return 1
    kernel=SimpleNamespace(OpenProcess=lambda *args:99,GetExitCodeProcess=read,
        CloseHandle=lambda handle:closed.append(handle))
    monkeypatch.setattr(ctypes,'WinDLL',lambda *args,**kwargs:kernel)
    assert console._reader_native_active(123) is expected
    assert closed==[99]


def test_invalid_reader_document_never_presents_counts_as_verified(tmp_path,monkeypatch):
    from selection_automation import stage3_progress as progress
    console=web.Console(tmp_path)
    monkeypatch.setattr(progress,'snapshot_signature',lambda *args:('current',(),False))
    monkeypatch.setattr(console,'_read_stage3_snapshot',lambda base:{'category_key':'fixture',
        'progress':{'total':4,'completed':4,'pending':0}})
    console.stage3_snapshot('fixture')
    deadline=time.monotonic()+1
    while console._stage3_refreshing and time.monotonic()<deadline:time.sleep(.01)
    result=console.stage3_snapshot('fixture')
    assert result['counting'] and result['error_kind']=='KeyError'
    assert 'KeyError' in result['message']


def test_reader_timeout_cleanup_targets_only_owned_readonly_interpreter(tmp_path,monkeypatch):
    import psutil
    console=web.Console(tmp_path);base=tmp_path/'runs/fixture';stopped=[]
    worker=SimpleNamespace(cmdline=lambda:['python','-m','selection_automation.stage3_progress','--base',str(base)],
        cwd=lambda:str(tmp_path),create_time=lambda:11,terminate=lambda:stopped.append('worker'))
    browser=SimpleNamespace(cmdline=lambda:['flowerbrowser.exe'],terminate=lambda:pytest.fail('Browser must survive'))
    owner=SimpleNamespace(create_time=lambda:10,children=lambda recursive:[worker,browser])
    monkeypatch.setattr(psutil,'Process',lambda pid:owner)
    child=SimpleNamespace(pid=123,kill=lambda:stopped.append('launcher'),wait=lambda timeout:None)
    console._stop_stage3_reader(child,base,10)
    assert stopped==['worker','launcher']
    stopped.clear()
    console._stop_stage3_reader(child,base,99)
    assert stopped==[]


def test_reader_failure_logs_structure_and_never_arbitrary_stderr(tmp_path,monkeypatch):
    console=web.Console(tmp_path);base=tmp_path/'runs/fixture'
    def fail(*args,**kwargs):
        raise web.subprocess.CalledProcessError(1,['python'],stderr=b"private-key-must-not-appear\nModuleNotFoundError: No module named 'fixture_dependency'")
    monkeypatch.setattr(web.subprocess,'Popen',fail)
    with pytest.raises(web.subprocess.CalledProcessError):console._read_stage3_snapshot(base)
    data=json.loads((base/'stage123/progress_reader_error.json').read_text(encoding='utf-8'))
    assert data['error']['kind']=='CalledProcessError'
    assert data['error']['missing_module']=='fixture_dependency'
    assert 'private-key-must-not-appear' not in json.dumps(data)


def test_market_scan_does_not_block_status_or_repeat_on_parallel_polls(tmp_path,monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    console=web.Console(tmp_path);console.key='fixture'
    base=tmp_path/'runs/fixture/temu/supervisors';base.mkdir(parents=True)
    path=base/'worker-01.json'
    web.selection.save(path,{'worker':'worker-01','status':'RUNNING','current_query':'first'})
    monkeypatch.setattr(console,'owner',lambda:None)
    monkeypatch.setattr(console,'stage3_snapshot',lambda key:{})
    monkeypatch.setattr(web.selection,'worker_snapshot',lambda *args:({}, {}, None))
    started=threading.Event();release=threading.Event();calls=[]
    def scan(base):
        calls.append(base);started.set();release.wait(5)
        return {'keywords':[{'query':'fixture'}]}
    monkeypatch.setattr(web,'build_frontend_market',scan)
    try:
        first=console.status()
        assert started.wait(2)
        assert first['frontend_market']=={'counting':True}
        web.selection.save(path,{'worker':'worker-01','status':'RUNNING','current_query':'second'})
        with ThreadPoolExecutor(max_workers=6) as pool:
            states=list(pool.map(lambda _:console.status(),range(6)))
        assert len(calls)==1
        assert all(state['workers'][0]['current_query']=='second' for state in states)
    finally:release.set()


def test_market_cache_isolates_tasks_and_keeps_good_snapshot_on_failure(tmp_path,monkeypatch):
    console=web.Console(tmp_path)
    console._frontend_cache['first']=(time.monotonic()-16,{'keywords':['saved-first']})
    started=threading.Event();release=threading.Event()
    def failing_scan(base):
        started.set();release.wait(5);raise OSError('partially published snapshot')
    monkeypatch.setattr(web,'build_frontend_market',failing_scan)
    try:
        assert console.frontend_market_snapshot('first')=={'keywords':['saved-first']}
        assert started.wait(2)
        assert console.frontend_market_snapshot('second')=={'counting':True}
    finally:release.set()
    deadline=time.monotonic()+2
    while console._frontend_refreshing and time.monotonic()<deadline:time.sleep(.01)
    assert not console._frontend_refreshing
    assert console.frontend_market_snapshot('first')=={'keywords':['saved-first']}
    assert console.frontend_market_snapshot('second')=={'counting':True}
    assert not console._frontend_refreshing  # Failed refreshes also respect the refresh interval.


def test_resume_uses_current_saved_task_and_never_regenerates_or_enables_paid_analysis(tmp_path,monkeypatch):
    console=web.Console(tmp_path)
    base=tmp_path/'runs/current';base.mkdir(parents=True)
    web.selection.save(base/'category.json',{'category_key':'current','category_name_zh':'遥控器','execution_mode':'collection'})
    web.selection.save(tmp_path/'runtime/last_run.json',{'category_key':'current'})
    data=base/'temu/products.jsonl';data.parent.mkdir();data.write_text('existing-data')
    monkeypatch.setattr(console,'owner',lambda:None)
    calls=[]
    monkeypatch.setattr(console,'invoke',lambda argv:(calls.append(argv) or SimpleNamespace(returncode=0)))
    assert console.resume_current({})['category_key']=='current'
    assert calls[0][1:]==['start','--preserve-prepared','--category',str(base/'category.json')]
    assert '--paid' not in calls[0]
    assert data.read_text()=='existing-data'
    monkeypatch.setattr(console,'owner',lambda:{'category_key':'current'})
    with pytest.raises(web.ConsoleError):console.resume_current({})
    assert len(calls)==1


def test_retry_preparation_reuses_frozen_task_without_paid_flag(tmp_path,monkeypatch):
    console=web.Console(tmp_path);console.key='fixture'
    base=tmp_path/'runs/fixture';base.mkdir(parents=True)
    (base/'category.json').write_text(json.dumps({'category_key':'fixture'}))
    (base/'status.json').write_text(json.dumps({'阶段':{'bootstrap':{'状态':'启动准备失败'}}}))
    monkeypatch.setattr(console,'owner',lambda:None)
    calls=[]
    def invoke(argv):
        calls.append(argv);return SimpleNamespace(returncode=0,stdout=None)
    monkeypatch.setattr(console,'invoke',invoke)
    assert console.retry_preparation({'category_key':'fixture'})['ok']
    assert calls[0][1:]==['start','--category',str(base/'category.json')]
    assert '--paid' not in calls[0]
    monkeypatch.setattr(console,'owner',lambda:{'category_key':'fixture'})
    with pytest.raises(web.ConsoleError):console.retry_preparation({'category_key':'fixture'})
    assert len(calls)==1
    with pytest.raises(web.ConsoleError):console.retry_preparation({'category_key':'other'})


def test_failed_preparation_preserves_resolved_listing_and_key_only_in_memory(tmp_path,monkeypatch):
    console=web.Console(tmp_path)
    console.pending_scope={'category':'键盘','amazon_count':20,'per_keyword':200,'worker_count':4,
        'api_base_url':'https://provider.example','api_model':'test-model','bestsellers_url':''}
    target='https://www.amazon.com/zgbs/pc/12879431'
    def fail(args,**kwargs):
        path=Path(args[args.index('--progress-file')+1])
        path.write_text(json.dumps({'stage':'error','failed_stage':'generating','message':'model timeout',
            'route':[{'url':target,'heading':'Best Sellers in Computer Keyboards'}]}))
        return SimpleNamespace(returncode=1)
    monkeypatch.setattr(console,'invoke',fail)
    console._start({'category_name':'键盘','amazon_count':20,'per_keyword':200,
        'api_base_url':'https://provider.example','api_model':'test-model','api_key':'TEST_ONLY_PENDING_KEY'})
    assert console.pending_scope['bestsellers_url']==target
    assert console.pending_key=='TEST_ONLY_PENDING_KEY'
    for path in tmp_path.rglob('*'):
        if path.is_file():assert 'TEST_ONLY_PENDING_KEY' not in path.read_text('utf-8')
    assert web.Console(tmp_path).pending_key is None
    console.progress_file=tmp_path/'waiting.json'
    console.progress_file.write_text(json.dumps({'stage':'generating','message':'model working',
        'model_waiting':True,'model_started_at':time.time()-10}))
    assert '已等待 10 秒' in console.generation_progress()['message']


@pytest.mark.parametrize('other_category',[False,True])
def test_preparation_retry_key_is_bound_to_same_category_and_provider(tmp_path,monkeypatch,other_category):
    console=web.Console(tmp_path);console.pending_key='TEST_ONLY_PENDING_KEY'
    console.pending_scope={'category':'键盘','api_base_url':'https://provider.example','api_model':'test-model',
        'bestsellers_url':'https://www.amazon.com/zgbs/pc/12879431'}
    monkeypatch.setattr(console,'owner',lambda:None);started=[]
    class Thread:
        def __init__(self,target,args,daemon):started.append(args[0])
        def start(self):pass
    monkeypatch.setattr(web.threading,'Thread',Thread)
    body={'category_name':'电钻' if other_category else '键盘','amazon_count':20,'per_keyword':200,
        'api_key':'','api_base_url':'https://provider.example','api_model':'test-model'}
    if other_category:
        with pytest.raises(web.ConsoleError):console.start(body)
        assert not started
    else:
        console.start(body)
        assert started[0]['api_key']=='TEST_ONLY_PENDING_KEY'
        assert started[0]['bestsellers_url']==console.pending_scope['bestsellers_url']


def test_start_can_use_server_credential_without_returning_it(tmp_path, monkeypatch):
    console=web.Console(tmp_path)
    monkeypatch.setenv('DEEPSEEK_API_KEY','server-only-fixture')
    monkeypatch.setattr(console,'owner',lambda:None)
    pending=[]
    class Thread:
        def __init__(self, target, args, daemon): pending.append(args[0])
        def start(self): pass
    monkeypatch.setattr(web.threading,'Thread',Thread)
    body={'category_name':'测试','amazon_count':10,'per_keyword':500,'api_key':''}
    result=console.start(body)
    assert body['api_key']==''
    assert pending[0]['api_key']=='server-only-fixture'
    state=console.status()
    assert state['saved_credential_available'] is True
    assert 'server-only-fixture' not in json.dumps([result,state])
    console.busy=False
    console.start({**body,'api_key':'explicit-fixture'})
    assert pending[-1]['api_key']=='explicit-fixture'


def test_missing_saved_credential_gives_actionable_error(tmp_path, monkeypatch):
    monkeypatch.delenv('DEEPSEEK_API_KEY',raising=False)
    console=web.Console(tmp_path)
    with pytest.raises(web.ConsoleError) as error:
        console.start({'category_name':'测试','amazon_count':10,'per_keyword':500,'api_key':''})
    assert error.value.code=='MISSING_CREDENTIAL'


def test_cached_category_preparation_does_not_require_model_credential(tmp_path,monkeypatch):
    import generate_category
    monkeypatch.setattr(generate_category,'approved_category',lambda *args:{'verified':'fixture'})
    console=web.Console(tmp_path);monkeypatch.setattr(console,'saved_credential',lambda:None)
    monkeypatch.setattr(console,'owner',lambda:None);pending=[]
    class Thread:
        def __init__(self,target,args,daemon):pending.append(args[0])
        def start(self):pass
    monkeypatch.setattr(web.threading,'Thread',Thread)
    result=console.start({'category_name':'鼠标','amazon_count':2,'per_keyword':10,'api_key':''})
    assert result['ok'] and pending[0]['api_key']==''


def test_restored_draft_refreshes_keywords_from_saved_titles_without_model(tmp_path,monkeypatch):
    folder=tmp_path/'runtime/web-console';folder.mkdir(parents=True)
    category=folder/'category.json';amazon=folder/'amazon.json';plan=folder/'plan.json'
    web.selection.save(category,{'temu':{'base_queries':['dog bowl'],'worker_count':4,
        'keyword_limit':20,'max_products_per_keyword':500,'keyword_features':[]}})
    web.selection.save(amazon,{'records':[{'asin':'A','title':'Blue Dog Bowls 2 Cup'}]})
    web.selection.save(plan,{'keywords':[]})
    web.selection.save(folder/'resume-draft.json',{'draft':{'id':'original','plain_stage1':'keep'},
        'category_path':str(category),'amazon_path':str(amazon),'plan_path':str(plan)})
    monkeypatch.setenv('DEEPSEEK_API_KEY','private-fixture')
    console=web.Console(tmp_path)
    assert console.draft['id']=='original' and console.draft['plain_stage1']=='keep'
    assert console.draft['keyword_coverage']['specific_products']==1
    assert len(console.draft['keyword_plan'])==2
    assert any(q['amazon_products']==1 for q in console.draft['keyword_plan'] if q.get('purpose')!='CATEGORY_SAMPLE')
    assert console.draft_key=='private-fixture'
    assert 'private-fixture' not in (folder/'resume-draft.json').read_text('utf-8')


def test_live_console_port_cannot_be_shared_by_another_server():
    server=web.LocalConsoleServer(('127.0.0.1',0),web.Handler)
    try:
        with pytest.raises(OSError):web.LocalConsoleServer(server.server_address,web.Handler)
    finally:server.server_close()


def test_market_refresh_runs_python_without_paid_vision_and_exposes_status(tmp_path,monkeypatch):
    console=web.Console(tmp_path);console.key='market_fixture'
    monkeypatch.setattr(console,'owner',lambda:{'category_key':'market_fixture'})
    base=tmp_path/'runs/market_fixture';base.mkdir(parents=True)
    web.selection.save(base/'category.json',{'category_key':'market_fixture','category_name_zh':'测试'})
    calls=[];finished=threading.Event()
    def invoke(args,**kwargs):
        calls.append(args)
        if str(args[0]).endswith('collect_market_evidence.py'):
            web.selection.save(base/'market/status.json',{'status':'COMPLETE','message':'fixture complete'})
        else:finished.set()
        return SimpleNamespace(returncode=0)
    monkeypatch.setattr(console,'invoke',invoke)
    assert console.refresh_market({})['ok']
    assert finished.wait(3)
    assert [Path(c[0]).name for c in calls]==['collect_market_evidence.py','publish_all.py']
    assert console.status()['market_status']['status']=='COMPLETE'
    assert console.status()['stage3_control']['enabled'] is False


def test_market_auth_block_is_red_and_old_report_is_hidden(tmp_path,monkeypatch):
    console=web.Console(tmp_path);console.key='market_fixture'
    monkeypatch.setattr(console,'owner',lambda:None)
    base=tmp_path/'runs/market_fixture'
    web.selection.save(base/'market/status.json',{'status':'ACTION_REQUIRED','message':'请登录'})
    web.selection.save(base/'publish_state.json',{'errors':{'commercial':'fixture failure'}})
    web.selection.save(base/'commercial_report.json',{'products':[{'signals':3}]})
    state=console.status()
    assert state['severity']=='red' and state['commercial']=={}
    assert state['commercial_error']


@pytest.mark.parametrize('status', ['ACTION_REQUIRED', 'FAILED', 'CATEGORY_UNRESOLVED'])
def test_market_retry_does_not_publish_while_login_or_source_is_unavailable(tmp_path, monkeypatch, status):
    console=web.Console(tmp_path);console.key='market_fixture'
    monkeypatch.setattr(console,'owner',lambda:{'category_key':'market_fixture'})
    base=tmp_path/'runs/market_fixture'
    web.selection.save(base/'category.json',{'category_key':'market_fixture','category_name_zh':'测试'})
    calls=[]
    def invoke(args, **kwargs):
        calls.append(Path(args[0]).name)
        assert web.selection.read(base/'market/status.json')['status']=='RUNNING'
        web.selection.save(base/'market/status.json',{'status':status,'message':'fixture'})
        return SimpleNamespace(returncode=0)
    monkeypatch.setattr(console,'invoke',invoke)
    threads=[];original_thread=web.threading.Thread
    def thread(*args, **kwargs):
        item=original_thread(*args, **kwargs);threads.append(item);return item
    monkeypatch.setattr(web.threading,'Thread',thread)
    assert console.refresh_market({})['ok']
    threads[0].join(3)
    assert not threads[0].is_alive()
    assert calls==['collect_market_evidence.py']
    assert console.market_busy is False
    assert web.selection.read(base/'market/status.json')['status']==status


def test_inputs_and_duplicate_start(tmp_path,monkeypatch):
    console=web.Console(tmp_path)
    monkeypatch.setattr(console,'owner',lambda:{'category_key':'active'})
    body={'category_name':'测试','amazon_count':3,'per_keyword':5,'api_key':'private-key'}
    with pytest.raises(web.ConsoleError) as exc: console.start(body)
    assert exc.value.code=='ALREADY_RUNNING'
    for field,value in [('amazon_count',True),('per_keyword',0),('category_name',''),('api_key','')]:
        with pytest.raises(web.ConsoleError):console.start({**body,field:value})


def test_generation_then_start_key_only_in_env(tmp_path,monkeypatch):
    console=web.Console(tmp_path); calls=[]
    def invoke(args,**kwargs):
        calls.append((args,kwargs))
        if 'generate_category.py' in args[0]:
            category=json.loads((ROOT/'config/categories/power_drill.json').read_text('utf-8'))
            category['amazon']['target_count']=3
            Path(args[args.index('--output')+1]).write_text(json.dumps(category))
        if 'collect_amazon_search_frontend.mjs' in args[1]:
            category=json.loads(Path(args[args.index('--config')+1]).read_text('utf-8'))
            rows=[{'asin':f'B00000000{n}','rank':n,'title':'Cordless power drill kit'} for n in range(1,4)]
            Path(args[args.index('--output')+1]).write_text(json.dumps({'records':rows,'record_count':3,
                'source_url':category['amazon']['bestsellers_url'],'category_key':category['category_key']}))
        return SimpleNamespace(returncode=0)
    monkeypatch.setattr(console,'invoke',invoke)
    monkeypatch.setattr(console,'explain_file',lambda source,*a:source)
    console.busy=True
    monkeypatch.setattr(console,'owner',lambda:{'category_key':console.key})
    console._start({'category_name':'测试','amazon_count':3,'per_keyword':5,'api_key':'private-key'})
    assert len(calls)==3
    assert '--listing-only' not in calls[1][0]
    assert 'extract_match_criteria.py' in calls[2][0][0]
    assert all('private-key' not in json.dumps(args) for args,kw in calls)
    assert console.phase=='规则与 Amazon 关键词已生成，请编辑确认后点击运行'
    assert console.draft and console.draft_key=='private-key'
    assert console.draft['keyword_plan'][0]['amazon_products']==3
    for path in tmp_path.rglob('*'):
        if path.is_file(): assert 'private-key' not in path.read_text('utf-8')
    assert console.key is None
    assert 'private-key' not in json.dumps(console.draft)


def test_reviewed_run_keeps_its_amazon_keyword_evidence(tmp_path):
    console=web.Console(tmp_path)
    folder=tmp_path/'runtime/web-console';folder.mkdir(parents=True)
    console.draft_amazon_path=folder/'amazon.json';console.draft_plan_path=folder/'keywords.json'
    category={'category_key':'reviewed_drill','amazon':{'target_count':2}}
    amazon={'source_url':'https://www.amazon.com/bestsellers','status':'COMPLETE','category_key':'draft_drill',
            'records':[{'asin':'B000000001','rank':1,'title':'Mini drill'},
                       {'asin':'B000000002','rank':2,'title':'Portable drill'}]}
    plan={'keywords':[{'query':'mini drill','source_asins':['B000000001']},
                      {'query':'portable drill','source_asins':['B000000002']}]}
    console.draft_amazon_path.write_text(json.dumps(amazon))
    console.draft_plan_path.write_text(json.dumps(plan))
    console.seed_run_from_draft(category)
    base=tmp_path/'runs/reviewed_drill'
    assert json.loads((base/'amazon/amazon_frontend_latest.json').read_text())['category_key']=='reviewed_drill'
    checkpoint=json.loads((base/'amazon/amazon_frontend_latest.json.checkpoint.json').read_text())
    assert checkpoint['scope']['category_key']=='reviewed_drill'
    assert json.loads((base/'temu/keyword_plan.json').read_text())==plan


def test_web_run_starts_full_collector_without_paid_vision(tmp_path, monkeypatch):
    console=web.Console(tmp_path)
    folder=tmp_path/'runtime/web-console';folder.mkdir(parents=True)
    console.pending_scope={'category':'failed-new-category'};console.generation_error={'code':'FAILED'}
    web.selection.save(folder/'pending-generation.json',{'scope':console.pending_scope})
    console.draft_path=folder/'category.json'
    console.draft_key='private-key'
    console.draft_amazon_path=folder/'amazon.json'
    console.draft_plan_path=folder/'keywords.json'
    console.draft_amazon_path.write_text(json.dumps({'source_url':'https://www.amazon.com/bestsellers',
        'records':[{'asin':'B000000001','rank':1,'title':'Mini drill'}]}))
    console.draft_plan_path.write_text(json.dumps({'keywords':[{'query':'mini drill','source_asins':['B000000001']}]}))
    console.draft_path.write_text('{}')
    commands=[]
    def invoke(args,**kwargs):
        commands.append(args)
        if 'rule_editor.py' in args[0]:
            output=Path(args[args.index('--output')+1])
            output.write_text(json.dumps({'category_key':'drill','execution_mode':'collection','amazon':{'target_count':1}}))
        if 'start' in args:
            web.selection.save(tmp_path/'runtime/active.json',{'category_key':console.key,'pid':123,'created':1.0})
        return SimpleNamespace(returncode=0)
    monkeypatch.setattr(console,'invoke',invoke)
    monkeypatch.setattr(console,'owner',lambda:{'category_key':console.key,'pid':123,'created':1.0} if console.key else None)
    console._run_draft({'stage1_text':'drill','stage2_threshold':60,'stage3_text':'drill'})
    start=next(args for args in commands if 'selection.py' in args[0] and 'start' in args)
    assert '--category' in start and '--paid' not in start
    assert console.draft_used
    assert '第三层等待网页按钮' in console.phase
    run=tmp_path/'runs'/console.key
    reviewed=Path(start[start.index('--category')+1])
    assert json.loads(reviewed.read_text('utf-8'))['execution_mode']=='full'
    assert (run/'amazon/amazon_frontend_latest.json').exists()
    assert (run/'temu/keyword_plan.json').exists()
    assert not (run/'stage123/paid_enabled.json').exists()
    assert not (folder/'pending-generation.json').exists()
    assert console.pending_scope is None and console.generation_error is None
    recovered=web.Console(tmp_path)
    monkeypatch.setattr(recovered,'owner',lambda:{'category_key':console.key})
    assert recovered.current_run_key()[0]==console.key


def test_stale_running_state_red_and_actual_worker(tmp_path,monkeypatch):
    console=web.Console(tmp_path);console.key='fixture'
    monkeypatch.setattr(console,'owner',lambda:None)
    base=tmp_path/'runs/fixture';(base/'temu/supervisors').mkdir(parents=True)
    (base/'status.json').write_text(json.dumps({'状态':'运行中'}))
    (base/'temu/supervisors/worker-01.json').write_text(json.dumps({'status':'WAITING_HUMAN_VERIFICATION'}))
    result=console.status()
    assert not result['active'] and result['severity']=='red'
    assert '已退出' in result['phase']
    assert len(result['workers'])==1


def test_local_http_rejects_cross_origin_and_serves_status(tmp_path,monkeypatch):
    server=web.ThreadingHTTPServer(('127.0.0.1',0),web.Handler);server.console=web.Console(tmp_path)
    monkeypatch.setattr(server.console,'owner',lambda:None)
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    try:
        client=http.client.HTTPConnection('127.0.0.1',server.server_port)
        client.request('POST','/api/stop','{}',{'Content-Type':'application/json','Origin':'https://attacker.invalid'})
        response=client.getresponse();assert response.status==403;response.read()
        client.request('GET','/api/status');response=client.getresponse()
        assert response.status==200 and json.loads(response.read())['active'] is False
        client.close()
    finally: server.shutdown();server.server_close();thread.join()


def test_four_excel_buttons_use_current_run_and_newest_view(tmp_path,monkeypatch):
    console=web.Console(tmp_path);console.key='drill_run'
    monkeypatch.setattr(console,'owner',lambda:None)
    base=tmp_path/'runs/drill_run';excel=base/'excel';excel.mkdir(parents=True)
    (base/'category.json').write_text(json.dumps({'category_key':'drill_run','category_name_zh':'电钻'}))
    names={'amazon':'Amazon_US_电钻_Best_Sellers.xlsx',
           'temu':'TEMU_US_电钻_关键词采集明细.xlsx',
           'layer12':'电钻_第一二层.xlsx','layer123':'电钻_第一二三层.xlsx'}
    for kind,name in names.items():
        (excel/name).write_bytes(kind.encode())
    newest=excel/'TEMU_US_电钻_关键词采集明细_查看快照_20260925_100000_000001.xlsx'
    newest.write_bytes(b'newest temu')
    os.utime(excel/names['temu'],(1,1));os.utime(newest,(2,2))
    assert console.excel_workbook('temu')==newest
    assert console.excel_workbook('layer123') is None
    assert console.excel_workbook('../../runtime/secrets') is None
    assert console.status()['excel_files']['layer123']['available'] is False
    (base/'publish_state.json').write_text(json.dumps({'deliveries':{
        'layer123':{'当前查看产物已交付':True}}}))
    assert console.excel_workbook('layer123')==excel/names['layer123']
    assert console.status()['excel_files']['layer123']['available'] is True
    assert not (base/'stage123/paid_enabled.json').exists()
    marker=base/'stage123/paid_enabled.json';marker.parent.mkdir(exist_ok=True);marker.write_text('{}')
    assert console.excel_workbook('layer123')==excel/names['layer123']
    opened=[];monkeypatch.setattr(web.os,'startfile',lambda path:opened.append(path),raising=False)
    assert console.open_excel('amazon')['ok'] and len(opened)==1
    assert Path(opened[0]).read_bytes()==(excel/names['amazon']).read_bytes()
    assert Path(opened[0]).parent==tmp_path/'runtime/web-console/opened-excel'
    console.pending_scope={'category':'新任务','amazon_count':1,'per_keyword':3};console.key=None
    assert all(not row['available'] for row in console.status()['excel_files'].values())


def test_finished_run_excel_button_opens_published_file_without_refresh(tmp_path,monkeypatch):
    console=web.Console(tmp_path);console.key='drill_run'
    monkeypatch.setattr(console,'owner',lambda:None)
    base=tmp_path/'runs/drill_run';excel=base/'excel';excel.mkdir(parents=True)
    (base/'category.json').write_text(json.dumps({'category_key':'drill_run','category_name_zh':'电钻'}))
    source=base/'amazon/amazon_frontend_latest.json';source.parent.mkdir();source.write_text('{}')
    old=excel/'Amazon_US_电钻_Best_Sellers.xlsx';old.write_bytes(b'old')
    latest=excel/'Amazon_US_电钻_Best_Sellers_查看快照_20260925_180000_000001.xlsx'
    called=[]
    def publish(command,**kwargs):
        called.append(command)
        latest.write_bytes(b'new')
        return type('Result',(),{'returncode':0})()
    monkeypatch.setattr(web.subprocess,'run',publish)
    opened=[];monkeypatch.setattr(web.os,'startfile',lambda path:opened.append(path),raising=False)
    assert console.open_excel('amazon')['ok']
    assert Path(opened[0])!=old and Path(opened[0]).read_bytes()==old.read_bytes()
    assert not called


def test_excel_http_download_and_open_only_allow_current_task(tmp_path,monkeypatch):
    console=web.Console(tmp_path);console.key='drill_run'
    monkeypatch.setattr(console,'owner',lambda:None)
    base=tmp_path/'runs/drill_run';excel=base/'excel';excel.mkdir(parents=True)
    (base/'category.json').write_text(json.dumps({'category_key':'drill_run','category_name_zh':'电钻'}))
    (excel/'Amazon_US_电钻_Best_Sellers.xlsx').write_bytes(b'current workbook')
    opened=[];monkeypatch.setattr(web.os,'startfile',lambda path:opened.append(path),raising=False)
    server=web.ThreadingHTTPServer(('127.0.0.1',0),web.Handler);server.console=console
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    try:
        client=http.client.HTTPConnection('127.0.0.1',server.server_port)
        client.request('GET','/api/excel/amazon');response=client.getresponse()
        assert response.status==200 and response.read()==b'current workbook'
        assert 'spreadsheetml' in response.getheader('Content-Type')
        client.request('GET','/api/excel/layer123');response=client.getresponse()
        assert response.status==404;response.read()
        client.request('GET','/api/excel/../../runtime/secrets');response=client.getresponse()
        assert response.status==404;response.read()
        body=json.dumps({'kind':'amazon'})
        client.request('POST','/api/open-excel',body,{'Content-Type':'application/json',
            'Origin':f'http://127.0.0.1:{server.server_port}'})
        response=client.getresponse();assert response.status==200;response.read()
        assert len(opened)==1 and Path(opened[0]).read_bytes()==b'current workbook'
        assert Path(opened[0]).parent==tmp_path/'runtime/web-console/opened-excel'
        client.close()
    finally: server.shutdown();server.server_close();thread.join()


def test_cancel_generation_terminates_owned_child(tmp_path,monkeypatch):
    console=web.Console(tmp_path);console.cancel.set();child=SimpleNamespace(poll=lambda:None)
    monkeypatch.setattr(web.subprocess,'Popen',lambda *a,**k:child)
    stopped=[];monkeypatch.setattr(web.selection,'terminate_owned',lambda process:stopped.append(process))
    with pytest.raises(web.ConsoleError) as exc:console.invoke(['generate'],env={'DEEPSEEK_API_KEY':'secret'})
    assert exc.value.code=='CANCELLED' and stopped==[child]


def test_layer123_download_does_not_rebuild_published_workbook(tmp_path,monkeypatch):
    console=web.Console(tmp_path);console.key='drill_run'
    monkeypatch.setattr(console,'owner',lambda:None)
    base=tmp_path/'runs/drill_run';excel=base/'excel';excel.mkdir(parents=True)
    web.selection.save(base/'category.json',{'category_key':'drill_run','category_name_zh':'电钻'})
    (base/'stage123').mkdir()
    (base/'stage123/paid_enabled.json').write_text('{}')
    source=excel/'电钻_第一二三层.xlsx';source.write_bytes(b'published-color-workbook')
    def reject_rebuild(kind):
        raise AssertionError('Published download must not rebuild Excel')
    monkeypatch.setattr(console,'refresh_excel',reject_rebuild)
    server=web.ThreadingHTTPServer(('127.0.0.1',0),web.Handler);server.console=console
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    try:
        client=http.client.HTTPConnection('127.0.0.1',server.server_port,timeout=5)
        client.request('GET','/api/excel/layer123');response=client.getresponse()
        assert response.status==200 and response.read()==source.read_bytes()
        assert 'spreadsheetml' in response.getheader('Content-Type')
        client.close()
    finally:
        server.shutdown();server.server_close();thread.join()


def test_layer_application_failure_is_reported_per_layer_and_preserves_draft(tmp_path,monkeypatch):
    console=web.Console(tmp_path)
    console.draft={'id':'original'};console.draft_key='TEST_ONLY_KEY'
    def fail(*args,**kwargs):
        raise web.ConsoleError('PLAIN_RULES_FORMAT','应用失败：规则说明返回结构无效')
    monkeypatch.setattr(console,'explain_file',fail)
    for scope in ('stage1','stage3'):
        console._plain_rules('保留用户要求',scope)
    assert console.draft=={'id':'original'}
    assert all(console.plain_application[x]['state']=='failed' for x in ('stage1','stage3'))
    stored=web.selection.read(tmp_path/'runtime/web-console/plain-application.json')
    assert stored['draft_id']=='original'
    assert '结构无效' in stored['states']['stage3']['message']
    assert 'TEST_ONLY_KEY' not in json.dumps(stored)


def test_amazon_invocation_uses_node_not_python(tmp_path,monkeypatch):
    import selection_automation.node_runtime as runtime
    monkeypatch.setattr(runtime,'ensure_node_runtime',lambda root:pytest.fail('网页采集不得自动安装依赖'))
    console=web.Console(tmp_path);calls=[]
    child=SimpleNamespace(poll=lambda:0,returncode=0)
    monkeypatch.setattr(web.shutil,'which',lambda executable:'C:/node.exe' if executable=='node' else None)
    monkeypatch.setattr(web.subprocess,'Popen',lambda command,**kwargs:(calls.append(command) or child))
    console.invoke(['node','collector.mjs','--config','category.json'])
    assert calls==[['C:/node.exe','collector.mjs','--config','category.json']]


@pytest.mark.parametrize('message,expected',[
    ('安装网页采集浏览器未完成；请检查网络后重试，或运行 deploy.cmd。','新版已移除该步骤'),
    ('品类模型请求返回 HTTP 403；密钥权限不足或服务拒绝访问','已更新 API 客户端标识'),
    ('连接品类模型超时或网络失败；未创建任务','已改为流式接收')])
def test_legacy_failure_is_labelled_previous_attempt_and_retains_scope(tmp_path,message,expected):
    scope={'category':'键盘','amazon_count':20,'per_keyword':200,'worker_count':4,
        'api_base_url':'https://provider.example/v1','api_model':'test-model'}
    web.selection.save(tmp_path/'runtime/web-console/pending-generation.json',{
        'scope':scope,'code':'GENERATION_FAILED','message':message})
    console=web.Console(tmp_path)
    assert console.pending_scope==scope
    assert '上次' in console.phase and expected in console.phase
    assert console.generation_error['message']==console.phase and not console.busy


def test_failed_new_web_task_is_not_hidden_by_old_completion(tmp_path,monkeypatch):
    console=web.Console(tmp_path);console.key=None;console.phase='Amazon 采集失败，请检查网络后重试'
    console.pending_scope={'category':'电钻','amazon_count':1,'per_keyword':3}
    old=tmp_path/'runs/old_task';old.mkdir(parents=True)
    (old/'status.json').write_text(json.dumps({'状态':'完成'}))
    (tmp_path/'runtime').mkdir()
    (tmp_path/'runtime/last_run.json').write_text(json.dumps({'category_key':'old_task'}))
    monkeypatch.setattr(console,'owner',lambda:None)
    status=console.status()
    assert status['phase']=='Amazon 采集失败，请检查网络后重试'
    assert status['category_key'] is None and status['details']=={}
    assert status['scope']=={'category':'电钻','amazon_count':1,'per_keyword':3}


def test_progress_and_worker_view_are_safe(tmp_path,monkeypatch):
    console=web.Console(tmp_path);console.key='fixture';console.busy=True;console.phase='生成品类配置并核实榜单中'
    progress=tmp_path/'progress.json';console.progress_file=progress
    progress.write_text(json.dumps({'stage':'discovering','depth':3,'message':'核实榜单','api_key':'hidden'}))
    base=tmp_path/'runs/fixture/temu/supervisors';base.mkdir(parents=True)
    (base/'worker-01.json').write_text('{}')
    monkeypatch.setattr(console,'owner',lambda:None)
    monkeypatch.setattr(web.selection,'worker_snapshot',lambda *a:({'private':'worker'},{'状态':'验证码','api_key':'hidden'},None))
    result=console.status()
    assert '定位榜单' in result['phase'] and '第3步' in result['phase']
    assert result['workers'][0]['live_snapshot']=={'状态':'验证码'}
    assert 'hidden' not in json.dumps(result)
    assert web.clean(({'api_key':'hidden'},))==[{}]


def test_cancel_after_start_stops_new_category(tmp_path,monkeypatch):
    console=web.Console(tmp_path);calls=[]
    console.draft_path=tmp_path/'draft.json';console.draft_key='key'
    console.draft_amazon_path=tmp_path/'amazon.json';console.draft_plan_path=tmp_path/'plan.json'
    console.draft_amazon_path.write_text('{}');console.draft_plan_path.write_text('{}')
    monkeypatch.setattr(console,'seed_run_from_draft',lambda category:None)
    def invoke(args,**kwargs):
        calls.append(args)
        if 'rule_editor.py' in args[0]:
            Path(args[args.index('--output')+1]).write_text(json.dumps({'category_key':'fixture'}))
        if 'start' in args:console.cancel.set()
        return SimpleNamespace(returncode=0)
    monkeypatch.setattr(console,'invoke',invoke);monkeypatch.setattr(console,'owner',lambda:None)
    console._run_draft({})
    assert calls[-1][-3:]==['stop','--category-key',console.key]
    assert console.phase=='已停止'
    assert '--paid' not in next(args for args in calls if 'start' in args)


def test_web_key_registration_waits_for_actual_supervisor(tmp_path,monkeypatch):
    console=web.Console(tmp_path)
    launcher={'category_key':'live_run','pid':123,'created':1.0}
    supervisor={'category_key':'live_run','pid':456,'created':2.0}
    web.selection.save(tmp_path/'runtime/active.json',supervisor)
    observations=iter([launcher,supervisor])
    monkeypatch.setattr(console,'owner',lambda:next(observations))
    monkeypatch.setattr(web.time,'sleep',lambda seconds:None)
    assert console.registered_owner('live_run')==supervisor


def test_empty_visual_candidates_never_claim_paid_analysis_complete(tmp_path,monkeypatch):
    console=web.Console(tmp_path);console.key='finished_run';console.draft_key='private-key'
    monkeypatch.setattr(console,'stage3_snapshot',lambda key:{'total':0,'pending':0,'pending_after_codex':0})
    base=tmp_path/'runs/finished_run';(base/'stage12').mkdir(parents=True)
    web.selection.save(base/'category.json',{'stage3':{'base_url':'https://api.deepseek.com','model':'deepseek-flash'},'category_key':'finished_run'})
    web.selection.save(base/'stage12_config.json',{'output_dir':str(base/'stage12')})
    (base/'stage12/layer3_candidates_finished_run.jsonl').write_text('\n')
    monkeypatch.setattr(console,'owner',lambda:None)
    monkeypatch.setattr(console,'invoke',lambda *a,**k:pytest.fail('No paid process without candidates'))
    launched=[]
    monkeypatch.setattr(console,'launch_stage3_controller',lambda *args:launched.append(args))
    result=console.start_stage3({})
    assert result['ok'] and len(launched)==1
    assert console.stage3_busy
    assert (base/'stage123/paid_enabled.json').exists()


@pytest.mark.parametrize('source',['environment','saved'])
def test_completed_stage3_uses_configured_key_without_paid_request(tmp_path,monkeypatch,source):
    console=web.Console(tmp_path);console.key='finished_run'
    monkeypatch.setattr(console,'stage3_snapshot',lambda key:{'total':0,'pending':0,'pending_after_codex':0})
    base=tmp_path/'runs/finished_run';(base/'stage12').mkdir(parents=True)
    web.selection.save(base/'category.json',{'stage3':{'base_url':'https://api.deepseek.com','model':'deepseek-flash'},'category_key':'finished_run'})
    (base/'stage12/layer3_candidates_finished_run.jsonl').write_text('{}\n')
    monkeypatch.setattr(console,'owner',lambda:None)
    if source=='environment':monkeypatch.setenv('DEEPSEEK_API_KEY','environment-test-only')
    else:
        monkeypatch.delenv('DEEPSEEK_API_KEY',raising=False)
        from selection_automation import secrets
        monkeypatch.setattr(secrets,'load',lambda path:{'key':'saved-test-only'})
    pending=[]
    monkeypatch.setattr(console,'launch_stage3_controller',lambda *args:pending.append(args))
    before=console.status()
    assert before['saved_credential_available'] and before['stage3_control']['can_start']
    result=console.start_stage3({})
    assert result['ok'] and len(pending)==1
    assert pending[0][-1]==source+'-test-only'
    assert source+'-test-only' not in json.dumps([before,result])


def test_repeated_excel_open_uses_independent_copies(tmp_path,monkeypatch):
    console=web.Console(tmp_path)
    source=tmp_path/'runs/fixture/excel/产品_第一二三层_查看快照_20261005.xlsx'
    source.parent.mkdir(parents=True)
    source.write_bytes(b'first-published-workbook')
    monkeypatch.setattr(console,'refresh_excel',lambda kind:None)
    monkeypatch.setattr(console,'excel_workbook',lambda kind:source)
    opened=[]
    monkeypatch.setattr(web.os,'startfile',lambda path:opened.append(Path(path)),raising=False)
    assert console.open_excel('layer123')['filename']==source.name
    source.write_bytes(b'updated-published-workbook')
    console.open_excel('layer123')
    assert len(set(opened))==2 and source not in opened
    assert opened[0].read_bytes()==b'first-published-workbook'
    assert opened[1].read_bytes()==source.read_bytes()
    assert all(path.parent==tmp_path/'runtime/web-console/opened-excel' for path in opened)


def test_stage3_button_authorizes_only_current_active_run(tmp_path,monkeypatch):
    console=web.Console(tmp_path);console.key='old_web_run'
    monkeypatch.setattr(console,'stage3_snapshot',lambda key:{'total':0,'pending':0,'pending_after_codex':0})
    base=tmp_path/'runs/live_run';base.mkdir(parents=True)
    (base/'category.json').write_text(json.dumps({'stage3':{'base_url':'https://api.deepseek.com','model':'deepseek-flash'},'category_key':'live_run','category_name_zh':'投影仪',
        'amazon':{'target_count':10},'temu':{'max_products_per_keyword':100}}))
    (base/'stage123').mkdir()
    (base/'stage123/web_key_inherited.json').write_text(json.dumps({'pid':123,'created':1.0}))
    folder=tmp_path/'runtime';folder.mkdir()
    (folder/'last_run.json').write_text(json.dumps({'category_key':'other_run'}))
    monkeypatch.setattr(console,'owner',lambda:{'stage3':{'base_url':'https://api.deepseek.com','model':'deepseek-flash'},'category_key':'live_run','pid':123,'created':1.0})
    monkeypatch.setattr(console,'saved_credential',lambda:'test-only-key')
    monkeypatch.setattr(console,'launch_stage3_controller',lambda *args:None)
    result=console.start_stage3({})
    assert result['ok']
    marker=json.loads((base/'stage123/paid_enabled.json').read_text())
    assert marker['category_key']=='live_run' and marker['source']=='web-console'
    assert not (tmp_path/'runs/old_web_run/stage123/paid_enabled.json').exists()
    assert console.start_stage3({})['ok']
    status=console.status()
    assert status['category_key']=='live_run' and status['scope']=={
        'category':'投影仪','amazon_count':10,'per_keyword':100,'worker_count':4,'execution_mode':'full'}
    assert status['stage3_control']['enabled']


def test_stage3_can_start_alongside_cli_run_without_inherited_key(tmp_path,monkeypatch):
    console=web.Console(tmp_path)
    monkeypatch.setattr(console,'stage3_snapshot',lambda key:{'total':0,'pending':0,'pending_after_codex':0})
    base=tmp_path/'runs/cli_run';base.mkdir(parents=True)
    web.selection.save(base/'category.json',{'category_key':'cli_run'})
    monkeypatch.setattr(console,'owner',lambda:{'category_key':'cli_run','pid':456,'created':2.0})
    starts=[]
    monkeypatch.setattr(console,'launch_stage3_controller',lambda *args:starts.append(args))
    assert console.start_stage3({'api_key':'test-only-key'})['ok']
    assert len(starts)==1
    assert web.selection.read(base/'stage123/paid_enabled.json')['controller']=='independent'
    assert console.status()['stage3_control']['can_start']
    assert 'test-only-key' not in (base/'stage123/paid_enabled.json').read_text()


def test_completed_run_stage3_requires_key_and_consumes_candidates_once(tmp_path,monkeypatch):
    monkeypatch.delenv('DEEPSEEK_API_KEY',raising=False)
    console=web.Console(tmp_path);console.key='old_web_run'
    monkeypatch.setattr(console,'stage3_snapshot',lambda key:{'total':0,'pending':0,'pending_after_codex':0})
    base=tmp_path/'runs/finished_run';(base/'stage12').mkdir(parents=True)
    (base/'category.json').write_text(json.dumps({'category_key':'finished_run'}))
    (base/'stage12_config.json').write_text(json.dumps({'output_dir':str(base/'stage12')}))
    (base/'stage12/layer3_candidates_finished_run.jsonl').write_text('{}\n')
    folder=tmp_path/'runtime';folder.mkdir()
    (folder/'last_run.json').write_text(json.dumps({'category_key':'finished_run'}))
    monkeypatch.setattr(console,'owner',lambda:None)
    console.restore_history({'category_key':'finished_run'})
    with pytest.raises(web.ConsoleError) as exc:console.start_stage3({})
    assert exc.value.code=='KEY_REQUIRED'
    assert not (base/'stage123/paid_enabled.json').exists()
    calls=[]
    monkeypatch.setattr(console,'launch_stage3_controller',lambda *args:calls.append(args))
    assert console.start_stage3({'api_key':'test-only-key'})['ok']
    assert console.stage3_busy and len(calls)==1
    assert console.start_stage3({'api_key':'test-only-key'})['ok'] and len(calls)==1
    assert console.stop_stage3({})['ok']
    assert (base/'stage123/STOP').exists()
    assert (base/'stage123/paid_enabled.json').exists()
    assert 'test-only-key' not in (base/'stage123/paid_enabled.json').read_text()
    assert console.status()['category_key']=='finished_run'


def test_normal_worker_flags_not_red_and_identity_whitelisted(tmp_path,monkeypatch):
    console=web.Console(tmp_path);console.key='fixture'
    monkeypatch.setattr(console,'owner',lambda:{'category_key':'fixture'})
    base=tmp_path/'runs/fixture';(base/'temu/supervisors').mkdir(parents=True)
    (base/'status.json').write_text(json.dumps({'状态':'运行中'}))
    (base/'temu/supervisors/worker-01-new.json').write_text(json.dumps({'status':'RUNNING'}))
    (tmp_path/'config').mkdir()
    (tmp_path/'config/sessions.json').write_text(json.dumps({'workers':[{'name':'worker-01-new','profile_id':'real-profile','serial_number':'12','proxy_user':'hidden-user'}]}))
    monkeypatch.setattr(web.selection,'worker_snapshot',lambda *args:({}, {'状态':'运行中','阻断':False,'需人工介入':False},None))
    result=console.status()
    assert result['severity']=='green'
    assert result['workers'][0]['profile_id']=='real-profile'
    assert result['workers'][0]['serial_number']=='12'
    assert 'hidden-user' not in json.dumps(result)
    monkeypatch.setattr(web.selection,'worker_snapshot',lambda *args:({}, {'状态':'等待首屏商品','阻断':True,'需人工介入':False,'采集状态':'WAITING_INITIAL_PRODUCTS'},None))
    assert console.status()['severity']=='green'
    monkeypatch.setattr(web.selection,'worker_snapshot',lambda *args:({}, {'状态':'等待验证码','需人工介入':True},None))
    assert console.status()['severity']=='red'


def test_restore_draft_has_no_key_and_plain_failure_preserves(tmp_path,monkeypatch):
    folder=tmp_path/'runtime/web-console';folder.mkdir(parents=True)
    cat=folder/'category.json';cat.write_text('{}')
    draft={'id':'saved','stage1_text':'原规则'}
    (folder/'resume-draft.json').write_text(json.dumps({'draft':draft,'category_path':str(cat)}))
    console=web.Console(tmp_path)
    assert console.draft==draft and console.draft_key is None
    def fail(*a):raise web.ConsoleError('UNSUPPORTED','当前规则无法实现')
    monkeypatch.setattr(console,'explain_file',fail)
    console.draft_key='private';console._plain_rules('要求')
    assert console.draft==draft and console.draft_path==cat
    assert 'private' not in (folder/'resume-draft.json').read_text()



def test_generation_error_red_and_retry_scope_survives_restart(tmp_path,monkeypatch):
    console=web.Console(tmp_path)
    monkeypatch.setattr(console,'owner',lambda:None)
    console.pending_scope={'category':'遥控器','amazon_count':100,'per_keyword':500,'worker_count':12}
    console.key=None
    console.phase='榜单定位重复，未找到准确品类'
    console.progress_file=tmp_path/'progress.json'
    web.selection.save(console.progress_file,{'stage':'error','message':console.phase})
    state=console.status()
    assert state['severity']=='red' and state['can_retry_generation']
    assert state['workers']==[] and not state['active']
    web.selection.save(tmp_path/'runtime/web-console/pending-generation.json',{'scope':console.pending_scope,'message':console.phase})
    restored=web.Console(tmp_path)
    monkeypatch.setattr(restored,'owner',lambda:None)
    assert restored.status()['scope']['category']=='遥控器'
    assert restored.status()['can_retry_generation']


def test_manual_listing_is_validated_and_forwarded_without_stale_error(tmp_path,monkeypatch):
    console=web.Console(tmp_path)
    monkeypatch.setattr(console,'owner',lambda:None)
    pending=[]
    class Thread:
        def __init__(self,target,args,daemon): pending.append(args[0])
        def start(self): pass
    monkeypatch.setattr(web.threading,'Thread',Thread)
    body={'category_name':'遥控器','amazon_count':100,'per_keyword':500,'worker_count':12,'api_key':'secret',
          'bestsellers_url':'https://evil.com/zgbs/electronics/123'}
    with pytest.raises(web.ConsoleError) as exc: console.start(body)
    assert exc.value.code=='INVALID_LISTING_URL' and not pending
    console.generation_error={'code':'OLD','message':'old'}
    body['bestsellers_url']='https://www.amazon.com/zgbs/electronics/123'
    console.start(body)
    assert pending[0]['bestsellers_url']==body['bestsellers_url']
    assert console.status()['generation_error'] is None
    assert not console.status()['can_retry_generation']
    assert 'secret' not in (tmp_path/'runtime/web-console/pending-generation.json').read_text('utf-8')


def test_preparing_environment_does_not_report_previous_browser_captcha(tmp_path,monkeypatch):
    console=web.Console(tmp_path);console.key='fixture'
    base=tmp_path/'runs/fixture';(base/'temu/supervisors').mkdir(parents=True)
    (base/'category.json').write_text(json.dumps({'category_key':'fixture','category_name_zh':'fixture'}))
    (base/'status.json').write_text(json.dumps({'状态':'启动中','阶段':{'bootstrap':{'状态':'运行中'}}}))
    (base/'temu/supervisors/worker-01-new.json').write_text(json.dumps({'worker':'worker-01-new','status':'HUMAN_ACTION_REQUIRED','manual_action_required':True}))
    monkeypatch.setattr(console,'owner',lambda:{'category_key':'fixture'})
    status=console.status()
    assert status['workers']==[]
    assert status['phase']=='启动中'
    assert status['severity']=='green'
    assert not status['can_retry_preparation']


def test_developer_routes_export_only_selected_run_without_starting_jobs(tmp_path,monkeypatch):
    from selection_automation import developer_handoff
    console=web.Console(tmp_path);console.key='fixture'
    monkeypatch.setattr(console,'owner',lambda:None)
    base=tmp_path/'runs/fixture';base.mkdir(parents=True)
    (base/'category.json').write_text(json.dumps({'category_key':'fixture'}))
    calls=[]
    monkeypatch.setattr(developer_handoff,'describe_run',lambda root,key:{'run_key':key,'collection':{},'counts':{'amazon':1,'temu':2}})
    monkeypatch.setattr(developer_handoff,'build_bundle',lambda root,key:calls.append(key) or b'PK-fixture')
    monkeypatch.setattr(console,'invoke',lambda *a,**k:pytest.fail('Export must not start programs'))
    server=web.ThreadingHTTPServer(('127.0.0.1',0),web.Handler);server.console=console
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    try:
        client=http.client.HTTPConnection('127.0.0.1',server.server_port)
        client.request('GET','/api/developer');response=client.getresponse();data=json.loads(response.read())
        assert response.status==200 and data['run_key']=='fixture' and not data['collection']['is_collecting']
        client.request('GET','/api/developer/fixture/bundle');response=client.getresponse()
        assert response.status==200 and response.read()==b'PK-fixture'
        assert response.getheader('Content-Type')=='application/zip'
        assert calls==['fixture']
        for url in ['/api/developer/../bundle','/api/developer/%2e%2e/bundle','/api/developer/missing/bundle']:
            client.request('GET',url);response=client.getresponse();assert response.status==404;response.read()
        assert calls==['fixture']
        client.close()
    finally:server.shutdown();server.server_close();thread.join()


def test_finished_codex_reviews_do_not_reauthorize_paid_queue(tmp_path,monkeypatch):
    console=web.Console(tmp_path);console.key='fixture'
    base=tmp_path/'runs/fixture';base.mkdir(parents=True)
    web.selection.save(base/'category.json',{'category_key':'fixture'})
    monkeypatch.setattr(console,'owner',lambda:None)
    monkeypatch.setattr(console,'stage3_snapshot',lambda key:{'total':10855,'completed':10855,'api_completed':5976,'codex_completed':4879,'pending_after_codex':0,'pending':0})
    calls=[]
    monkeypatch.setattr(console,'launch_stage3_controller',lambda *args:calls.append(args))
    assert console.status()['stage3_control']['can_start'] is False
    with pytest.raises(web.ConsoleError) as exc:console.start_stage3({'api_key':'test-only-key'})
    assert exc.value.code=='NO_PENDING_CANDIDATES'
    assert not calls and not (base/'stage123/paid_enabled.json').exists()
    monkeypatch.setattr(console,'owner',lambda:{'category_key':'fixture'})
    assert console.status()['stage3_control']['can_start'] is True


@pytest.mark.parametrize('owner_active',[False,True])
@pytest.mark.parametrize('pending_progress',[{'counting':True},{'total':5,'completed':5,'refreshing':True}])
def test_stage3_cold_progress_blocks_paid_start_until_known(tmp_path,monkeypatch,owner_active,pending_progress):
    console=web.Console(tmp_path);console.key='fixture'
    web.selection.save(tmp_path/'runs/fixture/category.json',{'category_key':'fixture'})
    monkeypatch.setattr(console,'owner',lambda:{'category_key':'fixture'} if owner_active else None)
    monkeypatch.setattr(console,'stage3_snapshot',lambda key:pending_progress)
    monkeypatch.setattr(console,'launch_stage3_controller',lambda *args:pytest.fail('Cold progress must not launch paid controller'))
    assert not console.status()['stage3_control']['can_start']
    with pytest.raises(web.ConsoleError) as exc:console.start_stage3({'api_key':'test-only-key'})
    assert exc.value.code=='PROGRESS_NOT_READY'
    assert not (tmp_path/'runs/fixture/stage123/paid_enabled.json').exists()
    monkeypatch.setattr(console,'stage3_snapshot',lambda key:{'total':0,'pending':0})
    assert console.status()['stage3_control']['can_start']
