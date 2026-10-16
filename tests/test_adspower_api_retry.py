import io
import json
import sys
from pathlib import Path
from types import SimpleNamespace
import urllib.error
import pytest

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
import replace_proxyshare_worker_environment as module


def fake_api(monkeypatch, sequence):
    calls=[]; sleeps=[]
    def request(req,timeout):
        calls.append((req.method,timeout))
        item=sequence.pop(0)
        if isinstance(item,Exception):raise item
        return io.BytesIO(json.dumps(item).encode())
    monkeypatch.setattr(module.urllib.request,'build_opener',lambda *_:SimpleNamespace(open=request))
    monkeypatch.setattr(module.time,'sleep',sleeps.append)
    return calls,sleeps


@pytest.mark.parametrize('error',[urllib.error.URLError('secret'),TimeoutError('secret'),OSError('secret')])
def test_get_connection_recovers_with_bounded_delays(monkeypatch,error):
    calls,sleeps=fake_api(monkeypatch,[error,error,{'code':0,'data':{}}])
    assert module.api_call('http://fixture','/status',timeout=7)['code']==0
    assert calls==[('GET',7)]*3 and sleeps==[1,2]


def test_post_unknown_delivery_never_retries(monkeypatch):
    calls,sleeps=fake_api(monkeypatch,[urllib.error.URLError('password-secret')])
    with pytest.raises(RuntimeError) as caught:module.api_call('http://user:secret@fixture','/create',method='POST',body={'password':'secret'})
    assert len(calls)==1 and not sleeps
    assert 'secret' not in str(caught.value)


@pytest.mark.parametrize('method',['GET','POST'])
def test_explicit_rate_rejection_can_retry(method,monkeypatch):
    calls,sleeps=fake_api(monkeypatch,[{'code':-1,'msg':'too many request'},{'code':-1,'msg':'too many requests'},{'code':0}])
    assert module.api_call('http://fixture','/create',method=method)['code']==0
    assert len(calls)==3 and sleeps==[1,2]


@pytest.mark.parametrize('status',[401,403])
def test_http_auth_failure_never_retries_or_leaks_body(monkeypatch,status):
    calls,sleeps=fake_api(monkeypatch,[urllib.error.HTTPError('http://secret',status,'secret',{},io.BytesIO(b'password-secret'))])
    with pytest.raises(RuntimeError,match=f'HTTP {status}') as caught:module.api_call('http://fixture','/status')
    assert len(calls)==1 and not sleeps and 'secret' not in str(caught.value)


def test_persistent_connection_failure_stops_after_three(monkeypatch):
    calls,sleeps=fake_api(monkeypatch,[OSError('secret')]*3)
    with pytest.raises(RuntimeError):module.api_call('http://fixture','/status')
    assert len(calls)==3 and sleeps==[1,2]


def test_capacity_detection_survives_redaction(monkeypatch):
    from setup_proxyshare_adspower_workers import is_account_capacity_error
    fake_api(monkeypatch,[{'code':-1,'msg':'number of imported accounts exceeds the limit of 12 password=secret'}])
    with pytest.raises(RuntimeError) as caught:module.api_call('http://fixture','/create',method='POST')
    assert is_account_capacity_error(caught.value)
    assert 'secret' not in str(caught.value)


def test_not_open_remains_recognizable_without_echoing_profile_data(monkeypatch):
    calls,sleeps=fake_api(monkeypatch,[{'code':-1,'msg':'Browser is not open; password=secret'}])
    with pytest.raises(RuntimeError) as caught:module.api_call('http://fixture','/stop')
    assert 'not open' in str(caught.value)
    assert 'secret' not in str(caught.value)
    assert len(calls)==1 and not sleeps


@pytest.mark.parametrize('message',[
    'number of accounts exceeds limit of 12',
    'Number of imported accounts exceeds the limit of 12',
])
def test_all_existing_capacity_phrase_variants_remain_recognizable(monkeypatch,message):
    from setup_proxyshare_adspower_workers import is_account_capacity_error
    fake_api(monkeypatch,[{'code':-1,'msg':message}])
    with pytest.raises(RuntimeError) as caught:module.api_call('http://fixture','/create',method='POST')
    assert is_account_capacity_error(caught.value)
def test_existing_new_profile_start_retries_without_recreating(monkeypatch):
    import replace_proxyshare_worker_environment as replacement
    calls=[]
    def call(base,endpoint,**kwargs):
        calls.append(endpoint)
        if len(calls)<3: raise RuntimeError('provider rejected request')
        return {'data':{'marionette_port':1234}}
    monkeypatch.setattr(replacement,'api_call',call)
    monkeypatch.setattr(replacement.time,'sleep',lambda _:None)
    assert replacement.start_profile_with_retry('local','existing-new')['marionette_port']==1234
    assert len(calls)==3 and len(set(calls))==1
    assert all('/browser/start?' in url and 'user_id=existing-new' in url for url in calls)


@pytest.mark.parametrize('still_exists', [True, False])
def test_delete_connection_loss_reconciles_exact_profile(monkeypatch, still_exists):
    calls=[]
    def api(base,path,**kwargs):
        calls.append(path)
        if len(calls)==1: raise RuntimeError('AdsPower Local API 无法连接；本次请求未确认完成')
        if 'user/list' in path:
            assert 'user_id=target' in path
            return {'data':{'list':[{'user_id':'target'}] if still_exists else []}}
        return {'code':0}
    monkeypatch.setattr(module,'api_call',api)
    monkeypatch.setattr(module.time,'sleep',lambda _:None)
    module.delete_profile_after_release('http://fixture','target')
    assert len(calls)==(3 if still_exists else 2)


def test_delete_persistent_disconnect_is_bounded(monkeypatch):
    calls=[]
    def api(*args,**kwargs):
        calls.append(args)
        raise RuntimeError('无法连接')
    monkeypatch.setattr(module,'api_call',api)
    monkeypatch.setattr(module.time,'sleep',lambda _:None)
    with pytest.raises(RuntimeError):module.delete_profile_after_release('http://fixture','target')
    assert len(calls)==6
