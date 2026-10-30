"""Simulated service responses only: no real key, paid task or live-site solve."""
import base64
import io
import json
from pathlib import Path
import sqlite3
from types import SimpleNamespace

from PIL import Image
import pytest
from twocaptcha.exceptions.api import ApiException, NetworkException
from twocaptcha.exceptions.solver import NetworkException as NotReady

from selection_automation import captcha
from selection_automation.company_bundle import import_bundle, portable
from selection_automation.secrets import load, store


@pytest.fixture
def challenge_request():
    image = Image.new('RGB', (120, 40), 'white')
    for x in range(120):
        image.putpixel((x, x % 40), (x, 40, 90))
    buffer = io.BytesIO(); image.save(buffer, format='PNG')
    return {'scope': 'temu/test_only', 'fingerprint': 'a' * 64,
            'kind': 'normal', 'images': [base64.b64encode(buffer.getvalue()).decode()]}


@pytest.fixture
def config():
    return {**captcha.DEFAULTS, 'key': 'TEST_ONLY_CAPTCHA', 'enabled': True}


def test_unconfigured_disabled_and_unsupported_make_no_requests(tmp_path, challenge_request, config):
    assert captcha.step(tmp_path, challenge_request)['status'] == 'NOT_CONFIGURED'
    assert captcha.step(tmp_path, challenge_request, config={**config, 'enabled': False})['status'] == 'DISABLED'
    assert captcha.step(tmp_path, {**challenge_request, 'kind': 'rotate'}, config=config)['status'] == 'UNSUPPORTED'
    assert not (tmp_path / 'runtime/captcha').exists()


def test_durable_submit_restart_poll_claim_and_confirm(tmp_path, challenge_request, config):
    calls = []
    client = SimpleNamespace(balance=lambda: 1, send=lambda **p: calls.append(p) or '123', get_result=lambda _: 'ABC123')
    assert captcha.step(tmp_path, challenge_request, config=config, client=client, now=1)['status'] == 'PENDING'
    assert captcha.step(tmp_path, challenge_request, config=config, client=client, now=2)['status'] == 'PENDING'
    answer = captcha.step(tmp_path, challenge_request, config=config, client=client, now=12)
    assert answer['answer'] == 'ABC123' and len(calls) == 1
    # A restarted worker gets the existing answer without resubmission.
    assert captcha.step(tmp_path, challenge_request, config=config, client=client, now=13) == answer
    assert b'ABC123' not in (tmp_path / 'runtime/captcha/tasks.sqlite3').read_bytes()
    claim = {**challenge_request, 'action': 'claim'}
    assert captcha.step(tmp_path, claim, config=config)['status'] == 'APPLYING'
    assert captcha.step(tmp_path, challenge_request, config=config, client=client)['status'] == 'APPLYING'
    assert captcha.step(tmp_path, {**claim, 'action': 'verify', 'passed': True}, config=config)['status'] == 'VERIFIED'
    assert captcha.step(tmp_path, challenge_request, config=config, client=client)['status'] == 'VERIFIED'
    assert len(calls) == 1


@pytest.mark.parametrize('code,status', [('ERROR_ZERO_BALANCE', 'ZERO_BALANCE'),
    ('ERROR_WRONG_USER_KEY', 'INVALID_KEY'), ('ERROR_NO_SLOT_AVAILABLE', 'SERVICE_FAILED'),
    ('ERROR_CAPTCHA_UNSOLVABLE', 'SERVICE_FAILED')])
def test_service_errors_are_fixed_chinese_states_without_payload(tmp_path, challenge_request, config, code, status):
    def reject(**_):
        raise ApiException(code + ' TEST_ONLY_CAPTCHA SECRET_RESPONSE')
    client = SimpleNamespace(balance=lambda: 1, send=reject)
    result = captcha.step(tmp_path, challenge_request, config=config, client=client, now=0)
    assert result['status'] == status and '保留' in result['message']
    assert 'TEST_ONLY' not in json.dumps(result) and 'SECRET_RESPONSE' not in json.dumps(result)
    if status in ('ZERO_BALANCE', 'INVALID_KEY'):
        assert captcha.step(tmp_path, challenge_request, config=config,
            client=SimpleNamespace(balance=lambda: 1, send=lambda **_: '789'), now=1)['status'] == 'PENDING'


def test_ambiguous_submit_is_never_paid_twice(tmp_path, challenge_request, config):
    calls = []
    def unknown(**_):
        calls.append(1); raise NetworkException('TEST_ONLY_SECRET')
    client = SimpleNamespace(balance=lambda: 1, send=unknown)
    for now in (1, 10, 1000):
        assert captcha.step(tmp_path, challenge_request, config=config, client=client, now=now)['status'] == 'SUBMIT_UNKNOWN'
    assert len(calls) == 1


def test_not_ready_has_a_deadline_and_keeps_one_task(tmp_path, challenge_request, config):
    calls = []
    def waiting(_):
        raise NotReady()
    client = SimpleNamespace(balance=lambda: 1, send=lambda **_: calls.append(1) or '123', get_result=waiting)
    assert captcha.step(tmp_path, challenge_request, config=config, client=client, now=0)['status'] == 'PENDING'
    assert captcha.step(tmp_path, challenge_request, config=config, client=client, now=10)['status'] == 'PENDING'
    assert captcha.step(tmp_path, challenge_request, config=config, client=client, now=180)['status'] == 'TIMEOUT'
    assert captcha.step(tmp_path, challenge_request, config=config, client=client, now=999)['status'] == 'TIMEOUT'
    assert len(calls) == 1


def test_temu_official_three_piece_parameters_and_result_validation(tmp_path, challenge_request, config):
    calls = []
    client = SimpleNamespace(balance=lambda: 1, send=lambda **p: calls.append(p) or '123',
                             get_result=lambda _: 'x=10,y=20|x=30,y=40|x=50,y=60')
    challenge_request.update(kind='temu', images=challenge_request['images'] * 4)
    assert captcha.step(tmp_path, challenge_request, config=config, client=client, now=0)['status'] == 'PENDING'
    assert set(calls[0]) == {'method', 'body', 'part1', 'part2', 'part3'}
    assert calls[0]['method'] == 'temuimage'
    result = captcha.step(tmp_path, challenge_request, config=config, client=client, now=10)
    assert json.loads(result['answer']) == [{'x': 10, 'y': 20}, {'x': 30, 'y': 40}, {'x': 50, 'y': 60}]


@pytest.mark.parametrize('limits', [{'max_requests': 1}, {'budget_usd': 0.01}])
def test_shared_task_limits_bound_new_questions(tmp_path, challenge_request, config, limits):
    client = SimpleNamespace(balance=lambda: 1, send=lambda **_: '123')
    config.update(limits)
    assert captcha.step(tmp_path, challenge_request, config=config, client=client)['status'] == 'PENDING'
    result = captcha.step(tmp_path, {**challenge_request, 'fingerprint': 'b' * 64}, config=config, client=client)
    assert result['status'] == 'BUDGET_LIMIT'


def test_balance_delta_stops_new_spending(tmp_path, challenge_request, config):
    client = SimpleNamespace(balance=lambda: 1, send=lambda **_: '123')
    captcha.step(tmp_path, challenge_request, config=config, client=client)
    client.balance = lambda: 0.89
    assert captcha.step(tmp_path, {**challenge_request, 'fingerprint': 'b' * 64}, config=config, client=client)['status'] == 'BUDGET_LIMIT'


def test_full_private_bundle_automatically_provisions_key_and_enables_service(tmp_path, config):
    import sys
    import zipfile
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
    from package_company_wizard import write_bundle
    # Even an old disabled setting must deploy as the built-in automatic service.
    bundle = {'adspower_api_key': 'TEST_ONLY_ADSPOWER', 'proxy_architecture': 'LOCAL_VPN',
              'twocaptcha': {**config, 'enabled': False}}
    base = tmp_path / 'internal-base.zip'; packaged = tmp_path / 'company.zip'
    with zipfile.ZipFile(base, 'w') as archive:
        archive.writestr('BUILD_INFO.json', json.dumps({'channel': 'internal', 'contains_company_credentials': False}))
    write_bundle(base, packaged, bundle)
    with zipfile.ZipFile(packaged) as archive:
        exported = json.loads(archive.read('company.local.json'))
        assert json.loads(archive.read('BUILD_INFO.json'))['unattended_company_import']
        assert not any(name.endswith('.dpapi') for name in archive.namelist())
    assert exported['twocaptcha']['enabled'] is True
    target = tmp_path / 'recipient-install'
    import_bundle(target, exported, store)
    assert load(target / 'runtime/secrets/twocaptcha.dpapi') == config
    assert 'twocaptcha' not in load(target / 'runtime/secrets/company.dpapi')
    assert b'TEST_ONLY_CAPTCHA' not in (target / 'runtime/secrets/twocaptcha.dpapi').read_bytes()
    assert captcha.settings(target)['enabled'] is True
    assert captcha.settings(target)['key'] == config['key']


def test_public_filter_rejects_captcha_keys_and_runtime_data():
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
    from build_delivery_packages import scan_files, included
    findings = scan_files({'config/mistake.json': b'{"twocaptcha":{"key":"deliberately-invalid-literal"}}'})
    assert findings[0]['field'] == 'twocaptcha.key'
    assert not included('runtime/secrets/twocaptcha.dpapi') and not included('runtime/captcha/tasks.sqlite3')


def test_sdk_transport_enforces_https_timeout_and_only_exposes_error_code(monkeypatch):
    import requests
    calls = []
    def mock(method, url, **kwargs):
        calls.append((method, url, kwargs))
        return SimpleNamespace(status_code=200, text='ERROR_ZERO_BALANCE TEST_ONLY_SECRET_RESPONSE')
    monkeypatch.setattr(requests, 'request', mock)
    client = captcha.sdk_client('TEST_ONLY_CAPTCHA')
    with pytest.raises(ApiException, match='^ERROR_ZERO_BALANCE$'):
        client.balance()
    assert calls[0][1] == 'https://2captcha.com/res.php' and calls[0][2]['timeout'] == (5, 15)


@pytest.mark.parametrize('change', [{'max_requests': 0}, {'timeout_seconds': True}, {'budget_usd': float('nan')},
                                    {'reserve_usd': 11}, {'enabled': 'yes'}, {'key': 'contains\nnewline'}])
def test_invalid_config_rejected_at_boundary(config, change):
    with pytest.raises(ValueError):
        captcha.normalize({**config, **change})


@pytest.mark.parametrize('status,configured,stopped,restart', [
    ('PENDING', True, False, True), ('ANSWER_READY', True, False, True),
    ('PENDING', False, False, False), ('PENDING', True, True, False),
    ('REJECTED', True, False, False)])
def test_supervisor_resumes_paid_task_in_same_session_without_rotation(tmp_path, monkeypatch, status, configured, stopped, restart):
    import os
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
    import selection
    monkeypatch.setattr(selection, 'ROOT', tmp_path)
    base = tmp_path / 'runs/test_only'; output = base / 'temu/workers/worker-01-new/query'
    selection.save(output / 'progress.json', {'status': 'WAITING_CAPTCHA', 'captcha': {'status': status}})
    path = tmp_path / 'runtime/secrets/twocaptcha.dpapi'
    path.parent.mkdir(parents=True); path.write_bytes(b'TEST_ONLY_CONFIG')
    os.utime(path, (1, 1))
    if stopped:
        (base / 'STOP').touch()
    worker = {'status': 'HUMAN_ACTION_REQUIRED', 'output': str(output), 'current_query': 'fixture'}
    view = {'采集状态': 'WAITING_CAPTCHA'}
    recovery = {'verify_to_resume': True}
    monkeypatch.setattr(selection, 'worker_snapshot', lambda *_: (worker, view.copy(), recovery))
    monkeypatch.setattr(captcha, 'settings', lambda _: {**captcha.DEFAULTS, 'enabled': configured, 'key': 'TEST_ONLY_CAPTCHA'})
    state = {}; session = {'name': 'worker-01-new', 'profile_id': 'retained', 'session_id_file': 'retained.sid'}
    held = selection.hold_blocked_worker(base, session, children={}, finished=set(), attempts={}, next_at={},
                                         state=state, verify=lambda *_: False)
    assert held is not restart
    assert session['profile_id'] == 'retained' and session['session_id_file'] == 'retained.sid'
    if restart:
        saved = selection.read(base / 'temu/supervisors/worker-01-new.json', {})
        assert saved['status'] == 'RESUMING' and saved['output'] == str(output)


def test_workbench_has_no_manual_captcha_setup_ui_or_api():
    root = Path(__file__).resolve().parents[1]
    assert '/api/captcha' not in (root / 'scripts/web_console.py').read_text('utf-8')
    assert 'captcha-settings' not in (root / 'web/index.html').read_text('utf-8')
    assert 'captcha-save' not in (root / 'web/index.html').read_text('utf-8')
