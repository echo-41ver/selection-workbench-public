"""Local fake upstream only: these tests never reach a residential proxy."""
from contextlib import contextmanager
import importlib.util
import json
import os
from pathlib import Path
import shutil
import socket
import socketserver
import subprocess
import sys
import threading
import time

import pytest

from selection_automation.proxy_activity import ProxyActivityLease, revoke_worker_activity


ROOT = Path(__file__).resolve().parents[1]


def header(sock):
    result = b''
    while b'\r\n\r\n' not in result:
        part = sock.recv(8192)
        if not part:
            break
        result += part
    return result


@pytest.fixture
def proxy(tmp_path, request):
    worker = getattr(request, "param", 1)
    received = []

    class Upstream(socketserver.BaseRequestHandler):
        def handle(self):
            self.request.settimeout(4)
            incoming = header(self.request)
            if not incoming:
                return
            received.append(incoming)
            if incoming.startswith(b'CONNECT '):
                self.request.sendall(b'HTTP/1.1 200 Connection Established\r\n\r\n')
                try:
                    while data := self.request.recv(4096):
                        self.request.sendall(data)
                except (OSError, TimeoutError):
                    pass
            else:
                self.request.sendall(b'HTTP/1.1 200 OK\r\nContent-Length: 2\r\nConnection: close\r\n\r\nOK')

    server = socketserver.ThreadingTCPServer(('127.0.0.1', 0), Upstream)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    with socket.socket() as reserve:
        reserve.bind(('127.0.0.1', 0))
        port = reserve.getsockname()[1]
    activity = tmp_path / f'runtime/proxyshare-chains/activity/worker-{worker}'
    env = {**os.environ, 'TEMU_CHAIN_LISTEN_HOST': '127.0.0.1', 'TEMU_CHAIN_LISTEN_PORT': str(port),
           'TEMU_PREPROXY_PORT': '0', 'PROXYSHARE_PROXY_HOST': '127.0.0.1',
           'PROXYSHARE_PROXY_PORT': str(server.server_address[1]),
           'PROXYSHARE_PROXY_USER': 'offline-fixture', 'PROXYSHARE_PROXY_PASSWORD': 'offline-fixture',
           'TEMU_CHAIN_WORKER': str(worker), 'TEMU_CHAIN_ACTIVITY_DIR': str(activity)}
    process = subprocess.Popen([shutil.which('node'), str(ROOT / 'scripts/proxyshare_chain_proxy.mjs')],
                               env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        deadline = time.monotonic() + 8
        while True:
            try:
                with socket.create_connection(('127.0.0.1', port), timeout=.2):
                    break
            except OSError:
                if process.poll() is not None or time.monotonic() >= deadline:
                    raise RuntimeError('offline proxy did not start')
                time.sleep(.05)
        yield tmp_path, port, received
    finally:
        process.terminate()
        process.communicate(timeout=5)
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


@contextmanager
def request(port, target, method='CONNECT'):
    with socket.create_connection(('127.0.0.1', port), timeout=3) as sock:
        sock.settimeout(3)
        sock.sendall(f'{method} {target} HTTP/1.1\r\nHost: ignored.example\r\n\r\n'.encode())
        yield sock, header(sock)


def test_proxy_denies_inactive_unrelated_and_wrong_port_without_upstream(proxy):
    root, port, received = proxy
    with request(port, 'www.temu.com:443') as (_, response):
        assert b'403 Forbidden' in response
    with ProxyActivityLease(root, 1):
        for target in ('example.com:443', 'temu.com.example.com:443', 'temucdn.com.example.com:443', '127.0.0.1:443', 'www.temu.com:444', 'www.temu.com:80'):
            with request(port, target) as (_, response):
                assert b'403 Forbidden' in response
        with request(port, 'http://example.com/', 'GET') as (_, response):
            assert b'403 Forbidden' in response
    assert received == []


@pytest.mark.parametrize('host', ['aimg.temucdn.com', 'img.temucdn.com', 'commimg.temucdn.com'])
def test_first_party_cdn_tunnel_remains_available_during_verification(proxy, host):
    root, port, received = proxy
    with ProxyActivityLease(root, 1, purpose='manual_verification'):
        with request(port, host + ':443') as (sock, response):
            assert b'200 Connection Established' in response
            sock.sendall(b'cdn-fixture'); assert sock.recv(100) == b'cdn-fixture'
    assert len(received) == 1


def test_active_temu_tunnel_closes_on_revoke_and_old_lease_cannot_restore(proxy):
    root, port, received = proxy
    with ProxyActivityLease(root, 1) as lease:
        with request(port, 'www.temu.com:443') as (sock, response):
            assert b'200 Connection Established' in response
            sock.sendall(b'local fixture payload')
            assert sock.recv(100) == b'local fixture payload'
            revoke_worker_activity(root, 1)
            assert sock.recv(100) == b''
            assert not lease._refresh()
        with request(port, 'www.temu.com:443') as (_, response):
            assert b'403 Forbidden' in response
    assert len(received) == 1
    with ProxyActivityLease(root, 1, purpose='manual_verification'):
        with request(port, 'commimg.kwcdn.com:443') as (_, response):
            assert b'200 Connection Established' in response
    assert len(received) == 2


def test_proxy_expired_lease_denied_and_http_host_is_constrained(proxy):
    root, port, received = proxy
    with ProxyActivityLease(root, 1) as lease:
        payload = json.loads(lease.path.read_text())
        payload['expires_at'] = time.time() - 1
        lease.path.write_text(json.dumps(payload), encoding='utf-8')
        with request(port, 'www.temu.com:443') as (_, response):
            assert b'403 Forbidden' in response
        assert received == []
        assert lease._refresh()
        with request(port, 'http://www.temu.com/fixture', 'GET') as (_, response):
            assert b'200 OK' in response
        assert b'host: www.temu.com\r\n' in received[-1]
        assert b'ignored.example' not in received[-1]


def setup_module():
    global company_setup
    spec = importlib.util.spec_from_file_location('proxy_install_company_setup', ROOT / 'scripts/company_setup.py')
    company_setup = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(company_setup)


@pytest.mark.parametrize('location,relative', [
    ('ProgramFiles', 'AdsPower Global/AdsPower Global.exe'),
    ('ProgramFiles', 'AdsPower/AdsPower Global.exe'),
    ('ProgramFiles(x86)', 'AdsPower/AdsPower.exe'),
    ('LOCALAPPDATA', 'Programs/AdsPower Global/AdsPower Global.exe'),
    ('LOCALAPPDATA', 'Programs/AdsPower/AdsPower.exe'),
])
def test_adspower_installed_paths(tmp_path, monkeypatch, location, relative):
    for name in ('ProgramFiles', 'ProgramFiles(x86)', 'LOCALAPPDATA'):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv(location, str(tmp_path))
    executable = tmp_path / relative
    executable.parent.mkdir(parents=True)
    executable.write_bytes(b'offline fixture, never executed')
    assert Path(company_setup.find_adspower_executable()) == executable
    custom = tmp_path / 'company-custom.exe'
    custom.write_bytes(b'offline fixture, never executed')
    assert company_setup.find_adspower_executable({'adspower_executable': str(custom)}) == str(custom)


@pytest.mark.skipif(sys.platform != 'win32', reason='Windows PowerShell 5 parser')
def test_installer_bom_ps5_and_no_residential_preflight():
    assert (ROOT / 'install.ps1').read_bytes().startswith(b'\xef\xbb\xbf')
    shell = Path(os.environ['SystemRoot']) / 'System32/WindowsPowerShell/v1.0/powershell.exe'
    command = "$tokens=$null;$errors=$null;[System.Management.Automation.Language.Parser]::ParseFile((Join-Path (Get-Location) 'install.ps1'),[ref]$tokens,[ref]$errors)|Out-Null;if($errors){$errors;exit 1}"
    result = subprocess.run([str(shell), '-NoProfile', '-Command', command], cwd=ROOT, capture_output=True)
    assert result.returncode == 0, result.stdout
    manager = (ROOT / 'scripts/proxyshare_chain_manager.ps1').read_text(encoding='utf-8-sig')
    assert 'curl.exe' not in manager
    assert 'TEMU_CHAIN_ACTIVITY_DIR' in manager


@pytest.mark.parametrize('proxy', [8, 32], indirect=True)
def test_high_numbered_worker_can_tunnel_and_stop_revokes_all(proxy):
    from selection_automation.proxy_activity import revoke_all_activity
    root, port, received = proxy
    # The fixture starts only the configured high-numbered proxy.
    for number in (8, 32):
        with ProxyActivityLease(root, number):
            with request(port, 'www.temu.com:443') as (_, response):
                if b'200 Connection Established' not in response:
                    continue
                assert received
                revoke_all_activity(root)
            with request(port, 'www.temu.com:443') as (_, stopped):
                assert b'403 Forbidden' in stopped
            return
    pytest.fail('No high-numbered lease opened its proxy')
