"""Exercise the real supervisor through site stop, replacement, and same-pass resume."""
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import pipeline


def test_local_vpn_recovery_never_starts_residential_chain(tmp_path, monkeypatch):
    from selection_automation import browser_transport
    monkeypatch.setattr(pipeline,'ROOT',tmp_path)
    monkeypatch.setattr(browser_transport,'verify_saved',lambda settings:True)
    write(tmp_path/'runtime/secrets/proxyshare.settings.json',
          {'proxy_architecture':'LOCAL_VPN','preproxy_protocol':'SOCKS5',
           'preproxy_host':'127.0.0.1','preproxy_port':7890})
    def unexpected(*args,**kwargs):pytest.fail('Local VPN must not use a residential proxy chain')
    monkeypatch.setattr(pipeline.socket,'create_connection',unexpected)
    monkeypatch.setattr(pipeline,'run_background_launcher',unexpected)
    assert pipeline.ensure_proxy_chain(1,19081,SimpleNamespace(check_stop=lambda:None)) is False


def write(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding='utf-8')


@pytest.mark.parametrize('status,kind,scrolled', [
    ('WAITING_LOGIN','LOGIN',True), ('WAITING_CAPTCHA','CAPTCHA',True),
    ('STOPPED_HTTP_403','ACCESS',True), ('STOPPED_HTTP_429','RATE_LIMIT',True),
    ('WAITING_CAPTCHA',None,False), ('WAITING_SEARCH_VERIFICATION',None,False),
    ('WAITING_HUMAN_VERIFICATION',None,False),
])
def test_site_stop_replaces_rebinds_and_resumes_same_output(tmp_path, monkeypatch, status, kind, scrolled):
    monkeypatch.setattr(pipeline,'ROOT',tmp_path)
    monkeypatch.setattr(pipeline,'ensure_proxy_chain',lambda *args:False)
    category={'category_key':'fixture'}
    name='worker-01-new'
    sessions=tmp_path/'sessions.json'
    manifest={'workers':[{'name':name,'profile_id':'old', 'webdriver_url':'http://old',
                          'session_id_file':'old.sid','bidi_url_file':'old.bidi'}]}
    write(sessions,manifest)
    write(tmp_path/'config/proxy_policy.json', {
        'session_rotation_policy':'NEW_ADSPOWER_PROFILE_AND_NEW_PROXY_SESSION',
        'delete_old_profile_first_via_adspower':True,'worker_chain_ports':[19081]})
    write(pipeline.keyword_plan_path(category),{'keywords':[{'query':'mouse','max_products':2}]})
    calls=[]; waits=[]; replacements=[]
    def collect(command, **kwargs):
        calls.append(command.copy())
        assert command[command.index('--auto-session-recovery')+1]=='true'
        output=Path(command[command.index('--output')+1])
        output.mkdir(parents=True,exist_ok=True)
        snapshot=output/'products.jsonl'
        if len(calls)==1:
            snapshot.write_text('{"goods_id":"retained"}\n',encoding='utf-8')
            write(output/'progress.json', {'status':status,'unique_goods':1,
                'scroll_collection_started':scrolled,
                'manual_action_required':True,'automatic_retry':False,
                'http_block':{'status':429,'retry_after_seconds':180} if kind=='RATE_LIMIT' else None})
            return 2
        if len(calls)==2:
            assert snapshot.read_text('utf-8')=='{"goods_id":"retained"}\n'
            assert output == Path(calls[0][calls[0].index('--output')+1])
            assert command[command.index('--webdriver-url')+1]=='http://new'
        write(output/'progress.json',{'status':'KEYWORD_LIMIT_REACHED','unique_goods':2})
        return 0
    def replace(command,**kwargs):
        replacements.append(command)
        manifest['workers'][0].update(profile_id='new',webdriver_url='http://new',
                                     session_id_file='new.sid',bidi_url_file='new.bidi')
        write(sessions,manifest)
        return SimpleNamespace(returncode=0)
    monkeypatch.setattr(pipeline,'run_collector',collect)
    monkeypatch.setattr(pipeline.subprocess,'run',replace)
    monkeypatch.setattr(pipeline,'interruptible_wait',lambda seconds,lease:waits.append(seconds))
    if kind is None:
        with pytest.raises(RuntimeError,match='incomplete'):
            pipeline.collect_temu(tmp_path/'category.json',category,sessions,name)
        assert len(calls)==1 and replacements==[] and waits==[]
        worker=json.loads((pipeline.run_root(category)/'temu/supervisors'/f'{name}.json').read_text('utf-8'))
        assert worker['status']=='HUMAN_ACTION_REQUIRED' and worker['manual_action_required']
        output=Path(calls[0][calls[0].index('--output')+1])
        assert (output/'products.jsonl').read_text('utf-8')=='{"goods_id":"retained"}\n'
        return
    pipeline.collect_temu(tmp_path/'category.json',category,sessions,name)
    assert len(replacements)==1
    output=Path(calls[0][calls[0].index('--output')+1])
    recovery=json.loads((output/'automatic_recovery.json').read_text('utf-8'))
    assert recovery['kind']==kind and recovery['attempts_without_progress']==1
    assert waits==([180] if kind=='RATE_LIMIT' else [30] if kind=='ACCESS' else [])
    worker=json.loads((pipeline.run_root(category)/'temu/supervisors'/f'{name}.json').read_text('utf-8'))
    assert worker['status']=='WORKER_COMPLETE'


@pytest.fixture
def login_budget_setup(tmp_path, monkeypatch):
    monkeypatch.setattr(pipeline, 'ROOT', tmp_path)
    monkeypatch.setattr(pipeline, 'ensure_proxy_chain', lambda *args: False)
    name = 'worker-01-new'
    category = {'category_key': 'fixture'}
    sessions = tmp_path / 'sessions.json'
    manifest = {'workers': [{'name': name, 'profile_id': 'initial',
        'webdriver_url': 'http://initial', 'session_id_file': 'initial.sid',
        'bidi_url_file': 'initial.bidi'}]}
    write(sessions, manifest)
    write(tmp_path / 'config/proxy_policy.json', {
        'session_rotation_policy': 'NEW_ADSPOWER_PROFILE_AND_NEW_PROXY_SESSION',
        'delete_old_profile_first_via_adspower': True, 'worker_chain_ports': [19081]})
    write(pipeline.keyword_plan_path(category), {'keywords': [{'query': 'mouse', 'max_products': 2}]})
    scenario = {'remaining_stops': 5, 'goods': 0, 'kind': 'LOGIN'}
    calls, replacements = [], []
    def collect(command, **kwargs):
        calls.append(command.copy())
        output = Path(command[command.index('--output') + 1])
        output.mkdir(parents=True, exist_ok=True)
        marker = output / 'retained-checkpoint.txt'
        if not marker.exists(): marker.write_bytes(b'original query/pass checkpoint')
        if scenario['remaining_stops']:
            scenario['remaining_stops'] -= 1
            status = scenario.get('status') or ('STOPPED_LOGIN_REQUIRED' if scenario['kind'] == 'LOGIN' else 'STOPPED_HTTP_403')
            write(output / 'progress.json', {'status': status, 'unique_goods': scenario['goods'],
                'confirmed_login_redirect': scenario.get('confirmed_login_redirect', scenario['kind'] == 'LOGIN'),
                'http_block': scenario.get('http_block'), 'http_status': scenario.get('http_status'),
                'manual_action_required': True, 'automatic_retry': False})
            return 2
        write(output / 'progress.json', {'status': 'KEYWORD_LIMIT_REACHED', 'unique_goods': 2})
        return 0
    def replace(command, **kwargs):
        assert Path(command[1]).name == 'replace_proxyshare_worker_environment.py'
        replacements.append(command.copy())
        identity = f'changed-profile-{len(replacements)}'
        manifest['workers'][0].update(profile_id=identity, webdriver_url='http://' + identity,
            session_id_file=identity + '.sid', bidi_url_file=identity + '.bidi')
        write(sessions, manifest)
        return SimpleNamespace(returncode=0)
    monkeypatch.setattr(pipeline, 'run_collector', collect)
    monkeypatch.setattr(pipeline.subprocess, 'run', replace)
    monkeypatch.setattr(pipeline, 'interruptible_wait', lambda *args: None)
    base = pipeline.run_root(category)
    def output(query='mouse', sort='sales'):
        return base / 'temu/workers' / name / (pipeline.slug(query) + ('--recent' if sort == 'recent' else ''))
    def seed(query='mouse', sort='sales'):
        write(output(query, sort) / 'automatic_recovery.json', {
            'attempts_without_progress': 3, 'unique_goods_before': 0, 'profile_id': 'previous-profile',
            'kind': 'LOGIN', 'limit': 3, 'query': query, 'sort': sort})
    return SimpleNamespace(run=lambda: pipeline.collect_temu(tmp_path / 'category.json', category, sessions, name),
        scenario=scenario, calls=calls, replacements=replacements, output=output, seed=seed,
        worker=base / 'temu/supervisors' / f'{name}.json')


def test_login_recovery_budget_stops_after_three_successful_replacements(login_budget_setup):
    f = login_budget_setup
    with pytest.raises(RuntimeError, match='incomplete'): f.run()
    assert len(f.calls) == 4 and len(f.replacements) == 3
    ledger = json.loads((f.output() / 'automatic_recovery.json').read_text('utf-8'))
    assert ledger['attempts_without_progress'] == ledger['limit'] == 3
    assert ledger['unique_goods_before'] == 0
    worker = json.loads(f.worker.read_text('utf-8'))
    assert worker['status'] == 'HUMAN_ACTION_REQUIRED' and worker['recovery_budget_exhausted']
    assert worker['manual_action_required'] and worker['browser_preserved']
    assert worker['automatic_retry'] is False and worker['automatic_resume_pending'] is False
    assert '3' in worker['recovery_action'] and '登录' in worker['recovery_action']
    assert (f.output() / 'retained-checkpoint.txt').read_bytes() == b'original query/pass checkpoint'


def test_login_recovery_budget_survives_restart_and_profile_change(login_budget_setup):
    f = login_budget_setup
    f.seed()
    with pytest.raises(RuntimeError, match='incomplete'): f.run()
    assert len(f.calls) == 1 and f.replacements == []
    before = (f.output() / 'automatic_recovery.json').read_bytes()
    with pytest.raises(RuntimeError, match='incomplete'): f.run()
    assert len(f.calls) == 2 and f.replacements == []
    assert (f.output() / 'automatic_recovery.json').read_bytes() == before


def test_login_recovery_budget_resets_only_after_real_goods_progress(login_budget_setup):
    f = login_budget_setup
    f.seed()
    f.scenario.update(remaining_stops=1, goods=1)
    f.run()
    assert len(f.replacements) == 1
    ledger = json.loads((f.output() / 'automatic_recovery.json').read_text('utf-8'))
    assert ledger['attempts_without_progress'] == 1 and ledger['unique_goods_before'] == 1
    assert ledger['progress_since_previous_recovery'] is True


@pytest.mark.parametrize('other_query,other_sort', [('other query', 'sales'), ('mouse', 'recent')])
def test_login_recovery_budget_is_independent_for_each_query_and_sort(login_budget_setup, other_query, other_sort):
    f = login_budget_setup
    f.seed(other_query, other_sort)
    prior = (f.output(other_query, other_sort) / 'automatic_recovery.json').read_bytes()
    with pytest.raises(RuntimeError, match='incomplete'): f.run()
    assert len(f.replacements) == 3
    assert (f.output(other_query, other_sort) / 'automatic_recovery.json').read_bytes() == prior
    assert json.loads((f.output() / 'automatic_recovery.json').read_text('utf-8'))['attempts_without_progress'] == 3


def test_login_recovery_budget_does_not_change_other_recovery_kinds(login_budget_setup):
    f = login_budget_setup
    f.seed()
    f.scenario.update(remaining_stops=1, kind='ACCESS')
    f.run()
    assert len(f.replacements) == 1
    ledger = json.loads((f.output() / 'automatic_recovery.json').read_text('utf-8'))
    assert ledger['kind'] == 'ACCESS' and ledger['limit'] is None
    assert ledger['attempts_without_progress'] == 4


def test_login_recovery_budget_allows_already_recovered_page_without_new_profile(login_budget_setup):
    f = login_budget_setup
    f.seed()
    with pytest.raises(RuntimeError, match='incomplete'): f.run()
    assert json.loads(f.worker.read_text('utf-8'))['recovery_budget_exhausted'] is True
    before = (f.output() / 'automatic_recovery.json').read_bytes()
    f.scenario['remaining_stops'] = 0
    f.run()
    assert f.replacements == []
    worker = json.loads(f.worker.read_text('utf-8'))
    assert worker['status'] == 'WORKER_COMPLETE'
    assert (f.output() / 'automatic_recovery.json').read_bytes() == before
    assert (f.output() / 'retained-checkpoint.txt').read_bytes() == b'original query/pass checkpoint'


@pytest.mark.parametrize('status,http_evidence', [
    ('STOPPED_HTTP_403', {}), ('STOPPED_HTTP_429', {}), ('STOPPED_ACCESS_DENIED', {}),
    ('STOPPED_HTTP_BLOCK', {'http_block': {'status': 403}}),
    ('STOPPED_HTTP_BLOCK', {'http_status': 429}),
])
def test_http_failure_with_login_redirect_retains_nonhuman_recovery_contract(login_budget_setup, status, http_evidence):
    f = login_budget_setup
    f.scenario.update(status=status, confirmed_login_redirect=True, **http_evidence)
    f.run()
    assert len(f.replacements) == 5
    # The five replacements and the recovered first pass retain one output;
    # the following, successfully unlocked recent-sort pass has its own output.
    recovery_calls = f.calls[:len(f.replacements) + 1]
    assert len(recovery_calls) == 6
    assert len({command[command.index('--output') + 1] for command in recovery_calls}) == 1
    ledger = json.loads((f.output() / 'automatic_recovery.json').read_text('utf-8'))
    assert ledger['kind'] == 'LOGIN' and ledger['limit'] is None
    assert ledger['attempts_without_progress'] == 5
    assert json.loads(f.worker.read_text('utf-8'))['status'] == 'WORKER_COMPLETE'
    assert (f.output() / 'retained-checkpoint.txt').read_bytes() == b'original query/pass checkpoint'


@pytest.mark.parametrize('http_evidence', [{'http_block': {'status': 403}}, {'http_status': 429}])
@pytest.mark.parametrize('status', ['STOPPED_LOGIN_REQUIRED', 'WAITING_LOGIN'])
def test_explicit_login_recovery_remains_bounded_with_http_metadata(login_budget_setup, http_evidence, status):
    f = login_budget_setup
    f.scenario.update(status=status, **http_evidence)
    with pytest.raises(RuntimeError, match='incomplete'): f.run()
    assert len(f.replacements) == 3
    assert json.loads(f.worker.read_text('utf-8'))['recovery_budget_exhausted'] is True
