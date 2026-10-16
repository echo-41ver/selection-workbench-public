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
