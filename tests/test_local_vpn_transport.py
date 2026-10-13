import json
import pytest
from selection_automation import browser_transport as transport
from selection_automation.company_bundle import normalize
from selection_automation.recovery_policy import recovery_kind


def test_local_vpn_config_needs_no_residential_credentials():
    config=normalize({'adspower_api_key':'fixture','proxy_architecture':'LOCAL_VPN'})
    assert config['proxy_architecture']=='LOCAL_VPN' and 'proxy' not in config


@pytest.mark.parametrize('protocol,port,expected',[
    ('SOCKS5',7890,'socks5'),('HTTP',7890,'http'),('SYSTEM_ROUTE',0,None),
])
def test_ads_and_amazon_use_same_verified_local_route(monkeypatch,tmp_path,protocol,port,expected):
    monkeypatch.setattr(transport,'verify_saved',lambda row:True)
    settings={'proxy_architecture':'LOCAL_VPN','preproxy_protocol':protocol,'preproxy_port':port,'preproxy_host':'127.0.0.1'}
    directory=tmp_path/'runtime/secrets';directory.mkdir(parents=True)
    (directory/'proxyshare.settings.json').write_text(json.dumps(settings))
    ads=transport.adspower_proxy(settings)
    amazon=transport.amazon_environment(tmp_path)
    if expected:
        assert ads['proxy_type']==expected and ads['proxy_port']=='7890'
        assert amazon=={'AMAZON_BROWSER_PROXY':f'{expected}://127.0.0.1:7890'}
    else:assert ads=={'proxy_soft':'no_proxy'} and amazon=={}


def test_changed_route_does_not_fall_back(monkeypatch,tmp_path):
    monkeypatch.setattr(transport,'verify_saved',lambda row:False)
    with pytest.raises(RuntimeError,match='归属已变化'):transport.adspower_proxy({})


@pytest.mark.parametrize('status,kind',[('STOPPED_HTTP_403','ACCESS'),('STOPPED_HTTP_429','RATE_LIMIT'),('STOPPED_LOGIN_REQUIRED','LOGIN'),('WAITING_CAPTCHA','CAPTCHA')])
def test_local_vpn_site_gate_requests_a_new_browser_session(status,kind):
    assert recovery_kind(status,{'scroll_collection_started':True,'automatic_recovery_eligible':True,'resume_requires_session_rotation':True},True) == kind
