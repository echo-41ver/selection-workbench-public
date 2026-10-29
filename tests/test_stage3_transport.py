from contextlib import contextmanager
from types import SimpleNamespace

import pytest

from selection_automation import stage3_transport as transport


@pytest.mark.parametrize('host', ['localhost', '127.0.0.1', '[::1]'])
def test_loopback_never_uses_system_proxy(monkeypatch, host):
    monkeypatch.setattr(transport, 'getproxies', lambda: pytest.fail('Loopback proxy lookup'))
    for mode in ('local_model', 'multimodal_api'):
        assert transport.transport_options({'mode': mode, 'base_url': f'http://{host}:11434/v1'}) == {'trust_env': False}


def test_remote_uses_system_proxy_and_honors_bypass(monkeypatch):
    monkeypatch.setattr(transport, 'getproxies', lambda: {'https': 'http://127.0.0.1:7890'})
    monkeypatch.setattr(transport, 'proxy_bypass', lambda host: host == 'bypass.example')
    assert transport.transport_options({'mode': 'liquid_d1', 'base_url': 'https://api.example/v1'})['proxy'].endswith(':7890')
    assert 'proxy' not in transport.transport_options({'mode': 'multimodal_api', 'base_url': 'https://bypass.example/v1'})


def clients(monkeypatch, failures):
    from httpx2 import ConnectError
    import openai
    created, calls = [], []
    class Client:
        def __init__(self, **kw):
            self.options, self.closed = kw, False
            self.index = len(created)
            created.append(self)
        @contextmanager
        def stream(self, method, url, **kw):
            calls.append((method, url, kw))
            if failures[self.index]:
                raise ConnectError('fixture unreachable')
            yield SimpleNamespace(status_code=403)
        def close(self):
            self.closed = True
        def __enter__(self):
            return self
        def __exit__(self, *args):
            self.close()
    monkeypatch.setattr(openai, 'DefaultHttpxClient', Client)
    monkeypatch.setattr(transport, 'transport_options', lambda p: {'trust_env': False, 'proxy': 'http://proxy.invalid'})
    return created, calls


def test_bad_proxy_recovers_before_any_inference_or_credentials(monkeypatch):
    created, calls = clients(monkeypatch, [True, False])
    with transport.create_transport({'mode': 'liquid_d1', 'base_url': 'https://api.example/v1/systemone'}, timeout=600, limits=None) as selected:
        assert selected is created[1] and created[0].closed and not selected.closed
        assert 'proxy' not in selected.options
    assert selected.closed
    assert len(calls) == 2
    assert all(method == 'GET' and 'headers' not in kw and not kw.get('follow_redirects') for method, url, kw in calls)


def test_http_denial_does_not_switch_routes_or_retry_inference(monkeypatch):
    created, calls = clients(monkeypatch, [False])
    with transport.create_transport({'mode': 'multimodal_api', 'base_url': 'https://api.example/v1'}, timeout=600, limits=None) as selected:
        assert selected is created[0]
    assert len(calls) == len(created) == 1


def test_incremental_passes_reuse_route_within_same_controller(monkeypatch, tmp_path):
    created, calls = clients(monkeypatch, [False, True, False])
    route_file = tmp_path / 'transport-route.json'
    provider = {'mode': 'multimodal_api', 'base_url': 'https://api.example/v1'}
    for scope in ([123, 10.0], [123, 10.0], [456, 20.0]):
        with transport.create_transport(provider, timeout=600, limits=None,
                                        route_file=route_file, route_scope=scope):
            pass
    assert len(created) == 3
    assert len(calls) == 2
    assert all(client.closed for client in created)
    assert 'proxy.invalid' not in route_file.read_text('utf-8')


def test_selected_direct_route_survives_incremental_passes(monkeypatch, tmp_path):
    created, calls = clients(monkeypatch, [True, False, True])
    provider = {'mode': 'multimodal_api', 'base_url': 'https://api.example/v1'}
    for _ in range(2):
        with transport.create_transport(provider, timeout=600, limits=None,
                                        route_file=tmp_path / 'transport-route.json',
                                        route_scope=[123, 10.0]) as selected:
            assert 'proxy' not in selected.options
    assert len(created) == 3 and len(calls) == 2


def test_both_routes_unavailable_close_clients_before_model_request(monkeypatch):
    created, calls = clients(monkeypatch, [True, True])
    with pytest.raises(ConnectionError, match='尚未发送分析'):
        with transport.create_transport({'mode': 'liquid_d1', 'base_url': 'https://api.example/v1'}, timeout=600, limits=None):
            pytest.fail('Model dispatch must not start')
    assert len(calls) == 2 and all(client.closed for client in created)


def test_local_service_sends_no_preflight_inference(monkeypatch):
    created, calls = clients(monkeypatch, [])
    with transport.create_transport({'mode': 'local_model', 'base_url': 'http://127.0.0.1:11434/v1'}, timeout=600, limits=None) as selected:
        assert selected is created[0]
    assert not calls


def test_native_d1_survives_server_closing_a_reused_connection():
    import socket
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from httpx2 import Limits

    class Handler(BaseHTTPRequestHandler):
        protocol_version = 'HTTP/1.1'

        def do_GET(self):
            if getattr(self, 'served', False):
                self.close_connection = True
                self.connection.shutdown(socket.SHUT_RDWR)
                return
            self.served = True
            self.send_response(200)
            self.send_header('Content-Length', '2')
            self.end_headers()
            self.wfile.write(b'OK')

        def log_message(self, *args):
            pass

    with ThreadingHTTPServer(('127.0.0.1', 0), Handler) as server:
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = f'http://127.0.0.1:{server.server_port}'
        provider = {'mode': 'local_model', 'protocol': 'decisions', 'base_url': base+'/v1/decide'}
        try:
            with transport.create_transport(provider, timeout=3, limits=Limits(max_connections=1)) as client:
                assert client.get(base+'/health').text == 'OK'
                assert client.get(base+'/health').text == 'OK'
        finally:
            server.shutdown()
            thread.join(timeout=2)
