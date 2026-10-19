import http.client
import ssl
import urllib.error
import urllib.request
from types import SimpleNamespace
import pytest
from selection_automation import model_transport as transport


def setup(monkeypatch,failures,after_connect=None,proxy=True):
    routes=[];sent=[];timeouts=[]
    monkeypatch.setattr(transport.urllib.request,'getproxies',lambda:{'https':'http://127.0.0.1:7890'} if proxy else {})
    monkeypatch.setattr(transport.urllib.request,'proxy_bypass',lambda host:False)
    def connect(connection):
        timeouts.append(connection.timeout)
        if len(timeouts)<=failures:raise ssl.SSLEOFError('offline TLS failure')
        connection.sock=SimpleNamespace(settimeout=lambda value:None,close=lambda:None)
    monkeypatch.setattr(http.client.HTTPSConnection,'connect',connect)
    def build(proxy_handler,handler):
        routes.append(proxy_handler.proxies)
        def opened(request,timeout):
            def send(factory,request,**kwargs):
                connection=factory('models.example',timeout=timeout)
                connection.connect()
                sent.append(request.data)
                if after_connect:raise after_connect
                return SimpleNamespace(status=200)
            handler.do_open=send
            return handler.https_open(request)
        return SimpleNamespace(open=opened)
    monkeypatch.setattr(transport.urllib.request,'build_opener',build)
    return routes,sent,timeouts


def test_tls_handshake_failure_switches_proxy_to_direct_before_one_post(monkeypatch):
    routes,sent,timeouts=setup(monkeypatch,1)
    events=[];request=urllib.request.Request('https://models.example/v1/chat/completions',data=b'one paid request')
    assert transport.open_model_url(request,timeout=600,notify=lambda **fields:events.append(fields)).status==200
    assert routes==[{'https':'http://127.0.0.1:7890'},{}] and sent==[request.data]
    assert timeouts==[12,12] and any(event.get('model_network',{}).get('request_sent') is False for event in events)


@pytest.mark.parametrize('error',[ssl.SSLEOFError('after TLS'),urllib.error.URLError('read failed'),
    urllib.error.HTTPError('https://models.example',503,'busy',{},None)])
def test_any_failure_after_tls_connect_is_not_replayed(monkeypatch,error):
    routes,sent,_=setup(monkeypatch,0,error)
    with pytest.raises(type(error)):transport.open_model_url(urllib.request.Request('https://models.example',data=b'paid'))
    assert len(routes)==len(sent)==1


def test_two_failed_connects_are_bounded_and_send_no_body(monkeypatch):
    routes,sent,_=setup(monkeypatch,2)
    with pytest.raises(ssl.SSLEOFError):transport.open_model_url(urllib.request.Request('https://models.example',data=b'paid'))
    assert len(routes)==2 and not sent


def test_direct_only_connection_retries_one_pre_request_tls_failure(monkeypatch):
    routes,sent,_=setup(monkeypatch,1,proxy=False)
    transport.open_model_url(urllib.request.Request('https://models.example',data=b'paid'))
    assert routes==[{},{}] and sent==[b'paid']


def test_proxy_mutation_does_not_carry_over_to_direct_request(monkeypatch):
    setup(monkeypatch,1)
    original=transport.urllib.request.build_opener;hosts=[]
    def build(proxy,handler):
        opener=original(proxy,handler);send=opener.open
        def opened(request,timeout):
            hosts.append(request.host)
            if proxy.proxies:request.set_proxy('127.0.0.1:1','http')
            return send(request,timeout)
        opener.open=opened;return opener
    monkeypatch.setattr(transport.urllib.request,'build_opener',build)
    request=urllib.request.Request('https://models.example',data=b'paid')
    transport.open_model_url(request)
    assert hosts==['models.example','models.example'] and request.host=='models.example'
