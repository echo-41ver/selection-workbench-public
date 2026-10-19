"""Offline supervision tests: no subprocesses, browsers or network requests."""
import json
import os
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import pipeline
import selection


def write(path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding='utf-8')


def worker_fixture(base, status, *, worker_status='RUNNING', **progress_fields):
    output = base / 'temu' / 'workers' / 'worker1' / 'power-drill'
    worker = {'status': worker_status, 'current_query': 'power drill', 'output': str(output),
              'completed_queries': ['corded drill'], 'query_status': status}
    write(base / 'temu' / 'supervisors' / 'worker1.json', worker)
    write(output / 'progress.json', {'status': status, **progress_fields})
    return worker, output


@pytest.mark.parametrize('status', ['STOPPED_TRY_AGAIN_STALLED',
    'STOPPED_PROXY_OR_NETWORK_FAILURE', 'STOPPED_HTTP_429', 'STOPPED_LOGIN_REQUIRED'])
def test_stopped_recovery_browser_keeps_network_until_collector_or_stop(tmp_path, monkeypatch, status):
    from selection_automation.proxy_activity import ProxyActivityLease, revoke_worker_activity
    monkeypatch.setattr(selection, 'ROOT', tmp_path)
    worker_fixture(tmp_path, status, worker_status='HUMAN_ACTION_REQUIRED')
    leases = {}
    session = {'name': 'worker1'}
    selection.sync_manual_proxy(tmp_path, session, 1, {}, leases)
    lease = leases['worker1']
    assert isinstance(lease, ProxyActivityLease) and lease.path.exists()
    try:
        selection.sync_manual_proxy(tmp_path, session, 1, {}, leases)
        assert leases['worker1'] is lease
        revoke_worker_activity(tmp_path, 1)
        assert not lease.path.exists()
        selection.sync_manual_proxy(tmp_path, session, 1, {}, leases)
        assert leases['worker1'] is not lease and leases['worker1'].path.exists()
        lease = leases['worker1']
        selection.sync_manual_proxy(tmp_path, session, 1,
            {'worker1': SimpleNamespace(poll=lambda: None)}, leases)
        assert leases == {} and not lease.path.exists()
    finally:
        for retained in leases.values():
            retained.__exit__(None, None, None)


@pytest.mark.parametrize('status', ['WAITING_CAPTCHA', 'WAITING_SEARCH_VERIFICATION', 'WAITING_LOGIN'])
def test_live_verification_is_visible_without_killing_or_restarting(tmp_path, status):
    worker_fixture(tmp_path, status, unique_goods=37)
    child = SimpleNamespace(pid=123, poll=lambda: None)
    children = {'worker1': child}; finished = {'worker1'}; state = {}
    def no_verify(*args):
        pytest.fail('A live collector must handle its own manual verification')
    assert selection.hold_blocked_worker(tmp_path, {'name': 'worker1'}, children=children,
        finished=finished, attempts={}, next_at={}, state=state, verify=no_verify)
    assert children == {'worker1': child}
    assert not finished
    assert state['worker1']['采集状态'] == status
    assert state['worker1']['唯一商品数'] == 37
    assert state['worker1']['已完成关键词'] == ['corded drill']
    summary = selection.supervision_status(state)
    assert summary['状态'] == '等待人工介入'
    assert summary['阻断阶段'] == summary['人工介入阶段'] == ['worker1']


def test_first_screen_wait_is_not_reported_as_zero_or_complete(tmp_path):
    worker_fixture(tmp_path, 'WAITING_INITIAL_PRODUCTS')
    _, view, recovery = selection.worker_snapshot(tmp_path, 'worker1')
    assert '首屏' in view['状态']
    assert '唯一商品数' not in view
    assert recovery is None
    assert selection.supervision_status({'worker1': view})['阻断阶段'] == ['worker1']


def test_recovered_worker_clears_previous_verification_advice(tmp_path):
    _, output = worker_fixture(tmp_path, 'WAITING_CAPTCHA', unique_goods=37)
    _, previous, recovery = selection.worker_snapshot(tmp_path, 'worker1')
    assert recovery and previous['建议动作']
    write(output/'progress.json', {'status':'RUNNING', 'unique_goods':77})
    _, current, recovery = selection.worker_snapshot(tmp_path, 'worker1')
    merged = {**previous, **current}
    assert recovery is None
    assert merged['唯一商品数'] == 77
    assert merged['建议动作'] == ''
    assert merged['浏览器保留'] is False
    assert merged['需人工介入'] is False
    assert selection.supervision_status({'worker1':merged})['人工介入阶段'] == []
    write(output/'progress.json', {'status':'WAITING_CAPTCHA', 'unique_goods':77})
    _, current, recovery = selection.worker_snapshot(tmp_path, 'worker1')
    assert recovery and current['需人工介入'] and current['建议动作']


def test_automatic_replacement_does_not_request_manual_login(tmp_path):
    worker_fixture(tmp_path,'STOPPED_LOGIN_REQUIRED',worker_status='REPLACING_LOGIN_ENVIRONMENT')
    _,view,recovery=selection.worker_snapshot(tmp_path,'worker1')
    assert recovery is None and not view['需人工介入']
    assert view['采集状态']=='REPLACING_LOGIN_ENVIRONMENT'
    assert '更换环境' in view['状态'] and view['建议动作']==''


def test_network_terminal_holds_zero_exit_until_page_recovers(tmp_path):
    _, output = worker_fixture(tmp_path, 'STOPPED_PROXY_OR_NETWORK_FAILURE', worker_status='HUMAN_ACTION_REQUIRED')
    children = {'worker1': SimpleNamespace(pid=123, poll=lambda: 0)}
    finished = {'worker1'}; state = {}
    assert selection.hold_blocked_worker(tmp_path, {'name': 'worker1'}, children=children,
        finished=finished, attempts={}, next_at={}, state=state,
        verify=lambda *args: False)
    assert not children and not finished
    assert state['worker1']['输出目录'] == str(output)
    assert '自动检查' in state['worker1']['建议动作']
    worker = selection.read(tmp_path/'temu'/'supervisors'/'worker1.json')
    write(tmp_path/'temu'/'supervisors'/'worker1.json', {**worker, 'status':'SESSION_REPLACED'})
    assert not selection.hold_blocked_worker(tmp_path, {'name':'worker1'}, children=children,
        finished=finished, attempts={}, next_at={}, state=state)
    current, view, recovery = selection.worker_snapshot(tmp_path, 'worker1')
    assert current['completed_queries'] == ['corded drill']
    assert current['output'] == str(output)
    assert not recovery and not view['阻断']


@pytest.mark.parametrize('status', ['STOPPED_INITIAL_PRODUCTS_TIMEOUT',
    'STOPPED_PROXY_OR_NETWORK_FAILURE','STOPPED_TRY_AGAIN_STALLED'])
def test_stopped_worker_resumes_only_after_positive_current_query_check(tmp_path, status):
    worker_fixture(tmp_path, status, worker_status='HUMAN_ACTION_REQUIRED')
    children = {}; finished = {'worker1'}; attempts = {'worker1': 2}; next_at = {'worker1': 999}; state = {}
    seen = []
    def verify(session, query):
        seen.append((session['name'], query))
        return len(seen) > 1
    args = dict(children=children, finished=finished, attempts=attempts, next_at=next_at, state=state, verify=verify)
    if status=='STOPPED_INITIAL_PRODUCTS_TIMEOUT':
        assert not selection.hold_blocked_worker(tmp_path, {'name':'worker1'}, **args)
        assert seen==[]  # Same-profile retry does not need a DOM verification.
        current=selection.read(tmp_path/'temu/supervisors/worker1.json')
        assert current['status']=='RESUMING' and current['automatic_resume_pending']
        assert current['completed_queries']==['corded drill']
        assert attempts['worker1']==next_at['worker1']==0
        return
    assert selection.hold_blocked_worker(tmp_path, {'name':'worker1'}, **args)
    assert not selection.hold_blocked_worker(tmp_path, {'name':'worker1'}, **args)
    assert seen == [('worker1', 'power drill')] * 2
    current, view, recovery = selection.worker_snapshot(tmp_path, 'worker1')
    assert current['status'] == 'RESUMING'
    assert current['completed_queries'] == ['corded drill']
    assert attempts['worker1'] == next_at['worker1'] == 0
    assert not recovery and not view['阻断']


@pytest.mark.parametrize('status,http_block', [
    ('STOPPED_HTTP_429', {}), ('STOPPED_HTTP_403', {}),
    ('STOPPED_SALES_SORT_UNCONFIRMED', {'status':429}),
    ('STOPPED_PROXY_OR_NETWORK_FAILURE', {'status':403}),
])
def test_rejected_search_resumes_for_session_replacement_without_using_old_cards(tmp_path, status, http_block):
    worker, _ = worker_fixture(tmp_path,status,worker_status='HUMAN_ACTION_REQUIRED',
        unique_goods=325,http_block=http_block)
    def old_cards_are_visible(*args):
        pytest.fail('DOM-only verification must not restart a rejected search')
    attempts={'worker1':2};next_at={'worker1':999}
    assert not selection.hold_blocked_worker(tmp_path,{'name':'worker1'},children={},
        finished=set(),attempts=attempts,next_at=next_at,state={},verify=old_cards_are_visible)
    current=selection.read(tmp_path/'temu/supervisors/worker1.json')
    assert current['status']=='RESUMING' and current['manual_action_required'] is False
    assert current['output']==worker['output'] and current['completed_queries']==worker['completed_queries']
    assert attempts=={'worker1':0} and next_at=={'worker1':0}


def test_completed_human_verification_can_resume_despite_previous_http_signal(tmp_path):
    worker_fixture(tmp_path,'WAITING_CAPTCHA',worker_status='HUMAN_ACTION_REQUIRED',
        http_block={'status':429,'document_endpoint':'https://www.temu.com/bgn_verification.html'})
    assert not selection.hold_blocked_worker(tmp_path,{'name':'worker1'},children={},
        finished=set(),attempts={},next_at={},state={},verify=lambda *args:True)
    assert selection.read(tmp_path/'temu/supervisors/worker1.json')['status']=='RESUMING'


def test_recovered_sort_page_resumes_for_collector_sort_confirmation(tmp_path):
    worker_fixture(tmp_path,'STOPPED_SALES_SORT_UNCONFIRMED',worker_status='HUMAN_ACTION_REQUIRED')
    assert not selection.hold_blocked_worker(tmp_path,{'name':'worker1'},children={},
        finished=set(),attempts={},next_at={},state={},verify=lambda *args:True)
    assert selection.read(tmp_path/'temu/supervisors/worker1.json')['status']=='RESUMING'


def test_new_collector_does_not_inherit_previous_run_terminal(tmp_path):
    worker, output = worker_fixture(tmp_path, 'STOPPED_INITIAL_PRODUCTS_TIMEOUT')
    os.utime(output/'progress.json', (100, 100))
    worker.pop('query_status')
    write(tmp_path/'temu'/'supervisors'/'worker1.json', {**worker, 'collector_started_at':200})
    _, view, recovery = selection.worker_snapshot(tmp_path, 'worker1')
    assert not recovery and view['采集状态'] == 'RUNNING'
    write(output/'progress.json', {'status':'WAITING_CAPTCHA'})
    _, view, recovery = selection.worker_snapshot(tmp_path, 'worker1')
    assert recovery and view['采集状态'] == 'WAITING_CAPTCHA'


@pytest.mark.parametrize('terminal', ['STOPPED_INITIAL_PRODUCTS_TIMEOUT'])
def test_pipeline_initial_timeout_retries_same_profile_and_preserves_recent_checkpoint(tmp_path, monkeypatch, terminal):
    monkeypatch.setattr(pipeline, 'ROOT', tmp_path)
    category = {'category_key':'fixture'}
    category_path = tmp_path/'category.json'
    sessions_path = tmp_path/'sessions.json'
    write(sessions_path, {'workers':[{'name':'worker1','webdriver_url':'http://127.0.0.1:4444',
        'session_id_file':'sid.txt','bidi_url_file':'bidi.txt'}]})
    write(tmp_path/'config'/'proxy_policy.json', {'session_rotation_policy':'NEW_ADSPOWER_PROFILE_AND_NEW_PROXY_SESSION',
        'delete_old_profile_first_via_adspower':True,'worker_chain_ports':[19081]})
    write(pipeline.keyword_plan_path(category), {'keywords':[
        {'query':'corded drill','max_products':100}, {'query':'power drill','max_products':100},
        {'query':'another drill','max_products':100}]})
    prior = pipeline.run_root(category)/'temu'/'workers'/'worker1'/'corded-drill'/'progress.json'
    evidence={'requested':'sales','state':'UI_CONFIRMED','query':'corded drill'}
    write(prior, {'status':'KEYWORD_LIMIT_REACHED','unique_goods':100,'sales_sort':evidence})
    (prior.parent/'products.jsonl').write_text(''.join(json.dumps({'goods_id':str(i),'search_sort':evidence})+'\n' for i in range(100)),encoding='utf-8')
    class Connection:
        def __enter__(self): return self
        def __exit__(self, *args): pass
    monkeypatch.setattr(pipeline.socket, 'create_connection', lambda *args, **kwargs: Connection())
    exhausted=prior.parent.parent/'corded-drill--recent/automatic_recovery.json'
    write(exhausted,{'attempts_without_progress':3,'unique_goods_before':17})
    # The interrupted recent pass is owned and started in the durable dispatcher.
    # Without this state, unstarted sales passes are correctly claimed first.
    from selection_automation import work_dispatch
    base=pipeline.run_root(category)
    plan=json.loads(pipeline.keyword_plan_path(category).read_text())
    dispatch=work_dispatch.sync(base,plan['keywords'],['worker1'])
    job=next(row for row in dispatch['jobs'] if row['query']=='corded drill')
    job['sort_passes']['sales']['complete']=True
    job['sort_passes']['recent'].update(owner='worker1',started=True)
    work_dispatch.save(base,dispatch)
    calls = []
    def run(command, **kwargs):
        calls.append(command)
        output = Path(command[command.index('--output')+1])
        write(output/'progress.json', {'status':terminal if len(calls)<=5 else 'WAITING_CAPTCHA','unique_goods':17})
        return 0
    monkeypatch.setattr(pipeline, 'run_collector', run)
    rotations=[]
    monkeypatch.setattr(pipeline, 'interruptible_wait', lambda seconds,lease:lease.check_stop())
    monkeypatch.setattr(pipeline.subprocess, 'run', lambda command,**kw:rotations.append(command) or SimpleNamespace(returncode=0))
    with pytest.raises(RuntimeError, match='incomplete'):
        pipeline.collect_temu(category_path, category, sessions_path, 'worker1')
    assert len(calls) == 6 and rotations==[]
    worker = selection.read(pipeline.run_root(category)/'temu'/'supervisors'/'worker1.json')
    assert worker['status'] == 'HUMAN_ACTION_REQUIRED'
    assert worker['query_status'] == 'WAITING_CAPTCHA'
    assert not worker.get('recovery_budget_exhausted', False)
    assert worker['current_query'] == 'corded drill'
    assert worker['current_sort'] == 'recent'
    assert worker['completed_queries'] == []
    assert worker['completed_passes'] == [{'query':'corded drill','sort':'sales'}]
    assert worker['unique_goods'] == 17
    assert len({tuple(command) for command in calls}) == 1
    assert Path(worker['output']).name == 'corded-drill--recent'
    assert worker['browser_preserved'] and worker['manual_action_required']


def test_uncertain_browser_command_remains_paused_without_inspection_or_completion(tmp_path,monkeypatch):
    from selection_automation.recovery_policy import recovery_kind
    monkeypatch.setattr(selection,'ROOT',tmp_path)
    status='STOPPED_BROWSER_COMMAND_TIMEOUT'
    progress={'unique_goods':289,'round':7,'command_execution_unknown':True,
              'manual_action_required':True,'automatic_retry':False,
              'automatic_recovery_eligible':False,'resume_requires_session_rotation':False}
    worker,output=worker_fixture(tmp_path,status,worker_status='HUMAN_ACTION_REQUIRED',**progress)
    checkpoint=output/'products.jsonl';checkpoint.write_text('{"goods_id":"retained"}\n',encoding='utf-8')
    saved=checkpoint.read_bytes()
    _,view,recovery=selection.worker_snapshot(tmp_path,'worker1')
    assert '浏览器控制超时' in view['状态'] and '代理保留' in view['状态']
    assert '限流' not in view['状态'] and '代理失败' not in view['状态']
    assert view['唯一商品数']==289 and view['采集轮次']==7
    assert view['浏览器保留'] and view['阻断'] and view['需人工介入']
    assert recovery['verify_to_resume'] is False
    assert recovery_kind(status,{'status':status,**progress},True) is None
    def no_action(*args):pytest.fail('Uncertain commands must not be inspected or automatically resent')
    finished={'worker1'};state={};children={'worker1':SimpleNamespace(pid=123,poll=lambda:0)}
    assert selection.hold_blocked_worker(tmp_path,{'name':'worker1'},children=children,
        finished=finished,attempts={},next_at={},state=state,verify=no_action,bind_launch=no_action)
    assert not finished and checkpoint.read_bytes()==saved
    assert state['worker1']['采集状态']==status
    assert state['worker1']['已完成关键词']==['corded drill']
    assert selection.supervision_status(state)['状态']=='等待人工介入'


@pytest.mark.parametrize('command_finished,page_ready,resumed',[(False,True,False),(True,False,False),(True,True,True)])
def test_command_timeout_resumes_only_after_read_fence_and_current_page_ready(tmp_path,monkeypatch,command_finished,page_ready,resumed):
    monkeypatch.setattr(selection,'ROOT',tmp_path)
    worker,output=worker_fixture(tmp_path,'STOPPED_BROWSER_COMMAND_TIMEOUT',worker_status='HUMAN_ACTION_REQUIRED',unique_goods=1155)
    checkpoint=output/'products.jsonl';checkpoint.write_text('{"goods_id":"saved"}\n',encoding='utf-8')
    saved=checkpoint.read_bytes(); calls=[]
    monkeypatch.setattr(selection,'control_command_finished',lambda session,query:command_finished)
    def verify(session,query):
        calls.append(query);return page_ready
    held=selection.hold_blocked_worker(tmp_path,{'name':'worker1'},children={},finished={'worker1'},attempts={},next_at={},state={},verify=verify)
    assert held is not resumed
    assert bool(calls) is command_finished
    assert checkpoint.read_bytes()==saved
    current=selection.read(tmp_path/'temu/supervisors/worker1.json')
    assert (current['status']=='RESUMING') is resumed
