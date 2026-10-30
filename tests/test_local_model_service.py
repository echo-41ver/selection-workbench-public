import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import threading

import pytest

from selection_automation import local_model_service as service
from selection_automation.stage3_provider import settings


@pytest.fixture(autouse=True)
def no_native_service_unless_explicitly_tested(monkeypatch):
    monkeypatch.setattr(service,'_probe_native',lambda base:None)


@pytest.fixture
def local_server(monkeypatch):
    calls = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def reply(self, body):
            data = json.dumps(body).encode()
            self.send_response(200)
            self.send_header('Content-Length', str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            calls.append((self.command, self.path))
            assert self.path == '/api/tags'
            self.reply({'models': [{'name': name, 'size': 100} for name in
                ('actual-vision', 'actual-vision', 'text-only', 'cloud-vision')]})

        def do_POST(self):
            calls.append((self.command, self.path))
            assert self.path == '/api/show'
            model = json.loads(self.rfile.read(int(self.headers['Content-Length'])))['model']
            self.reply({'capabilities': ['completion'] + ([] if model == 'text-only' else ['vision']),
                        **({'remote_host': 'https://cloud.invalid'} if model == 'cloud-vision' else {})})

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    address = f'http://127.0.0.1:{server.server_address[1]}'
    probe = service._probe
    monkeypatch.setattr(service, '_probe', lambda base: probe(base) if base == address else None)
    monkeypatch.setattr(service, '_endpoints', lambda: [address])
    monkeypatch.setattr(service, '_installed', lambda: None)
    monkeypatch.setattr(service, '_start', lambda root: pytest.fail('Running service must not be restarted'))
    yield address, calls
    server.shutdown()
    server.server_close()
    thread.join(2)


def test_legacy_sample_is_replaced_with_real_vision_model_and_protocol(tmp_path, local_server):
    address, calls = local_server
    provider = settings({'mode': 'local_model', 'workers': 77, 'concurrency_mode': 'manual'})
    before = dict(provider)
    result = service.repair_provider(tmp_path, provider)
    assert result['provider']['base_url'] == address + '/v1'
    assert result['provider']['model'] == 'actual-vision'
    assert result['provider']['protocol'] == 'chat_completions'
    assert result['provider']['workers'] == 77
    assert result['provider']['concurrency_mode'] == 'manual'
    assert provider == before
    assert [model['id'] for model in result['models']] == ['actual-vision']
    assert {path for _, path in calls} == {'/api/tags', '/api/show'}


def test_custom_model_and_endpoint_are_not_silently_replaced(tmp_path, local_server):
    provider = settings({'mode': 'local_model', 'base_url': 'http://127.0.0.1:19441/v1',
                         'model': 'my-selected-model', 'protocol': 'chat_completions'})
    before = dict(provider)
    result = service.repair_provider(tmp_path, provider)
    assert result['provider'] is None and provider == before
    assert result['models'][0]['id'] == 'actual-vision'
    assert '保留你指定' in result['message']


def test_changed_port_keeps_the_explicit_model(tmp_path, local_server):
    address, _ = local_server
    provider = settings({'mode': 'local_model', 'base_url': 'http://127.0.0.1:11434/v1',
                         'model': 'actual-vision', 'protocol': 'responses'})
    result = service.repair_provider(tmp_path, provider)
    assert result['provider']['model'] == 'actual-vision'
    assert result['provider']['base_url'] == address + '/v1'
    assert result['provider']['protocol'] == 'chat_completions'


def test_detecting_new_local_mode_can_choose_an_installed_model(tmp_path, local_server):
    provider = settings({'mode': 'local_model', 'base_url': 'http://127.0.0.1:11434/v1'})
    assert service.repair_provider(tmp_path, provider, allow_default=True)['provider']['model'] == 'actual-vision'


def test_stopped_installed_service_is_started_once_without_downloading_models(tmp_path, monkeypatch):
    started = []
    monkeypatch.setattr(service, '_endpoints', lambda: ['http://127.0.0.1:11434'])
    monkeypatch.setattr(service, '_installed', lambda: 'fixture-ollama')
    monkeypatch.setattr(service, '_tags', lambda base: [] if started else None)
    monkeypatch.setattr(service, '_probe', lambda base: None)
    monkeypatch.setattr(service, '_start', lambda root: started.append(root) or True)

    def disconnected(*args, **kwargs):
        raise ConnectionRefusedError('fixture')

    monkeypatch.setattr(service.socket, 'create_connection', disconnected)
    result = service.catalog(tmp_path, start=True)
    assert started == [tmp_path] and result['started'] and result['service_connected']
    assert result['models'] == [] and '没有检测到已安装的看图模型' in result['message']


def test_start_throttle_does_not_spawn_repeated_servers(tmp_path, monkeypatch):
    from types import SimpleNamespace
    calls = []
    monkeypatch.setattr(service, '_installed', lambda: 'fixture-ollama')
    monkeypatch.setattr(service.subprocess, 'Popen', lambda args, **kw: calls.append((args, kw)) or SimpleNamespace(pid=123))
    assert service._start(tmp_path)
    assert not service._start(tmp_path)
    assert len(calls) == 1 and calls[0][0] == ['fixture-ollama', 'serve']
    assert calls[0][1]['env']['OLLAMA_HOST'] == '127.0.0.1:11434'


def test_read_only_discovery_does_not_start_missing_service(tmp_path, monkeypatch):
    monkeypatch.setattr(service, '_endpoints', lambda: ['http://127.0.0.1:11434'])
    monkeypatch.setattr(service, '_tags', lambda base: None)
    monkeypatch.setattr(service, '_probe', lambda base: None)
    monkeypatch.setattr(service, '_installed', lambda: None)
    monkeypatch.setattr(service, '_start', lambda root: pytest.fail('No launch during read-only discovery'))
    result = service.catalog(tmp_path)
    assert not result['service_connected'] and not result['models'] and '未安装' in result['message']
