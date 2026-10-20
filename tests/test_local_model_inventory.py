import json
from pathlib import Path
import threading
import time
from types import SimpleNamespace

from selection_automation import local_model_inventory as inventory
from selection_automation import local_model_service as service


def test_non_http_listener_is_ignored_without_aborting_other_model_services(monkeypatch):
    from http.client import BadStatusLine
    import pytest

    class InvalidResponse:
        def open(self, *args, **kwargs):
            raise BadStatusLine('fixture authentication protocol')

    monkeypatch.setattr(service, 'build_opener', lambda *args: InvalidResponse())
    with pytest.raises(OSError, match='valid HTTP metadata'):
        service._json('http://127.0.0.1:19761', '/v1/models')
    assert service._probe('http://127.0.0.1:19761') is None


def test_process_listener_discovery_covers_nonstandard_ports_without_lan_scans(monkeypatch):
    connections = [SimpleNamespace(status='LISTEN', laddr=SimpleNamespace(ip=host, port=port))
                   for host, port in [('0.0.0.0', 19761), ('::', 19762), ('127.0.0.2', 19763), ('192.0.2.2', 19764)]]
    monkeypatch.setattr(inventory.psutil, 'net_connections', lambda kind: connections)
    assert inventory.service_endpoints() == ['http://127.0.0.1:11434', 'http://127.0.0.1:1234',
        'http://127.0.0.1:19761', 'http://[::1]:19762', 'http://127.0.0.2:19763']


def test_native_metadata_confirms_vision_but_model_name_does_not(monkeypatch):
    calls = []
    responses = {
        '/api/v1/models': {'models': [{'key': 'actual-image', 'capabilities': {'vision': True}},
                                     {'key': 'named-vision-but-unknown'},
                                     {'key': 'text', 'capabilities': {'vision': False}},
                                     {'key': 'remote', 'remote_host': 'https://remote.invalid', 'capabilities': {'vision': True}}]}}

    def read(base, path, payload=None):
        calls.append((path, payload))
        if path not in responses:
            raise OSError('not this protocol')
        return responses[path]

    monkeypatch.setattr(service, '_json', read)
    found = service._probe('http://127.0.0.1:19761')
    ready, unknown = service._api_models(found)
    assert [row['id'] for row in ready] == ['actual-image']
    assert [(row['id'], row['vision']) for row in unknown] == [('named-vision-but-unknown', None), ('text', False)]
    assert all(payload is None for _, payload in calls)
    assert not any('completions' in path or 'chat' in path for path, _ in calls)


def test_llama_props_do_not_load_models_or_apply_one_models_vision_to_others(monkeypatch):
    paths = []

    def read(base, path, payload=None):
        paths.append(path)
        assert payload is None
        return {'modalities': {'vision': True}}

    monkeypatch.setattr(service, '_json', read)
    endpoint = {'base_url': 'http://127.0.0.1:19761', 'source': 'OpenAI 兼容本地服务', 'rows': [{'id': 'one'}]}
    ready, unknown = service._api_models(endpoint)
    assert ready[0]['source'] == 'llama.cpp' and not unknown
    endpoint['rows'].append({'id': 'two'})
    ready, unknown = service._api_models(endpoint)
    assert not ready and len(unknown) == 2
    assert set(paths) == {'/props?autoload=false'}


def test_file_inventory_checks_real_signatures_and_does_not_import_weights(tmp_path):
    (tmp_path / 'real.gguf').write_bytes(b'GGUF' + b'fixture')
    (tmp_path / 'fake.gguf').write_bytes(b'not a model')
    (tmp_path / 'mmproj.gguf').write_bytes(b'GGUF')
    (tmp_path / 'config.json').write_text(json.dumps({'model_type': 'fixture', 'vision_config': {}}))
    (tmp_path / 'model.safetensors').write_bytes(b'fixture; must not be loaded')
    rows = inventory.inspect_directory(tmp_path, [p.name for p in tmp_path.iterdir()])
    assert len(rows) == 2
    assert {row['source'] for row in rows} == {'Hugging Face / Transformers', 'GGUF'}
    assert all(row['status'] == 'files_only' and 'base_url' not in row for row in rows)


def test_orphan_ollama_manifests_are_not_presented_as_installed_weights(tmp_path):
    folder = tmp_path / 'manifests/registry.ollama.ai/library/fixture'
    folder.mkdir(parents=True)
    blobs = tmp_path / 'blobs'
    blobs.mkdir()
    manifest = {'layers': [{'mediaType': 'application/vnd.ollama.image.model', 'digest': 'sha256:' + 'a' * 64}]}
    (folder / 'latest').write_text(json.dumps(manifest))
    rows = inventory.inspect_directory(folder, ['latest'])
    assert rows[0]['status'] == 'incomplete_files'
    (blobs / ('sha256-' + 'a' * 64)).write_bytes(b'fixture')
    assert inventory.inspect_directory(folder, ['latest'])[0]['status'] == 'files_only'


def test_unrelated_system_manifests_are_not_read_as_model_manifests(tmp_path, monkeypatch):
    folder = tmp_path / 'Windows/WinSxS/Manifests'
    folder.mkdir(parents=True)
    (folder / 'component.manifest').write_text('fixture system manifest')
    monkeypatch.setattr(inventory, '_read_json', lambda path: (_ for _ in ()).throw(AssertionError('Not a model store')))
    assert inventory.inspect_directory(folder, ['component.manifest']) == []


def wait_complete(root):
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        result = inventory.scan_status(root)
        if not result['running']:
            return result
        time.sleep(.01)
    raise AssertionError('Scan did not finish')


def test_scanner_deduplicates_prioritized_roots_and_reports_unreadable_directories(tmp_path, monkeypatch):
    disk = tmp_path / 'disk'
    model = disk / 'models'
    denied = disk / 'denied'
    model.mkdir(parents=True)
    denied.mkdir()
    (model / 'real.gguf').write_bytes(b'GGUF')
    real_scandir = inventory.os.scandir

    def scandir(path):
        if Path(path) == denied:
            raise PermissionError('fixture')
        return real_scandir(path)

    monkeypatch.setattr(inventory.os, 'scandir', scandir)
    monkeypatch.setattr(inventory, 'scan_roots', lambda: [model, disk])
    monkeypatch.setattr(service, 'catalog', lambda root, **kwargs: {'models': [], 'candidates': []})
    first = inventory.start_scan(tmp_path)
    assert inventory.start_scan(tmp_path)['scan_id'] == first['scan_id']
    result = wait_complete(tmp_path)
    assert result['status'] == 'complete' and result['skipped'] == 1
    assert len(result['models']) == 1 and result['directories'] == 2
    assert inventory.start_scan(tmp_path)['scan_id'] == first['scan_id']
    assert inventory.start_scan(tmp_path, restart=True)['scan_id'] != first['scan_id']
    wait_complete(tmp_path)


def test_background_scan_can_be_cancelled_without_erasing_discovered_results(tmp_path, monkeypatch):
    entered, release = threading.Event(), threading.Event()

    def slow_roots():
        entered.set()
        release.wait(2)
        return [tmp_path]

    monkeypatch.setattr(inventory, 'scan_roots', slow_roots)
    monkeypatch.setattr(service, 'catalog', lambda root, **kwargs: {'models': []})
    started = inventory.start_scan(tmp_path)
    assert entered.wait(1)
    inventory.cancel_scan(tmp_path)
    release.set()
    result = wait_complete(tmp_path)
    assert result['status'] == 'cancelled' and result['scan_id'] == started['scan_id']


def test_other_listeners_do_not_prevent_starting_stopped_ollama(tmp_path, monkeypatch):
    started = []
    monkeypatch.setattr(service, '_endpoints', lambda: ['http://127.0.0.1:11434', 'http://127.0.0.1:8765'])
    monkeypatch.setattr(service, '_probe', lambda base: {'base_url': base, 'source': '需授权的本地服务', 'rows': [], 'authentication_required': True})
    monkeypatch.setattr(service, '_installed', lambda: 'fixture')
    monkeypatch.setattr(service, '_start', lambda root: started.append(root) or True)
    monkeypatch.setattr(service, '_tags', lambda base: [] if started else None)

    def connect(address, timeout):
        assert address == ('127.0.0.1', 11434)
        raise ConnectionRefusedError('fixture')

    monkeypatch.setattr(service.socket, 'create_connection', connect)
    result = service.catalog(tmp_path, start=True)
    assert started == [tmp_path] and result['started'] and result['service_connected']
