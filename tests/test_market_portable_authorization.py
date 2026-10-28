import json

import pytest

from selection_automation.company_bundle import import_bundle
from selection_automation.market_provider import Provider
from selection_automation.secrets import store


@pytest.mark.parametrize('local_seconds,package_seconds,shared_seconds,expected',[
    (3600,7200,10800,'LOCAL'), (None,3600,7200,'PACKAGE'), (-1,3600,7200,'PACKAGE'),
    (None,-1,3600,'SHARED'), (None,30,3600,'SHARED'), (None,None,3600,'SHARED'),
    (None,-1,None,None), (-1,None,-1,None),
])
def test_installation_then_package_then_shared_authorization(tmp_path,monkeypatch,capsys,local_seconds,package_seconds,shared_seconds,expected):
    import geekbi_temu_mcp.auth as sdk
    import selection_automation.market_auth as installed
    import selection_automation.secrets as secrets
    base='https://openapi.geekbi.com';now=2000000000
    monkeypatch.setattr(installed.time,'time',lambda:now)
    monkeypatch.setenv('GEEKBI_BASE_URL',base)
    shared=tmp_path/'shared.json';local=tmp_path/'runtime/market-auth/agent-auth.json'
    monkeypatch.setattr(sdk,'default_auth_path',lambda:shared)
    monkeypatch.setattr(sdk,'urlopen',lambda *a,**k:pytest.fail('unexpected network request'))
    for path,seconds,label in ((local,local_seconds,'LOCAL'),(shared,shared_seconds,'SHARED')):
        if seconds is not None:
            path.parent.mkdir(parents=True,exist_ok=True)
            path.write_text(json.dumps({'version':1,'servers':{base:{'accessToken':'TEST_ONLY_'+label,
                'accessTokenExpiresAt':now+seconds,'pending':{'deviceCode':'TEST_ONLY_PENDING','expiresAt':now+1000}}}}),encoding='utf8')
    account={'username':'TEST_ONLY_USER','password':'TEST_ONLY_PASSWORD'}
    if package_seconds is not None:account.update(access_token='TEST_ONLY_PACKAGE',expires_at=now+package_seconds)
    import_bundle(tmp_path,{'adspower_api_key':'TEST_ONLY_ADS','proxy_architecture':'LOCAL_VPN','geekbi':account},store)
    if expected=='LOCAL':monkeypatch.setattr(secrets,'load',lambda *a:pytest.fail('valid local authorization must not read company credentials'))
    provider=Provider(auth_state_path=local)
    assert provider.client.auth._authorization_header()==('Bearer TEST_ONLY_'+expected if expected else None)
    if expected in {'PACKAGE','SHARED'}:
        saved=json.loads(local.read_text('utf8'))['servers'][base]
        assert set(saved)=={'accessToken','accessTokenExpiresAt'}
    assert capsys.readouterr()==('','')


def test_packaged_authorization_is_not_sent_to_another_server(tmp_path,monkeypatch):
    import geekbi_temu_mcp.auth as sdk
    monkeypatch.setenv('GEEKBI_BASE_URL','https://fixture.example')
    monkeypatch.setattr(sdk,'default_auth_path',lambda:tmp_path/'shared.json')
    monkeypatch.setattr(sdk,'urlopen',lambda *a,**k:pytest.fail('unexpected network request'))
    import_bundle(tmp_path,{'adspower_api_key':'TEST_ONLY_ADS','proxy_architecture':'LOCAL_VPN',
        'geekbi':{'username':'TEST_ONLY_USER','password':'TEST_ONLY_PASSWORD','access_token':'TEST_ONLY_PACKAGE','expires_at':4102444800}},store)
    provider=Provider(auth_state_path=tmp_path/'runtime/market-auth/agent-auth.json')
    assert provider.client.auth._authorization_header() is None
