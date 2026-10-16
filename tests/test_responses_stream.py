import json
import sys
from pathlib import Path
import pytest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
import generate_category as generator
from selection_automation.responses_stream import read_responses
from selection_automation.model_stream import ModelStreamError
from selection_automation.rule_provider import settings,request_protocol
from test_model_stream import stream,Response


def completed(status='completed',**extra):
    return {'id':'resp_test','status':status,'output':[
        {'type':'reasoning','summary':[{'text':'private'}]},
        {'type':'message','role':'assistant','content':[{'type':'output_text','text':'{"ok":true}'}]}],
        'usage':{'input_tokens':20,'output_tokens':10,'total_tokens':30},**extra}


def test_responses_created_thinking_and_output_are_distinct():
    updates=[]
    s=stream({'type':'response.created','response':{'id':'resp_test','status':'in_progress'}},
        {'type':'response.reasoning_summary_text.delta','delta':'private'},
        {'type':'response.output_text.delta','delta':'{"ok":true}'},
        {'type':'response.completed','response':completed()},done=False)
    result=read_responses(s,lambda **fields:updates.append(fields))
    assert json.loads(result['choices'][0]['message']['content'])=={'ok':True}
    assert 'private' not in json.dumps(result) and 'private' not in json.dumps(updates)
    assert updates[-1]['model_usage']['total_tokens']==30 and updates[-1]['response_id']=='resp_test'
    assert updates[-1]['characters']==11


def test_responses_requires_completed_event_even_when_partial_json_looks_complete():
    with pytest.raises(ModelStreamError) as exc:
        read_responses(stream({'type':'response.output_text.delta','delta':'{"ok":true}'},done=True))
    assert exc.value.code=='MODEL_STREAM_INTERRUPTED' and '计费' in str(exc.value)


def test_responses_json_fallback_and_token_limit():
    r=Response(json.dumps(completed()).encode());r.headers={'Content-Type':'application/json'}
    assert read_responses(r)['choices'][0]['finish_reason']=='stop'
    r=stream({'type':'response.incomplete','response':completed('incomplete',incomplete_details={'reason':'max_output_tokens'})})
    assert read_responses(r)['choices'][0]['finish_reason']=='length'


@pytest.mark.parametrize('event',[{'type':'error','message':'service refused'},
    {'type':'response.failed','response':{'error':{'message':'service refused'}}}])
def test_responses_provider_errors_remain_errors(event):
    with pytest.raises(ModelStreamError,match='service refused'):read_responses(stream(event))


@pytest.mark.parametrize('base,model,protocol,url',[
    ('https://api.daitu.cc','gpt-6.1-sol','responses','https://api.daitu.cc/v1/responses'),
    ('https://api.daitu.cc/v1','gpt-6.1-sol','responses','https://api.daitu.cc/v1/responses'),
    ('https://api.daitu.cc','claude-test','chat_completions','https://api.daitu.cc/chat/completions'),
    ('https://other.example/v1/responses','gpt-test','responses','https://other.example/v1/responses'),
    ('https://other.example/v1','test-model','chat_completions','https://other.example/v1/chat/completions'),
    ('https://api.deepseek.com','deepseek-flash','chat_completions','https://api.deepseek.com/chat/completions')])
def test_protocol_is_selected_before_sending_and_never_by_paid_retry(base,model,protocol,url):
    assert request_protocol(settings(base,model))==(protocol,url)


def test_native_gpt_request_keeps_model_and_uses_low_effort(monkeypatch):
    monkeypatch.setenv('DEEPSEEK_API_KEY','TEST_ONLY_KEY')
    monkeypatch.setenv('SELECTION_RULES_BASE_URL','https://api.daitu.cc')
    monkeypatch.setenv('SELECTION_RULES_MODEL','gpt-6.1-sol')
    calls=[]
    def send(request,**kw):calls.append(request);return stream({'type':'response.completed','response':completed()})
    monkeypatch.setattr(generator.urllib.request,'urlopen',send)
    assert generator.model_json('test')=={'ok':True}
    body=json.loads(calls[0].data)
    assert body['model']=='gpt-6.1-sol' and body['reasoning']=={'effort':'low'}
    assert body['store'] is False and body['max_output_tokens']==16000
    assert 'messages' not in body and 'thinking' not in body and len(calls)==1
    assert calls[0].full_url=='https://api.daitu.cc/v1/responses'


def test_real_http_native_responses(monkeypatch):
    from http.server import ThreadingHTTPServer,BaseHTTPRequestHandler
    import threading
    calls=[]
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            calls.append((self.path,json.loads(self.rfile.read(int(self.headers['Content-Length'])))))
            self.send_response(200);self.send_header('Content-Type','text/event-stream');self.end_headers()
            self.wfile.write(stream({'type':'response.completed','response':completed()}).getvalue())
        def log_message(self,*args):pass
    server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    monkeypatch.setenv('DEEPSEEK_API_KEY','TEST_ONLY_KEY')
    monkeypatch.setenv('SELECTION_RULES_BASE_URL',f'http://127.0.0.1:{server.server_port}/v1/responses')
    monkeypatch.setenv('SELECTION_RULES_MODEL','test-model')
    try:assert generator.model_json('test')=={'ok':True}
    finally:server.shutdown();server.server_close();thread.join(2)
    assert calls[0][0]=='/v1/responses' and len(calls)==1
