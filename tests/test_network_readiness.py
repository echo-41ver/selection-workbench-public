import pathlib,sys,pytest
sys.path.insert(0,str(pathlib.Path(__file__).resolve().parents[1]/'scripts'))
from selection_automation.network_readiness import select_transport,validate_saved_route,describe,NetworkNotReady
from selection_automation.company_bundle import normalize

def company(**extra):
    return {'adspower_api_key':'TEST_ONLY_ADSPOWER','proxy':{'host':'proxy.example','port':5959,
            'username':'TEST_ONLY_USER_area-US_session-fixture_life-10','password':'TEST_ONLY_PASSWORD'},**extra}

def test_auto_without_running_client_is_not_ready_and_never_falls_back():
    with pytest.raises(NetworkNotReady):
        select_transport(company(),local_discovery=lambda:{'status':'NO_COMPATIBLE_PROXY','candidates':[]})

def test_required_missing_socks_never_checks_direct_gateway_or_claims_ready():
    calls=[]
    with pytest.raises(NetworkNotReady) as exc:
        select_transport(company(preproxy_mode='SOCKS5_REQUIRED'),tcp_probe=lambda *a:calls.append(a),local_discovery=lambda:{'candidates':[]})
    assert not calls and exc.value.diagnostic['target_access']=='UNVERIFIED'

def test_explicit_direct_is_valid_and_does_not_probe_or_force_local_socks():
    settings,status=select_transport(company(preproxy_mode='DIRECT'),local_discovery=lambda:pytest.fail('no local probe'))
    assert settings['preproxy_port']==0 and status['selected_mode']=='DIRECT'

@pytest.mark.parametrize('mode',['AUTO','DIRECT','SOCKS5_REQUIRED'])
def test_explicit_mode_survives_company_normalization(mode):
    assert normalize(company(preproxy_mode=mode))['preproxy_mode']==mode

@pytest.mark.parametrize('bad',['SOCKS5',None,False,[],{}])
def test_invalid_mode_fails_before_probing(bad):
    with pytest.raises((ValueError,TypeError)):
        select_transport(company(preproxy_mode=bad),local_discovery=lambda:pytest.fail('no probes'))

def test_legacy_company_and_saved_route_keep_default_behavior():
    assert 'preproxy_mode' not in normalize(company())
    status=validate_saved_route(company(),{'preproxy_port':7890})
    assert status['selected_mode']=='SOCKS5' and status['probe_level']=='SAVED_CONFIGURATION_ONLY'
    assert status['target_access']=='UNVERIFIED'

@pytest.mark.parametrize('mode,port',[('SOCKS5_REQUIRED',0),('DIRECT',7890)])
def test_conflicting_saved_route_needs_confirmation_not_automatic_change(mode,port):
    settings={'preproxy_port':port};before=dict(settings)
    with pytest.raises(NetworkNotReady):validate_saved_route(company(preproxy_mode=mode),settings)
    assert settings==before

def test_status_exposes_no_company_secrets():
    settings,status=select_transport(company(preproxy_mode='DIRECT'))
    assert all('TEST_ONLY' not in str(value) for value in status.values())
    assert describe({})['target_access']=='UNVERIFIED'
