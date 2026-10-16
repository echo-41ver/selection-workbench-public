import io
import http.server
import json
from pathlib import Path
import sys
import threading
from urllib.error import URLError

import pytest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
import generate_category as generator
from selection_automation.model_stream import read_chat_completion,ModelStreamError


class Response(io.BytesIO):
    headers={'Content-Type':'text/event-stream; charset=utf-8'}
    def __enter__(self):return self
    def __exit__(self,*args):self.close()


def stream(*chunks,done=True):
    data=': heartbeat\n\n'+''.join('data: '+json.dumps(packet,ensure_ascii=False)+'\n\n' for packet in chunks)
    return Response((data+('data: [DONE]\n\n' if done else '')).encode())


def chunk(content='',finish=None,**delta):
    return {'choices':[{'index':0,'delta':{'content':content,**delta},'finish_reason':finish}]}


def test_real_stream_assembles_only_answer_and_reports_progress():
    updates=[]
    response=stream(chunk(reasoning_content='private reasoning'),chunk('{"结果":'),chunk('true}',finish='stop'))
    result=read_chat_completion(response,lambda **fields:updates.append(fields))
    assert json.loads(result['choices'][0]['message']['content'])=={'结果':True}
    assert 'private reasoning' not in json.dumps(result)
    assert updates[-1]=={'events':3,'characters':11}


def test_complete_json_in_unfinished_stream_is_not_published():
    with pytest.raises(ModelStreamError) as captured:read_chat_completion(stream(chunk('{"ok":true}'),done=False))
    assert captured.value.code=='MODEL_STREAM_INTERRUPTED'
    assert read_chat_completion(stream(chunk('{"ok":true}',finish='stop'),done=False))['choices'][0]['finish_reason']=='stop'


def test_stream_deadline_is_bounded(monkeypatch):
    import selection_automation.model_stream as module
    monkeypatch.setattr(module.time,'monotonic',lambda:601)
    with pytest.raises(ModelStreamError) as captured:read_chat_completion(stream(chunk('data')),started=0)
    assert captured.value.code=='MODEL_RESPONSE_TIMEOUT'


def test_model_request_uses_stream_and_long_read_window_without_changing_provider(monkeypatch):
    monkeypatch.setenv('DEEPSEEK_API_KEY','TEST_ONLY_KEY')
    monkeypatch.setenv('SELECTION_RULES_BASE_URL','https://provider.example/v1')
    monkeypatch.setenv('SELECTION_RULES_MODEL','test-model')
    calls=[];updates=[]
    monkeypatch.setattr(generator,'MODEL_PROGRESS_REPORTER',lambda **fields:updates.append(fields))
    def call(request,**kwargs):
        calls.append((request,kwargs));return stream(chunk('{"ok":true}',finish='stop'))
    monkeypatch.setattr(generator.urllib.request,'urlopen',call)
    assert generator.model_json('test')=={'ok':True}
    payload=json.loads(calls[0][0].data)
    assert payload['stream'] is True and payload['model']=='test-model'
    assert calls[0][0].full_url=='https://provider.example/v1/chat/completions'
    assert calls[0][1]['timeout']==600 and len(calls)==1
    assert updates[0]['model_waiting'] is True and updates[-1]['model_waiting'] is False


@pytest.mark.parametrize('error,code',[(TimeoutError('read operation timed out'),'MODEL_RESPONSE_TIMEOUT'),
    (URLError(TimeoutError('connect timed out')),'MODEL_CONNECT_TIMEOUT'),
    (URLError('DNS lookup failed'),'MODEL_NETWORK_ERROR')])
def test_timeouts_and_network_failures_are_distinct_and_never_retried(monkeypatch,error,code):
    monkeypatch.setenv('DEEPSEEK_API_KEY','TEST_ONLY_KEY');calls=[]
    def fail(*args,**kwargs):calls.append(True);raise error
    monkeypatch.setattr(generator.urllib.request,'urlopen',fail)
    with pytest.raises(generator.CategoryGenerationError) as captured:generator.model_json('test')
    assert captured.value.code==code and len(calls)==1


def test_stream_service_error_cannot_echo_key(monkeypatch):
    key='TEST_ONLY_PRIVATE_KEY';monkeypatch.setenv('DEEPSEEK_API_KEY',key)
    monkeypatch.setattr(generator.urllib.request,'urlopen',lambda *a,**k:stream({'error':{'message':'blocked '+key}}))
    with pytest.raises(generator.CategoryGenerationError) as captured:generator.model_json('test')
    assert captured.value.code=='MODEL_STREAM_SERVICE_ERROR' and key not in str(captured.value)


def test_actual_http_stream_is_read_by_production_model_client(monkeypatch):
    received=[]
    class Handler(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            received.append(json.loads(self.rfile.read(int(self.headers['Content-Length']))))
            self.send_response(200);self.send_header('Content-Type','text/event-stream');self.end_headers()
            self.wfile.write(stream(chunk('{"ok":'),chunk('true}',finish='stop')).getvalue());self.wfile.flush()
        def log_message(self,*args):pass
    server=http.server.ThreadingHTTPServer(('127.0.0.1',0),Handler)
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    monkeypatch.setenv('DEEPSEEK_API_KEY','TEST_ONLY_KEY')
    monkeypatch.setenv('SELECTION_RULES_BASE_URL',f'http://127.0.0.1:{server.server_port}/v1')
    monkeypatch.setenv('SELECTION_RULES_MODEL','test-model')
    try:assert generator.model_json('test')=={'ok':True}
    finally:server.shutdown();server.server_close();thread.join(2)
    assert len(received)==1 and received[0]['stream'] is True


def test_cli_progress_preserves_route_while_waiting_and_restores_reporter(tmp_path,monkeypatch):
    monkeypatch.setenv('DEEPSEEK_API_KEY','TEST_ONLY_KEY')
    monkeypatch.setattr(generator.urllib.request,'urlopen',lambda *a,**k:stream(chunk('{"ok":true}',finish='stop')))
    captures=[];write=generator.atomic_write
    def capture(path,value):captures.append(json.loads(value));write(path,value)
    monkeypatch.setattr(generator,'atomic_write',capture)
    def generate(*args,progress,**kwargs):
        progress(stage='generating',route=[{'url':'https://www.amazon.com/zgbs/pc/12879431'}])
        return generator.model_json('test')
    monkeypatch.setattr(generator,'generate_category',generate)
    monkeypatch.setattr(generator.sys,'argv',['generate_category.py','--category-name','键盘','--amazon-count','20',
        '--per-keyword','200','--output',str(tmp_path/'category.json'),'--status-file',str(tmp_path/'status.json')])
    old=generator.MODEL_PROGRESS_REPORTER
    assert generator.main()==0 and generator.MODEL_PROGRESS_REPORTER is old
    waiting=[row for row in captures if row.get('model_waiting')]
    assert waiting and waiting[0]['stage']=='generating' and waiting[0]['route']
    assert 'TEST_ONLY_KEY' not in json.dumps(captures)
