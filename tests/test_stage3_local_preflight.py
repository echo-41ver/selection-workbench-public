import json
import socket
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import web_console as web
from selection_automation.stage3_provider import apply, settings
from selection_automation.stage3_runtime import update_control


@pytest.fixture(autouse=True)
def no_machine_specific_recovery(monkeypatch):
    monkeypatch.setattr('selection_automation.local_model_service.repair_provider',
        lambda *args, **kwargs: {'provider': None, 'message': 'fixture service unavailable'})


def task(tmp_path, mode='multimodal_api'):
    base = tmp_path / 'runs/fixture'
    folder = base / 'stage123'
    folder.mkdir(parents=True)
    category = {'category_key': 'fixture', 'stage3': {'modules': {'design': 100}}}
    apply(category, settings({'mode': mode}))
    web.selection.save(base / 'category.json', category)
    web.selection.save(folder / 'codex_pool_config.json', {'enabled': True, 'workers': 8})
    update_control(folder, paused=True, paused_agents=['doubao'])
    (folder / 'results.jsonl').write_text('{"saved_result":true}\n', encoding='utf-8')
    (folder / 'STOP').write_text('retained stop', encoding='utf-8')
    return base, folder


@pytest.mark.parametrize('mode', ['multimodal_api', 'local_agent'])
@pytest.mark.parametrize('configure_only', [False, True])
@pytest.mark.parametrize('failure', [ConnectionRefusedError, TimeoutError])
def test_unavailable_target_preserves_old_provider_pool_pause_and_results(
        tmp_path, monkeypatch, mode, configure_only, failure):
    base, folder = task(tmp_path, mode)
    before = {path: path.read_bytes() for path in base.rglob('*') if path.is_file()}
    console = web.Console(tmp_path)
    monkeypatch.setattr(console, 'current_run_key', lambda: ('fixture', True))
    monkeypatch.setattr(console, 'stage3_snapshot', lambda key: pytest.fail('No progress scan before preflight'))

    def unavailable(address, timeout):
        assert address == ('127.0.0.1', 18181) and timeout == 2
        raise failure('fixture service unavailable')

    monkeypatch.setattr(web.socket, 'create_connection', unavailable)
    with pytest.raises(web.ConsoleError) as error:
        console.start_stage3({'provider': settings({'mode': 'local_model'})}, configure_only=configure_only)
    assert error.value.code == 'LOCAL_MODEL_UNAVAILABLE' and error.value.status == 409
    assert '127.0.0.1:18181' in error.value.message and '原方案' in error.value.message
    assert {path: path.read_bytes() for path in base.rglob('*') if path.is_file()} == before


def test_resume_of_unavailable_service_keeps_pause_and_checkpoint(tmp_path, monkeypatch):
    base, folder = task(tmp_path, 'local_model')
    before = {path: path.read_bytes() for path in base.rglob('*') if path.is_file()}
    console = web.Console(tmp_path)
    monkeypatch.setattr(console, 'current_run_key', lambda: ('fixture', True))

    def unavailable(*args, **kwargs):
        raise ConnectionRefusedError('fixture')

    monkeypatch.setattr(web.socket, 'create_connection', unavailable)
    with pytest.raises(web.ConsoleError, match='本地模型服务或指定模型尚未就绪'):
        console.control_stage3({'action': 'resume'})
    assert {path: path.read_bytes() for path in base.rglob('*') if path.is_file()} == before


def test_reachable_service_starts_selected_provider_without_probe_inference(tmp_path, monkeypatch):
    base, folder = task(tmp_path)
    console = web.Console(tmp_path)
    monkeypatch.setattr(console, 'current_run_key', lambda: ('fixture', True))
    monkeypatch.setattr(console, 'stage3_snapshot', lambda key: {'total': 12, 'pending': 12})
    monkeypatch.setattr(web, 'codex_review_control', lambda base: {'running': False})
    starts = []
    monkeypatch.setattr(console, 'launch_stage3_controller', lambda *args: starts.append(args))
    with socket.socket() as listener:
        listener.bind(('127.0.0.1', 0))
        listener.listen(1)
        listener.settimeout(2)
        provider = settings({'mode': 'local_model',
            'base_url': f'http://127.0.0.1:{listener.getsockname()[1]}/v1/systemone'})
        result = console.start_stage3({'provider': provider})
        connection, _ = listener.accept()
        with connection:
            assert connection.recv(1) == b''
    assert result['ok'] and len(starts) == 1
    assert json.loads((base / 'category.json').read_text(encoding='utf-8'))['stage3']['provider'] == provider
    assert (folder / 'results.jsonl').read_text(encoding='utf-8') == '{"saved_result":true}\n'
