"""Metadata requests must not hold the task mutation lock or delay Stop."""
from concurrent.futures import ThreadPoolExecutor
import http.client
import json
import threading
from types import SimpleNamespace

import web_console as web
from selection_automation import local_model_inventory as inventory, local_model_service as service


def test_slow_metadata_repair_does_not_block_scan_cancel_or_task_stop(tmp_path, monkeypatch):
    entered, release = threading.Event(), threading.Event()
    def repair(root, provider, **kwargs):
        assert kwargs['background'] is True
        entered.set()
        assert release.wait(3)
        return {'models': [], 'provider': None, 'message': 'fixture'}
    monkeypatch.setattr(service, 'repair_provider', repair)
    monkeypatch.setattr(inventory, 'cancel_scan', lambda root: {'running': False, 'status': 'cancelled', 'models': []})
    server = web.LocalConsoleServer(('127.0.0.1', 0), web.Handler)
    server.console = SimpleNamespace(root=tmp_path, action=lambda action,body: {'ok': True, 'action': action})
    thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
    def post(path, body):
        client = http.client.HTTPConnection(*server.server_address, timeout=1)
        try:
            client.request('POST', path, json.dumps(body), {'Content-Type': 'application/json',
                           'Origin': f'http://127.0.0.1:{server.server_port}'})
            response = client.getresponse()
            return response.status, json.loads(response.read())
        finally: client.close()
    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            request = pool.submit(post, '/api/local-models/repair', {'provider': {'mode': 'local_model'}})
            try:
                assert entered.wait(1)
                assert post('/api/local-models/scan', {'action': 'cancel'})[1]['status'] == 'cancelled'
                assert post('/api/stop', {}) == (200, {'ok': True, 'action': 'stop'})
            finally: release.set()
            assert request.result()[0] == 200
    finally:
        release.set(); server.shutdown(); server.server_close(); thread.join()


def test_business_post_still_uses_release_lock(tmp_path):
    class HeldLock:
        def __enter__(self): raise RuntimeError('business route retained mutation lock')
        def __exit__(self, *args): pass
    handler = web.Handler.__new__(web.Handler)
    handler.path = '/api/stop'
    handler.server = SimpleNamespace(release_lock=HeldLock())
    handler._do_POST = lambda: None
    import pytest
    with pytest.raises(RuntimeError, match='retained mutation lock'): handler.do_POST()
