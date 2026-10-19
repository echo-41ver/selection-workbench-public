import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
import threading
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT/'scripts'))
sys.path.insert(0, str(ROOT/'src'))
from selection_automation.rule_provider import settings
import generate_category as generator
import web_console as web


@pytest.mark.parametrize('address', ['1234', 'https://user:pass@fixture/v1',
    'https://example.com/v1?key=private', 'http://localhost:99999/v1', 'https://example.com/#key'])
def test_invalid_address_rejected(address):
    with pytest.raises(ValueError):settings(address, 'test-model')


def test_custom_service_requires_explicit_model_and_key(tmp_path, monkeypatch):
    with pytest.raises(ValueError, match='模型名'):settings('http://localhost:8000/v1')
    console = web.Console(tmp_path)
    monkeypatch.setattr(console, 'saved_credential', lambda: 'saved-company-key')
    with pytest.raises(web.ConsoleError, match='不会转发'):
        console.start({'category_name': '钢笔', 'amazon_count': 20, 'per_keyword': 200,
            'api_key': '', 'api_base_url': 'http://localhost:8000/v1', 'api_model': 'test-model'})
    assert not console.busy and not list(tmp_path.rglob('pending-generation.json'))


def test_actual_http_request_uses_selected_endpoint_model_and_standard_payload(monkeypatch):
    received = []
    client_agents = []
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            client_agents.append(self.headers.get('User-Agent'))
            received.append((self.path, self.headers['Authorization'],
                json.loads(self.rfile.read(int(self.headers['Content-Length'])))))
            self.send_response(200);self.send_header('Content-Type', 'application/json');self.end_headers()
            self.wfile.write(json.dumps({'choices': [{'finish_reason': 'stop',
                'message': {'content': '{"success":true}'}}]}).encode())
        def log_message(self, *args):pass
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True);thread.start()
    monkeypatch.setenv('SELECTION_RULES_BASE_URL', f'http://127.0.0.1:{server.server_port}/v1/chat/completions')
    monkeypatch.setenv('SELECTION_RULES_MODEL', 'test-model')
    monkeypatch.setenv('DEEPSEEK_API_KEY', 'TEST_ONLY_KEY')
    try:assert generator.model_json('test') == {'success': True}
    finally:server.shutdown();server.server_close();thread.join(2)
    path, auth, payload = received[0]
    assert path == '/v1/chat/completions' and auth == 'Bearer TEST_ONLY_KEY'
    assert payload['model'] == 'test-model'
    assert client_agents==['selection-workbench/1.0']
    assert 'thinking' not in payload and 'reasoning_effort' not in payload


@pytest.mark.parametrize('body,expected',[
    (b'error code: 1010\n','客户端标识'),
    (b'{"error":{"code":"MODEL_NOT_ALLOWED","message":"model access denied"}}','MODEL_NOT_ALLOWED'),
    (b'{"message":"echo TEST_ONLY_PRIVATE_KEY and Bearer TEST_ONLY_OTHER"}','密钥已隐藏'),
    (b'<html>blocked TEST_ONLY_PRIVATE_KEY</html>','服务拒绝访问')])
def test_http403_preserves_reason_without_exposing_credentials(monkeypatch,body,expected):
    import io
    from urllib.error import HTTPError
    monkeypatch.setenv('DEEPSEEK_API_KEY','TEST_ONLY_PRIVATE_KEY')
    def fail(*args,**kwargs):raise HTTPError('https://provider.example/chat/completions',403,'Forbidden',{},io.BytesIO(body))
    monkeypatch.setattr(generator.urllib.request,'urlopen',fail)
    with pytest.raises(generator.CategoryGenerationError) as captured:generator.model_json('test')
    assert captured.value.code=='MODEL_HTTP_403' and expected in str(captured.value)
    assert 'TEST_ONLY_PRIVATE_KEY' not in str(captured.value)
    assert 'TEST_ONLY_OTHER' not in str(captured.value)
    assert '<html>' not in str(captured.value)


def test_web_generation_keeps_provider_for_edits_without_persisting_key(tmp_path, monkeypatch):
    console = web.Console(tmp_path);calls=[]
    def invoke(args, **kwargs):
        calls.append((args, dict(kwargs['env'])))
        if 'generate_category.py' in args[0]:
            category=json.loads((ROOT/'config/categories/power_drill.json').read_text('utf-8'))
            category['amazon']['target_count']=1
            Path(args[args.index('--output')+1]).write_text(json.dumps(category))
        elif len(args)>1 and 'collect_amazon_search_frontend.mjs' in args[1]:
            category=json.loads(Path(args[args.index('--config')+1]).read_text('utf-8'))
            Path(args[args.index('--output')+1]).write_text(json.dumps({'records':[
                {'asin':'B000000001','rank':1,'title':'Cordless power drill kit'}], 'record_count':1,
                'source_url':category['amazon']['bestsellers_url'],'category_key':category['category_key']}))
        return SimpleNamespace(returncode=0)
    monkeypatch.setattr(console, 'invoke', invoke)
    monkeypatch.setattr(console, 'explain_file', lambda source,*args: source)
    console._start({'category_name':'测试', 'amazon_count':1, 'per_keyword':5,
        'api_key':'TEST_ONLY_CUSTOM_KEY', 'api_base_url':'http://127.0.0.1:8000/v1','api_model':'test-model'})
    assert console.draft and console.draft_provider==settings('http://127.0.0.1:8000/v1','test-model')
    assert calls[0][1]['SELECTION_RULES_MODEL']=='test-model'
    assert calls[0][1]['SELECTION_RULES_BASE_URL']=='http://127.0.0.1:8000/v1'
    assert 'DEEPSEEK_API_KEY' not in calls[1][1]
    for path in tmp_path.rglob('*'):
        if path.is_file():assert 'TEST_ONLY_CUSTOM_KEY' not in path.read_text('utf-8')
    restored=web.Console(tmp_path)
    assert restored.draft_provider==console.draft_provider and restored.draft_key is None
    console.progress_file=tmp_path/'progress.json'
    console.progress_file.write_text('{"stage":"dependencies","message":"正在安装浏览器"}',encoding='utf-8')
    assert console.generation_progress()['stage']=='dependencies'
