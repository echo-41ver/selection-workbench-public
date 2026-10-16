import json,pathlib,sys,pytest
sys.path.insert(0,str(pathlib.Path(__file__).resolve().parents[1]/'scripts'))
from selection_automation.local_proxy import discover,choose,probe,endpoint
from selection_automation.network_readiness import select_transport,NetworkNotReady
from selection_automation.local_proxy_prepare import prepare,verify_saved
from selection_automation.company_bundle import normalize

def client(port=45678,pid=101,host='127.0.0.1',name='clash-meta.exe',protocol=None):
    return {'pid':pid,'created':100.0,'name':name,'listeners':[{'host':host,'port':port,'declared_protocol':protocol}],'declared_ports':[]}
def evidence(*clients,system=None):return {'processes':list(clients),'system':system or {}}
def scan(data,protocol='SOCKS5',owner=lambda c:True):
    return discover(data,check_owner=owner,protocol_probe=lambda c:{'protocol':protocol,'authentication_required':False})
def company(**extra):return {'proxy':{'host':'proxy.example','port':5959},**extra}

@pytest.mark.parametrize('port',[10800,45678,50001])
@pytest.mark.parametrize('protocol',['SOCKS5','HTTP','MIXED'])
def test_non_default_port_and_protocol(port,protocol):
    data=scan(evidence(client(port)),protocol)
    transport=choose(data)
    assert transport['preproxy_port']==port
    assert transport['preproxy_protocol']==('SOCKS5' if protocol=='SOCKS5' else 'HTTP')

@pytest.mark.parametrize('host',['127.0.0.1','::1'])
def test_ipv4_and_ipv6_loopback(host):
    assert choose(scan(evidence(client(host=host))))['preproxy_host']==host

def test_configured_port_does_not_replace_actual_listener():
    c=client(45678);c['declared_ports']=[7890]
    data=scan(evidence(c));assert choose(data)['preproxy_port']==45678
    assert data['warnings']

def test_no_client_or_installed_not_running_does_not_fallback():
    with pytest.raises(NetworkNotReady,match='先启动'):
        select_transport(company(),local_discovery=lambda:scan(evidence()))

def test_running_but_unknown_protocol_does_not_become_socks():
    with pytest.raises(NetworkNotReady):choose(scan(evidence(client()),None))

def test_multiple_non_equivalent_clients_require_explicit_selection():
    data=scan(evidence(client(45678),client(45679,202,name='sing-box.exe')))
    assert data['status']=='AMBIGUOUS'
    with pytest.raises(NetworkNotReady,match='多个'):choose(data)
    assert choose(data,preferred={'host':'127.0.0.1','port':45679})['local_proxy_owner']['pid']==202

def test_unknown_process_not_auto_adopted_even_with_system_proxy():
    data=scan(evidence(client(name='ordinary-server.exe'),system={'endpoints':[{'host':'127.0.0.1','port':45678,'declared_protocol':'HTTP'}]}))
    assert not data['candidates']

def test_owner_changed_during_probe_is_rejected():
    calls=iter([True,False])
    assert not scan(evidence(client()),owner=lambda c:next(calls))['candidates']

def test_reused_port_not_owned_is_never_probed():
    data=discover(evidence(client()),check_owner=lambda c:False,protocol_probe=lambda c:pytest.fail('must not probe'))
    assert not data['candidates']

def test_pac_is_metadata_only_not_executed():
    data=scan(evidence(system={'pac_present':True}));assert any('PAC' in s for s in data['warnings'])

def test_bound_no_port_scan_when_too_many_candidates():
    c=client();c['listeners']=[{'host':'127.0.0.1','port':40000+i} for i in range(13)]
    data=discover(evidence(c),check_owner=lambda c:pytest.fail('too many'),protocol_probe=lambda c:pytest.fail('too many'))
    assert data['status']=='TOO_MANY_CANDIDATES'

def test_direct_has_priority_and_never_discovers():
    settings,status=select_transport(company(preproxy_mode='DIRECT'),local_discovery=lambda:pytest.fail('no discovery'))
    assert settings['preproxy_port']==0 and status['target_access']=='UNVERIFIED'

def test_socks_required_cannot_accept_http():
    with pytest.raises(NetworkNotReady):choose(scan(evidence(client()),'HTTP'),'SOCKS5_REQUIRED')

def test_explicit_protocol_must_match_probe():
    with pytest.raises(NetworkNotReady):choose(scan(evidence(client()),'SOCKS5'),preferred={'host':'127.0.0.1','port':45678,'protocol':'HTTP'})

def test_tun_only_preserves_system_route_without_inventing_port():
    c=client(name='wireguard.exe');c.update(tun_only=True,listeners=[])
    settings,status=select_transport(company(),local_discovery=lambda:scan(evidence(c)))
    assert settings['preproxy_port']==0 and status['selected_mode']=='SYSTEM_ROUTE'
    assert status['target_access']=='UNVERIFIED'

def test_running_task_blocks_discovery_and_switch():
    with pytest.raises(NetworkNotReady):select_transport(company(),active=True,local_discovery=lambda:pytest.fail('no discovery'))

def test_legacy_saved_port_can_be_adopted_after_owner_and_protocol_confirmation():
    settings,_=select_transport(company(),saved={'preproxy_host':'127.0.0.1','preproxy_port':45678},local_discovery=lambda:scan(evidence(client())))
    assert settings['local_proxy_owner']['pid']==101

def test_saved_owner_restart_does_not_silently_replace_route():
    saved={'preproxy_host':'127.0.0.1','preproxy_port':45678,'local_proxy_owner':{'pid':202,'created':100}}
    with pytest.raises(NetworkNotReady,match='进程已变化'):
        select_transport(company(),saved=saved,local_discovery=lambda:scan(evidence(client())))

class Sock:
    def __init__(self,reply,requests):self.reply=reply;self.requests=requests
    def __enter__(self):return self
    def __exit__(self,*a):pass
    def settimeout(self,t):pass
    def sendall(self,b):self.requests.append(b)
    def recv(self,n):part=self.reply[:n];self.reply=self.reply[n:];return part

@pytest.mark.parametrize('socks_reply,http_reply,expected',[
    (b'\x05\x00',b'', 'SOCKS5'),
    (b'',b'HTTP/1.1 502 Bad Gateway\r\nProxy-Agent: test\r\n\r\n','HTTP'),
    (b'\x05\x00',b'HTTP/1.1 502 Bad Gateway\r\nProxy-Agent: test\r\n\r\n','MIXED'),
    (b'garbage',b'HTTP/1.1 405 Method Not Allowed\r\n\r\n',None),
    (b'\x05\xff',b'HTTP/1.1 404 Not Found\r\n\r\n',None),
    (b'',b'HTTP/1.1 407 Proxy Authentication Required\r\n\r\n',None),
])
def test_bounded_protocol_handshake_never_sends_external_target_or_secret(socks_reply,http_reply,expected):
    requests=[];replies=iter([socks_reply,http_reply]);addresses=[]
    def connect(address,**kw):addresses.append(address);return Sock(next(replies),requests)
    result=probe({'host':'127.0.0.1','port':45678},connect)
    assert result['protocol']==expected and addresses==[('127.0.0.1',45678)]*2
    assert requests==[b'\x05\x01\x00',b'CONNECT 127.0.0.1:0 HTTP/1.1\r\nHost: 127.0.0.1:0\r\nConnection: close\r\n\r\n']

@pytest.mark.parametrize('socks_reply,expected',[(b'', 'HTTP'),(b'\x05\x00', 'MIXED')])
def test_declared_clash_connect_200_preserves_explicit_http_choice(socks_reply,expected):
    requests=[];replies=iter([socks_reply,b'HTTP/1.1 200 Connection established\r\n\r\n'])
    data=discover(evidence(client(7890,protocol='HTTP')),check_owner=lambda c:True,
        protocol_probe=lambda c:probe(c,lambda *a,**k:Sock(next(replies),requests)))
    assert data['candidates'][0]['protocol']==expected
    selected=choose(data,preferred={'host':'127.0.0.1','port':7890,'protocol':'HTTP'})
    assert selected['preproxy_protocol']=='HTTP'
    assert b'CONNECT 127.0.0.1:0 HTTP/1.1\r\n' in requests[-1]

@pytest.mark.parametrize('reply,declared',[
    (b'HTTP/1.1 200 OK\r\nContent-Type: text/html\r\n\r\n<html>controller</html>','HTTP'),
    (b'HTTP/1.1 200 Connection established\r\n','HTTP'),
    (b'HTTP/1.1 200 Connection established\r\n\r\n',None),
])
def test_http_200_requires_complete_tunnel_response_and_proxy_evidence(reply,declared):
    replies=iter([b'',reply])
    result=probe({'host':'127.0.0.1','port':7890,'declared_protocol':declared},
        lambda *a,**k:Sock(next(replies),[]))
    assert result['protocol'] is None

@pytest.mark.parametrize('value',['user:secret@127.0.0.1:7890','https://127.0.0.1:7890','8.8.8.8:7890','127.0.0.1:0'])
def test_untrusted_remote_or_authenticated_endpoint_rejected(value):assert endpoint(value) is None

def test_no_secret_fields_in_discovery_result():
    data=evidence(client());data['processes'][0]['ignored_password']='TEST_ONLY_SECRET'
    assert 'TEST_ONLY_SECRET' not in json.dumps(scan(data))

def test_prepare_never_writes_settings_if_task_active(tmp_path):
    writes=[]
    with pytest.raises(NetworkNotReady):prepare(tmp_path,company(),lambda *a:{},lambda *a:writes.append(a),business_active=lambda root:True,observation=lambda:pytest.fail('no discovery'))
    assert not writes

def test_prepare_rechecks_activity_before_adoption(tmp_path):
    writes=[];calls=iter([False,True])
    with pytest.raises(NetworkNotReady):prepare(tmp_path,company(),lambda *a:{},lambda p,v:writes.append((p,v)),business_active=lambda root:next(calls),observation=lambda:scan(evidence(client())),check_owner=lambda c:True)
    assert all(p.name!='proxyshare.settings.json' for p,v in writes)

def test_saved_validation_requires_owner_and_actual_generation():
    assert not verify_saved({'preproxy_port':45678})
    assert not verify_saved({'preproxy_port':45678,'preproxy_host':'127.0.0.1','local_proxy_owner':{'pid':101,'created':100,'name':'clash-meta.exe'}},lambda c:False)
    assert verify_saved({'preproxy_port':0,'preproxy_protocol':'DIRECT'})


def test_dual_stack_equivalent_route_not_ambiguous_and_ipv6_selection_works():
    c=client();c['listeners'].append({'host':'::1','port':45678})
    data=scan(evidence(c));assert data['status']=='READY'
    assert choose(data,preferred={'host':'::1','port':45678})['preproxy_host']=='::1'

def test_dual_stack_different_protocols_require_explicit_choice():
    c=client();c['listeners'].append({'host':'::1','port':45678})
    data=discover(evidence(c),check_owner=lambda c:True,protocol_probe=lambda c:{'protocol':'HTTP' if c['host']=='::1' else 'SOCKS5'})
    assert data['status']=='AMBIGUOUS'

def test_known_process_management_503_is_not_proxy_evidence():
    replies=iter([b'',b'HTTP/1.1 503 Service Unavailable\r\n\r\n']);requests=[]
    result=probe({'host':'127.0.0.1','port':45678,'name':'clash-meta.exe'},lambda *a,**k:Sock(next(replies),requests))
    assert result['protocol'] is None

def test_explicit_socks_mode_wins_over_conflicting_http_preference():
    with pytest.raises(NetworkNotReady):choose(scan(evidence(client()),'MIXED'),'SOCKS5_REQUIRED',{'host':'127.0.0.1','port':45678,'protocol':'HTTP'})


def test_existing_proxy_cannot_be_replaced_by_tun_only_route():
    saved={'preproxy_host':'127.0.0.1','preproxy_port':45678,'local_proxy_owner':{'pid':101,'created':100}}
    before=dict(saved)
    with pytest.raises(NetworkNotReady,match='不能自动改'):
        select_transport(company(),saved=saved,local_discovery=lambda:{'status':'TUN_ONLY','candidates':[]})
    assert saved==before

def test_existing_http_protocol_preserved_when_endpoint_is_mixed():
    settings,_=select_transport(company(),saved={'preproxy_host':'127.0.0.1','preproxy_port':45678,'preproxy_protocol':'HTTP'},local_discovery=lambda:scan(evidence(client()),'MIXED'))
    assert settings['preproxy_protocol']=='HTTP'


def test_existing_socks_protocol_preserved_when_endpoint_is_mixed():
    settings,_=select_transport(company(),saved={'preproxy_host':'127.0.0.1','preproxy_port':45678,'preproxy_protocol':'SOCKS5'},local_discovery=lambda:scan(evidence(client()),'MIXED'))
    assert settings['preproxy_protocol']=='SOCKS5'


def test_explicit_socks_mode_preserved_when_endpoint_is_mixed():
    assert choose(scan(evidence(client()),'MIXED'),'SOCKS5_REQUIRED')['preproxy_protocol']=='SOCKS5'


def test_clash_verge_kernel_exact_alias_is_discovered():
    data=scan(evidence(client(port=7897,name='verge-mihomo.exe')),'MIXED')
    assert data['status']=='READY'
    transport=choose(data)
    assert transport['preproxy_port']==7897
    assert transport['local_proxy_owner']['name']=='verge-mihomo.exe'


@pytest.mark.parametrize('name',['clash-verge.exe','verge-mihomo-helper.exe','ordinary-server.exe'])
def test_verge_ui_or_similar_name_does_not_grant_proxy_ownership(name):
    assert not scan(evidence(client(port=7897,name=name)))['candidates']
