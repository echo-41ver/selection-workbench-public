import io
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import replace_proxyshare_worker_environment as replacement
from selection_automation import profile_single_use as profiles
from selection_automation.adspower_diagnostics import AdsPowerAPIError, provider_error


def test_explicit_start_timeout_is_retryable_only_for_browser_start_and_is_redacted():
    message = 'start browser timeout; password=TEST_ONLY_SECRET'
    error = provider_error('/api/v1/browser/start?user_id=TEST_ONLY_PRIVATE', -1, message)
    assert error.diagnostic['category'] == 'START_TIMEOUT' and error.diagnostic['retryable']
    assert error.diagnostic['provider_code'] == -1
    assert 'TEST_ONLY' not in str(error) and 'password' not in str(error)
    assert not provider_error('/api/v1/user/create', -1, message).diagnostic['retryable']
    assert not provider_error('/api/v1/browser/start', -1, 'unknown timeout').diagnostic['retryable']


def test_inactive_live_profile_is_pending_and_keeps_its_reservation(tmp_path):
    profiles.reserve_start(tmp_path, 'fixture', lambda: {})
    reservation = tmp_path / 'runtime/profile-single-use/fixture.json'
    before = reservation.read_bytes()
    with pytest.raises(AdsPowerAPIError) as caught:
        profiles.reserve_start(tmp_path, 'fixture', lambda: {'data': {'status': 'Inactive'}}, lambda _: True)
    assert caught.value.diagnostic['category'] == 'STARTUP_PENDING' and caught.value.diagnostic['retryable']
    assert '关闭后禁止' not in str(caught.value) and reservation.read_bytes() == before


@pytest.mark.parametrize('becomes_active', [False, True])
def test_start_timeout_uses_three_existing_attempts_without_resending_a_pending_start(tmp_path, monkeypatch, becomes_active):
    monkeypatch.setattr(replacement, 'ROOT', tmp_path)
    monkeypatch.setattr(profiles.psutil, 'process_iter', lambda _: [SimpleNamespace(info={
        'name': 'flowerbrowser.exe', 'cmdline': ['flowerbrowser.exe', '--user-data-dir=C:/fixture']})])
    calls, sleeps = [], []
    def request(req, timeout):
        endpoint = req.full_url.split('?', 1)[0].rsplit('/', 1)[-1]
        calls.append((endpoint, timeout))
        if endpoint == 'active':
            ready = becomes_active and sum(name == 'active' for name, _ in calls) == 2
            result = {'code': 0, 'data': {'status': 'Active' if ready else 'Inactive'}}
        elif endpoint == 'start':
            result = ({'code': -1, 'msg': 'start browser timeout; password=TEST_ONLY_SECRET'}
                if sum(name == 'start' for name, _ in calls) == 1
                else {'code': 0, 'data': {'marionette_port': 1234, 'webdriver': 'fixture-driver'}})
        else:
            pytest.fail('Startup reconciliation must not create another profile')
        return io.BytesIO(json.dumps(result).encode())
    monkeypatch.setattr(replacement.urllib.request, 'build_opener', lambda *_: SimpleNamespace(open=request))
    monkeypatch.setattr(replacement.time, 'sleep', sleeps.append)
    if becomes_active:
        assert replacement.start_profile_with_retry('http://fixture', 'fixture')['marionette_port'] == 1234
        assert calls == [('start', 45), ('active', 8), ('active', 8), ('start', 45)]
    else:
        with pytest.raises(AdsPowerAPIError) as caught:
            replacement.start_profile_with_retry('http://fixture', 'fixture')
        assert caught.value.diagnostic['category'] == 'STARTUP_PENDING'
        assert 'TEST_ONLY' not in str(caught.value)
        assert calls == [('start', 45), ('active', 8), ('active', 8)]
    assert sleeps == [2, 4]
    assert len(list((tmp_path / 'runtime/profile-single-use').glob('*.json'))) == 1


@pytest.mark.parametrize('status', ['Inactive', 'Active'])
def test_closed_used_profile_still_cannot_be_started_again(tmp_path, status):
    profiles.reserve_start(tmp_path, 'fixture', lambda: {})
    with pytest.raises(RuntimeError, match='关闭后禁止'):
        profiles.reserve_start(tmp_path, 'fixture', lambda: {'data': {'status': status}}, lambda _: False)
