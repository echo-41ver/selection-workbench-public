import io
import json
from pathlib import Path
import sys
from types import SimpleNamespace
from urllib.error import HTTPError, URLError

import pytest

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
from collect_market_evidence import safe_failure
from selection_automation.market_provider import Provider

BASE='https://openapi.geekbi.com'


@pytest.fixture
def sandbox(tmp_path,monkeypatch):
    import geekbi_temu_mcp.auth as sdk
    import selection_automation.market_auth as installed
    clock=[2000000000];sleeps=[]
    monkeypatch.setattr(installed.time,'time',lambda:clock[0])
    monkeypatch.setattr(installed.time,'sleep',sleeps.append)
    monkeypatch.setenv('GEEKBI_BASE_URL',BASE)
    monkeypatch.setattr(sdk,'default_auth_path',lambda:tmp_path/'shared.json')
    monkeypatch.setattr(sdk,'urlopen',lambda *a,**k:pytest.fail('unexpected network request'))
    path=tmp_path/'runtime/market-auth/agent-auth.json';path.parent.mkdir(parents=True)
    row={'accessToken':'TEST_ONLY_ACCESS_TOKEN','accessTokenExpiresAt':clock[0]+3600}
    path.write_text(json.dumps({'version':1,'servers':{BASE:row}}),encoding='utf8')
    return SimpleNamespace(auth=Provider(auth_state_path=path).client.auth,sdk=sdk,
        clock=clock,sleeps=sleeps,path=path,row=row)


def test_authenticated_get_recovers_once_with_same_url_authorization_and_timeout(sandbox,monkeypatch,capsys):
    calls=[]
    def transport(request,timeout):
        calls.append((request.full_url,request.get_method(),dict(request.header_items()),timeout))
        if len(calls)==1:raise URLError(ConnectionResetError(10054,'TEST_ONLY_PRIVATE_DETAIL'))
        return io.BytesIO(b'{"code":0,"data":[]}')
    monkeypatch.setattr(sandbox.sdk,'urlopen',transport)
    assert sandbox.auth.request_json(BASE+'/api/v1/temu/site/ai-list',12)=={'code':0,'data':[]}
    assert len(calls)==2 and calls[0]==calls[1]
    assert sandbox.sleeps==[.3]
    assert capsys.readouterr()==('','')


def test_second_url_error_is_returned_without_logging_private_exception(sandbox,monkeypatch,capsys):
    failure=URLError(ConnectionResetError(10054,'TEST_ONLY_PRIVATE_DETAIL'));calls=[]
    def transport(*args,**kwargs):calls.append(1);raise failure
    monkeypatch.setattr(sandbox.sdk,'urlopen',transport)
    with pytest.raises(URLError) as caught:sandbox.auth.request_json(BASE+'/api/v1/temu/site/ai-list',12)
    assert caught.value is failure and len(calls)==2 and sandbox.sleeps==[.3]
    assert 'TEST_ONLY' not in json.dumps(safe_failure(caught.value))
    assert capsys.readouterr()==('','')


@pytest.mark.parametrize('condition',['no_token','expires_soon','expired','pending','post','clock_advanced'])
def test_unqualified_request_does_not_retry_or_repeat_token_exchange(sandbox,monkeypatch,condition):
    row=dict(sandbox.row);options={}
    if condition=='no_token':row={}
    elif condition=='expires_soon':row['accessTokenExpiresAt']=sandbox.clock[0]+30
    elif condition=='expired':row['accessTokenExpiresAt']=sandbox.clock[0]-1
    elif condition=='pending':row['pending']={'deviceCode':'TEST_ONLY_DEVICE','jumpUrl':BASE+'/agent/auth/login','expiresAt':sandbox.clock[0]+600}
    elif condition=='post':options={'method':'POST','body':b'{}'}
    elif condition=='clock_advanced':sandbox.clock[0]+=3590
    sandbox.path.write_text(json.dumps({'version':1,'servers':{BASE:row}}),encoding='utf8')
    calls=[]
    def transport(request,timeout):calls.append((request.full_url,request.get_method()));raise URLError(TimeoutError())
    monkeypatch.setattr(sandbox.sdk,'urlopen',transport)
    with pytest.raises(URLError):sandbox.auth.request_json(BASE+'/api/v1/temu/site/ai-list',12,**options)
    assert len(calls)==1 and sandbox.sleeps==[]
    if condition=='pending':assert calls==[(BASE+sandbox.sdk.TOKEN_ENDPOINT,'POST')]


@pytest.mark.parametrize('status',[401,403])
def test_http_rejection_is_not_a_transient_network_retry(sandbox,monkeypatch,status):
    calls=[]
    def transport(request,timeout):
        calls.append(1)
        raise HTTPError(request.full_url,status,'TEST_ONLY_PRIVATE_DETAIL',{},io.BytesIO(b'{"code":1,"msg":"TEST_ONLY_REJECTION"}'))
    monkeypatch.setattr(sandbox.sdk,'urlopen',transport)
    with pytest.raises(sandbox.sdk.GeekBIError):sandbox.auth.request_json(BASE+'/api/v1/temu/site/ai-list',12)
    assert len(calls)==1 and sandbox.sleeps==[]


def test_auth_required_uses_existing_login_hook_once_without_network_retry(sandbox,monkeypatch):
    import selection_automation.market_login as login
    calls=[];logins=[]
    def transport(request,timeout):
        calls.append(1)
        payload={'code':1,'data':{'error':'AUTH_REQUIRED','deviceCode':'TEST_ONLY_DEVICE',
            'jumpUrl':BASE+'/agent/auth/login','expiresIn':600}}
        raise HTTPError(request.full_url,401,'fixture',{},io.BytesIO(json.dumps(payload).encode()))
    monkeypatch.setattr(sandbox.sdk,'urlopen',transport)
    monkeypatch.setattr(login,'complete_company_login',lambda *a:logins.append(1) or False)
    with pytest.raises(sandbox.sdk.ActionRequired):sandbox.auth.request_json(BASE+'/api/v1/temu/site/ai-list',12)
    assert calls==[1] and logins==[1] and sandbox.sleeps==[]
