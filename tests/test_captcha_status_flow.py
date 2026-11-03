import json
from pathlib import Path
import sys
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'scripts'))
import web_console as web
from selection_automation.captcha import MESSAGES


@pytest.mark.parametrize('state', ['PAGE_LOADING','PENDING','ANSWER_READY','VERIFIED',
    'SERVICE_FAILED','TIMEOUT','REJECTED','MANUAL_REQUIRED','SUBMIT_UNKNOWN'])
def test_real_worker_snapshot_and_console_distinguish_api_wait_from_manual(tmp_path, monkeypatch, state):
    monkeypatch.setattr(web.selection, 'ROOT', tmp_path)
    console = web.Console(tmp_path); console.key = 'captcha_fixture'
    monkeypatch.setattr(console, 'owner', lambda: {'category_key': console.key})
    base = tmp_path/'runs'/console.key
    output = base/'temu/workers/worker-01-new/fixture'
    web.selection.save(base/'status.json', {'状态': '运行中'})
    worker = {'name': 'worker-01-new', 'status': 'HUMAN_ACTION_REQUIRED', 'current_query': 'fixture',
              'output': str(output), 'verification_probe': {'check_ok': True, 'page_kind': 'CAPTCHA'}}
    web.selection.save(base/'temu/supervisors/worker-01-new.json', worker)
    progress = {'status': 'WAITING_HUMAN_VERIFICATION', 'unique_goods': 7,
                'captcha': {'status': state, 'message': MESSAGES[state]}, 'manual_action_required': True}
    web.selection.save(output/'progress.json', progress)
    payload = console.status()
    live = payload['workers'][0]['live_snapshot']
    manual = state not in {'PAGE_LOADING','PENDING','ANSWER_READY','VERIFIED'}
    assert live['验证码状态'] == state
    assert live['状态'] == live['建议动作'] == MESSAGES[state]
    assert live['需人工介入'] is manual and live['浏览器保留'] is True
    assert live['唯一商品数'] == 7
    assert payload['severity'] == ('red' if manual else 'green')
    assert web.selection.read(output/'progress.json') == progress

