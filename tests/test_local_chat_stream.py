from types import SimpleNamespace as N
import pytest
from selection_automation.stage3_provider import VisualClient


def chunk(text=None, finish=None, usage=None):
    return N(id='local-response',usage=usage,
             choices=[] if usage else [N(index=0,delta=N(content=text),finish_reason=finish)])


class Stream:
    def __init__(self, values):self.values=values;self.closed=False
    def __enter__(self):return self
    def __exit__(self,*args):self.closed=True
    def __iter__(self):return iter(self.values)


def client(values):
    stream=Stream(values);calls=[]
    def create(**kwargs):calls.append(kwargs);return stream
    instance=VisualClient.__new__(VisualClient)
    instance.openai=N(chat=N(completions=N(create=create)))
    return instance,stream,calls


def test_long_local_response_collects_chunks_and_final_usage_without_reposting():
    usage=N(total_tokens=20)
    instance,stream,calls=client([chunk(''),chunk('{"result":'),chunk('"same"}'),chunk(finish='stop'),chunk(usage=usage)])
    progress=[]
    response=instance.stream_local_chat({'model':'selected-local-model','max_tokens':1000},lambda **row:progress.append(row))
    assert response.output_text=='{"result":"same"}' and response.status=='completed'
    assert response.usage is usage and stream.closed and len(calls)==1
    assert calls[0]['stream'] is True and calls[0]['stream_options']=={'include_usage':True}
    assert [row['status'] for row in progress]==['receiving','completed']
    assert progress[0]['received_chars']==len('{"result":')
    assert 'content' not in str(progress)


@pytest.mark.parametrize('finish',[None,'tool_calls','content_filter'])
def test_missing_or_invalid_completion_cannot_publish_partial_local_facts(finish):
    instance,stream,calls=client([chunk('{"partial":true}',finish)])
    with pytest.raises(ValueError,match='未收到完成标记'):instance.stream_local_chat({})
    assert stream.closed and len(calls)==1


def test_truncated_response_retains_incomplete_status_for_existing_decoder():
    instance,stream,calls=client([chunk('{"partial":', 'length')])
    response=instance.stream_local_chat({})
    assert response.status=='incomplete' and response.incomplete_details['reason']=='max_output_tokens'
    assert stream.closed and len(calls)==1


def test_stream_transport_failure_is_not_retried():
    instance,stream,calls=client([])
    class Broken:
        def __enter__(self):return self
        def __exit__(self,*args):stream.closed=True
        def __iter__(self):yield chunk('{');raise TimeoutError('idle')
    def create(**kwargs):calls.append(kwargs);return Broken()
    instance.openai.chat.completions.create=create
    with pytest.raises(TimeoutError):instance.stream_local_chat({})
    assert stream.closed and len(calls)==1


def test_real_sdk_accepts_a_local_stream_whose_total_duration_exceeds_read_timeout():
    import json
    import threading
    import time
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from openai import DefaultHttpxClient
    from httpx2 import Timeout
    calls=[]
    class Handler(BaseHTTPRequestHandler):
        def log_message(self,*args):pass
        def do_POST(self):
            calls.append(json.loads(self.rfile.read(int(self.headers['Content-Length']))))
            self.send_response(200);self.send_header('Content-Type','text/event-stream');self.end_headers()
            for text,finish in [('{',None),('"ok"',None),(':',None),('true}',None),(None,'stop')]:
                data={'id':'fixture','object':'chat.completion.chunk','created':1,'model':'fixture',
                      'choices':[{'index':0,'delta':{'content':text} if text else {},'finish_reason':finish}]}
                self.wfile.write(('data: '+json.dumps(data)+'\n\n').encode());self.wfile.flush();time.sleep(.08)
            self.wfile.write(b'data: [DONE]\n\n');self.wfile.flush()
    server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
    worker=threading.Thread(target=server.serve_forever,daemon=True);worker.start()
    timeout=Timeout(.25,connect=1)
    try:
        with DefaultHttpxClient(trust_env=False,timeout=timeout) as transport:
            provider={'mode':'local_model','protocol':'chat_completions',
                      'base_url':f'http://127.0.0.1:{server.server_port}/v1','model':'fixture'}
            instance=VisualClient(provider,transport,'fixture',timeout)
            started=time.monotonic()
            response=instance.create(model='fixture',instructions='Fixture',input=[{'content':[]}],
                max_output_tokens=32,text={'format':{'type':'json_schema','name':'fixture','schema':{'type':'object'}}})
            assert time.monotonic()-started>.25
            assert response.output_text=='{"ok":true}' and response.status=='completed'
            assert len(calls)==1 and calls[0]['stream'] is True
    finally:
        server.shutdown();server.server_close();worker.join()
