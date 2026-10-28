import io
import json
import subprocess
import time
from types import SimpleNamespace
from urllib.error import HTTPError

import pytest

from selection_automation.market_provider import Provider


BASE = 'https://openapi.geekbi.com'
QUERY = {'keyword': 'fixture', 'siteId': 48, 'page': 1, 'size': 1}
ACCOUNT = {'username': 'TEST_ONLY_ACCOUNT', 'password': 'TEST_ONLY_PASSWORD'}
SENT_REPLY = json.dumps({'status': 'AUTHORIZATION_SENT', 'diagnostics': {'submitted': True, 'callback': False}})


def response(data):
    return io.BytesIO(json.dumps({'code': 0, 'data': data}).encode())


def challenge(request, device='fixture-device'):
    payload = {'msg': 'fixture-login-required', 'data': {
        'error': 'AUTH_REQUIRED', 'deviceCode': device,
        'jumpUrl': BASE + '/agent/auth/login?fixture=' + device, 'expiresIn': 600}}
    return HTTPError(request.full_url, 401, 'fixture', {},
                     io.BytesIO(json.dumps(payload).encode()))


def write_server(path, server):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({'version': 1, 'servers': {BASE: server}}), encoding='utf8')


def read_server(path):
    return json.loads(path.read_text('utf8'))['servers'][BASE]


@pytest.fixture
def sandbox(tmp_path, monkeypatch):
    import geekbi_temu_mcp.auth as sdk
    import selection_automation.market_login as login
    import selection_automation.secrets as secrets

    def forbidden(*args, **kwargs):
        raise AssertionError('unexpected network, DPAPI read or browser launch')

    monkeypatch.setenv('GEEKBI_BASE_URL', BASE)
    monkeypatch.setattr(sdk, 'default_auth_path', lambda: tmp_path / 'shared.json')
    monkeypatch.setattr(sdk, 'urlopen', forbidden)
    monkeypatch.setattr(login, 'load', forbidden)
    monkeypatch.setattr(secrets, 'load', forbidden)
    monkeypatch.setattr(login.subprocess, 'run', forbidden)
    return SimpleNamespace(root=tmp_path, local=tmp_path / 'runtime/market-auth/agent-auth.json',
                           sdk=sdk, login=login)


@pytest.fixture
def company(sandbox, monkeypatch):
    import selection_automation.secrets as secrets
    secret = sandbox.root / 'runtime/secrets/company.dpapi'
    secret.parent.mkdir(parents=True)
    secret.touch()
    saved_company = lambda path: {'geekbi': dict(ACCOUNT)}
    monkeypatch.setattr(sandbox.login, 'load', saved_company)
    monkeypatch.setattr(secrets, 'load', saved_company)
    monkeypatch.setattr(sandbox.login, 'activate', lambda root, env: env)
    monkeypatch.setattr(sandbox.login.shutil, 'which', lambda *args, **kwargs: 'fixture-node')
    return sandbox


def test_valid_installation_token_never_reads_company_secret_or_opens_browser(sandbox, monkeypatch):
    write_server(sandbox.local, {'accessToken': 'fixture-token',
                               'accessTokenExpiresAt': int(time.time()) + 3600})
    secret = sandbox.root / 'runtime/secrets/company.dpapi'
    secret.parent.mkdir(parents=True)
    secret.touch()
    calls = []

    def route(request, timeout):
        assert not request.full_url.endswith(sandbox.sdk.TOKEN_ENDPOINT)
        assert request.get_header('Token') == 'Bearer fixture-token'
        calls.append(request.full_url)
        return response({'list': [], 'total': 0})

    monkeypatch.setattr(sandbox.sdk, 'urlopen', route)
    assert Provider(auth_state_path=sandbox.local).categories(QUERY)['list'] == []
    assert Provider(auth_state_path=sandbox.local).categories(QUERY)['list'] == []
    assert len(calls) == 2


def test_auth_required_logs_in_via_stdin_retries_original_once_and_reuses_token(company, monkeypatch):
    requests = []
    launches = []

    def route(request, timeout):
        requests.append(request)
        if request.full_url.endswith(company.sdk.TOKEN_ENDPOINT):
            assert json.loads(request.data) == {'deviceCode': 'fixture-device'}
            return response({'accessToken': 'fixture-token', 'expiresIn': 3600})
        if request.get_header('Token') is None:
            raise challenge(request)
        assert request.get_header('Token') == 'Bearer fixture-token'
        return response({'list': [], 'total': 0})

    def launch(command, **options):
        launches.append((command, options))
        assert ACCOUNT['username'] not in str(command) and ACCOUNT['password'] not in str(command)
        supplied = json.loads(options['input'])
        assert supplied['username'] == ACCOUNT['username']
        assert supplied['password'] == ACCOUNT['password']
        assert supplied['jump_url'] == BASE + '/agent/auth/login?fixture=fixture-device'
        return SimpleNamespace(returncode=0, stdout=SENT_REPLY, stderr='')

    monkeypatch.setattr(company.sdk, 'urlopen', route)
    monkeypatch.setattr(company.login.subprocess, 'run', launch)
    first = Provider(auth_state_path=company.local)
    assert first.categories(QUERY)['list'] == []
    assert Provider(auth_state_path=company.local).categories(QUERY)['list'] == []
    originals = [request for request in requests if not request.full_url.endswith(company.sdk.TOKEN_ENDPOINT)]
    assert len(originals) == 3 and len(launches) == 1 and len(requests) == 4
    assert originals[0].full_url == originals[1].full_url == originals[2].full_url
    assert originals[0].method == originals[1].method and originals[0].data == originals[1].data
    saved = read_server(company.local)
    assert saved['accessToken'] == 'fixture-token' and 'pending' not in saved
    diagnostic = json.loads((company.local.parent / 'company-login-status.json').read_text('utf8'))
    assert diagnostic['status'] == 'AUTHORIZED' and diagnostic['diagnostics']['submitted'] is True


def test_second_auth_challenge_is_returned_without_another_browser_attempt(company, monkeypatch):
    originals = []
    launches = []

    def route(request, timeout):
        if request.full_url.endswith(company.sdk.TOKEN_ENDPOINT):
            return response({'accessToken': 'fixture-token', 'expiresIn': 3600})
        originals.append(request)
        raise challenge(request, 'fixture-device-' + str(len(originals)))

    def launch(*args, **kwargs):
        launches.append(1)
        return SimpleNamespace(returncode=0, stdout=SENT_REPLY, stderr='')

    monkeypatch.setattr(company.sdk, 'urlopen', route)
    monkeypatch.setattr(company.login.subprocess, 'run', launch)
    with pytest.raises(company.sdk.ActionRequired) as raised:
        Provider(auth_state_path=company.local).categories(QUERY)
    assert raised.value.action == 'AUTH_REQUIRED'
    assert len(originals) == 2 and len(launches) == 1
    assert read_server(company.local)['pending']['deviceCode'] == 'fixture-device-2'


@pytest.mark.parametrize('status', ['CAPTCHA_REQUIRED', 'LOGIN_REJECTED', 'LOGIN_HTTP_403',
                                  'LOGIN_HTTP_429', 'LOGIN_NETWORK_FAILED'])
def test_manual_gate_survives_repeated_and_expired_challenges_until_reset_or_token(company, monkeypatch, status):
    originals = []
    launches = []
    authorized = False

    def route(request, timeout):
        if request.full_url.endswith(company.sdk.TOKEN_ENDPOINT):
            if authorized:
                return response({'accessToken': 'fixture-token', 'expiresIn': 3600})
            raise HTTPError(request.full_url, 202, 'fixture', {},
                            io.BytesIO(json.dumps({'data': {'error': 'AUTHORIZATION_PENDING'}}).encode()))
        if request.get_header('Token'):
            return response({'list': [], 'total': 0})
        originals.append(request)
        raise challenge(request, 'fixture-device-' + str(len(originals)))

    def launch(*args, **kwargs):
        launches.append(1)
        return SimpleNamespace(returncode=0, stdout=json.dumps({'status': status}), stderr='')

    monkeypatch.setattr(company.sdk, 'urlopen', route)
    monkeypatch.setattr(company.login.subprocess, 'run', launch)
    provider = Provider(auth_state_path=company.local)
    for _ in range(2):
        with pytest.raises(company.sdk.ActionRequired) as raised:
            provider.categories(QUERY)
        assert raised.value.pending
    saved = read_server(company.local)
    assert saved['pending']['deviceCode'] == 'fixture-device-1'
    assert saved['companyLoginBlocked'] and len(launches) == 1

    saved['pending']['expiresAt'] = int(time.time()) - 1
    write_server(company.local, saved)
    provider = Provider(auth_state_path=company.local)
    with pytest.raises(company.sdk.ActionRequired):
        provider.categories(QUERY)
    assert read_server(company.local)['pending']['deviceCode'] == 'fixture-device-2'
    assert len(launches) == 1

    company.login.reset_company_login(provider.client.auth)
    assert 'companyLoginBlocked' not in read_server(company.local)
    with pytest.raises(company.sdk.ActionRequired):
        provider.categories(QUERY)
    assert len(launches) == 2
    authorized = True
    assert provider.categories(QUERY)['list'] == []
    saved = read_server(company.local)
    assert 'companyLoginBlocked' not in saved and 'pending' not in saved


@pytest.mark.parametrize('mode', ['invalid-json', 'nonzero', 'timeout'])
def test_browser_stdout_stderr_never_escape_to_error_state_or_console(company, monkeypatch, capsys, mode):
    secrets = [ACCOUNT['username'], ACCOUNT['password'], 'TEST_ONLY_STDOUT', 'TEST_ONLY_STDERR']

    def route(request, timeout):
        raise challenge(request)

    def launch(command, **kwargs):
        if mode == 'timeout':
            raise subprocess.TimeoutExpired(command, 1, output=secrets[2], stderr=secrets[3])
        return SimpleNamespace(returncode=0 if mode == 'invalid-json' else 1,
                               stdout=secrets[2], stderr=secrets[3])

    monkeypatch.setattr(company.sdk, 'urlopen', route)
    monkeypatch.setattr(company.login.subprocess, 'run', launch)
    with pytest.raises(company.sdk.ActionRequired) as raised:
        Provider(auth_state_path=company.local).categories(QUERY)
    captured = capsys.readouterr()
    visible = (str(raised.value) + json.dumps(raised.value.public_payload())
               + company.local.read_text('utf8') + captured.out + captured.err)
    assert raised.value.action == 'AUTH_REQUIRED' and raised.value.pending
    assert all(secret not in visible for secret in secrets)


def test_absent_company_dpapi_preserves_original_auth_required(sandbox, monkeypatch):
    def route(request, timeout):
        raise challenge(request)

    monkeypatch.setattr(sandbox.sdk, 'urlopen', route)
    with pytest.raises(sandbox.sdk.ActionRequired) as raised:
        Provider(auth_state_path=sandbox.local).categories(QUERY)
    assert raised.value.message == 'fixture-login-required'
    assert raised.value.action == 'AUTH_REQUIRED' and not raised.value.pending
    assert raised.value.jump_url == BASE + '/agent/auth/login?fixture=fixture-device'
    assert raised.value.expires_in == 600
    assert 'companyLogin' not in read_server(sandbox.local)['pending']


@pytest.mark.parametrize('status', ['FORM_UNAVAILABLE', 'AUTHORIZATION_SENT'])
def test_unsubmitted_page_never_polls_token_or_claims_submission_and_diagnostics_are_safe(company, monkeypatch, status):
    def route(request, timeout):
        assert not request.full_url.endswith(company.sdk.TOKEN_ENDPOINT)
        raise challenge(request)

    observed = {'password_count': 0, 'account_count': 0, 'submit_count': 0,
                'submitted': False, 'callback': False, 'username': ACCOUNT['username'],
                'password': ACCOUNT['password'], 'url': 'https://fixture/?device=PRIVATE_QUERY',
                'body': 'PRIVATE_PAGE_TEXT'}
    monkeypatch.setattr(company.sdk, 'urlopen', route)
    monkeypatch.setattr(company.login.subprocess, 'run', lambda *a, **kw: SimpleNamespace(
        returncode=0, stdout=json.dumps({'status': status, 'diagnostics': observed}), stderr=''))
    with pytest.raises(company.sdk.ActionRequired) as raised:
        Provider(auth_state_path=company.local).categories(QUERY)
    assert '已提交' not in raised.value.message
    assert read_server(company.local)['pending']['companyLogin']['status'] == 'FORM_UNAVAILABLE'
    path = company.local.parent / 'company-login-status.json'
    diagnostic = json.loads(path.read_text('utf8'))
    assert diagnostic['status'] == 'FORM_UNAVAILABLE'
    assert diagnostic['diagnostics'] == {key: observed[key] for key in (
        'password_count', 'account_count', 'submit_count', 'submitted', 'callback')}
    assert all(secret not in path.read_text('utf8') for secret in (
        *ACCOUNT.values(), 'PRIVATE_QUERY', 'PRIVATE_PAGE_TEXT'))
