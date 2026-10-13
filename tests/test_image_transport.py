import ssl
import threading
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
from concurrent.futures import ThreadPoolExecutor
import pytest
from selection_workflow.image_transport import ImageTransport,MAX_IMAGE_CONNECTIONS


@pytest.fixture
def server():
    state={'ports':set(),'calls':0}
    class Handler(BaseHTTPRequestHandler):
        protocol_version='HTTP/1.1'
        def do_GET(self):
            state['ports'].add(self.client_address[1]);state['calls']+=1
            body=b'image-test-bytes';self.send_response(403 if self.path=='/denied' else 200)
            self.send_header('Content-Length',str(len(body)));self.end_headers();self.wfile.write(body)
        def log_message(self,*args):pass
    httpd=ThreadingHTTPServer(('127.0.0.1',0),Handler)
    thread=threading.Thread(target=httpd.serve_forever,daemon=True);thread.start()
    yield f'http://127.0.0.1:{httpd.server_port}',state
    httpd.shutdown();httpd.server_close();thread.join(timeout=2)


def test_sequential_images_reuse_connection_and_keep_verification(server):
    url,state=server;transport=ImageTransport(timeout=2)
    try:
        assert transport.read(url+'/a')==transport.read(url+'/b')==b'image-test-bytes'
        assert state['calls']==2 and len(state['ports'])==1
        client=transport._client(None)
        assert client._trust_env is False
        assert client._transport._pool._ssl_context.verify_mode==ssl.CERT_REQUIRED
        assert client._transport._pool._ssl_context.check_hostname
    finally:transport.close()


def test_http_denial_is_not_retried_or_routed_elsewhere(server):
    url,state=server;transport=ImageTransport(timeout=2)
    try:
        with pytest.raises(Exception):transport.read(url+'/denied')
        assert state['calls']==1
        assert list(transport._clients)==[None]
    finally:transport.close()


def test_parallel_connections_are_bounded_and_all_results_return(server):
    url,state=server;transport=ImageTransport(timeout=5)
    try:
        with ThreadPoolExecutor(max_workers=32) as pool:
            results=list(pool.map(transport.read,[url+'/image']*40))
        assert results==[b'image-test-bytes']*40
        assert state['calls']==40 and len(state['ports'])<=MAX_IMAGE_CONNECTIONS
    finally:transport.close()
