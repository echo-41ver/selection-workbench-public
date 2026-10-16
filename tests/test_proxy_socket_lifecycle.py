"""Local reset injection: no residential traffic or real credentials."""
import os
from contextlib import contextmanager
from pathlib import Path
import shutil
import socket
import socketserver
import struct
import subprocess
import sys
import threading
import time

import pytest
from selection_automation.proxy_activity import ProxyActivityLease,revoke_worker_activity
from test_proxy_install_repairs import header

ROOT=Path(__file__).resolve().parents[1]


@pytest.mark.parametrize('reset_after_header',[False,True])
def test_connection_reset_does_not_kill_proxy_and_next_tunnel_works(tmp_path,reset_after_header):
    attempts=[]
    class Upstream(socketserver.BaseRequestHandler):
        def handle(self):
            self.request.settimeout(3)
            incoming=header(self.request)
            if not incoming:return
            attempts.append(incoming)
            if len(attempts)==1:
                if reset_after_header:
                    self.request.sendall(b'HTTP/1.1 200 Connection Established\r\n\r\n')
                    time.sleep(.03)
                self.request.setsockopt(socket.SOL_SOCKET,socket.SO_LINGER,struct.pack('hh' if sys.platform=='win32' else 'ii',1,0))
                return
            self.request.sendall(b'HTTP/1.1 200 Connection Established\r\n\r\n')
            try:
                while data:=self.request.recv(4096):self.request.sendall(data)
            except OSError:pass
    server=socketserver.ThreadingTCPServer(('127.0.0.1',0),Upstream);server.daemon_threads=True
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    with socket.socket() as reserved:
        reserved.bind(('127.0.0.1',0));port=reserved.getsockname()[1]
    env={**os.environ,'TEMU_CHAIN_LISTEN_PORT':str(port),'TEMU_CHAIN_LISTEN_HOST':'127.0.0.1',
         'TEMU_CHAIN_HANDSHAKE_TIMEOUT_MS':'4000','TEMU_PREPROXY_PORT':'0','PROXYSHARE_PROXY_HOST':'127.0.0.1','PROXYSHARE_PROXY_PORT':str(server.server_address[1]),
         'PROXYSHARE_PROXY_USER':'fixture','PROXYSHARE_PROXY_PASSWORD':'fixture',
         'TEMU_CHAIN_WORKER':'1','TEMU_CHAIN_ACTIVITY_DIR':str(tmp_path/'runtime/proxyshare-chains/activity/worker-1')}
    process=subprocess.Popen([shutil.which('node'),str(ROOT/'scripts/proxyshare_chain_proxy.mjs')],env=env,stdout=subprocess.PIPE,stderr=subprocess.PIPE)
    try:
        with ProxyActivityLease(tmp_path,1):
            deadline=time.monotonic()+6
            while True:
                try:
                    with socket.create_connection(('127.0.0.1',port),timeout=.2):break
                except OSError:
                    assert process.poll() is None
                    assert time.monotonic()<deadline
                    time.sleep(.03)
            with socket.create_connection(('127.0.0.1',port),timeout=3) as first:
                first.sendall(b'CONNECT www.temu.com:443 HTTP/1.1\r\nHost: www.temu.com\r\n\r\n')
                if reset_after_header:
                    try:
                        while first.recv(4096):pass
                    except ConnectionResetError:pass
                    # Once the client received 200, a reset never replays TLS.
                    assert len(attempts)==1
                else:
                    assert b'200 Connection Established' in header(first)
                    first.sendall(b'same-client');assert first.recv(100)==b'same-client'
                    assert len(attempts)==2
            assert process.poll() is None
            with socket.create_connection(('127.0.0.1',port),timeout=3) as second:
                second.sendall(b'CONNECT www.temu.com:443 HTTP/1.1\r\nHost: www.temu.com\r\n\r\n')
                assert b'200 Connection Established' in header(second)
                second.sendall(b'still-alive');assert second.recv(100)==b'still-alive'
            assert process.poll() is None
            assert len(attempts)==(2 if reset_after_header else 3)
            assert all(attempt==attempts[0] for attempt in attempts)
    finally:
        process.terminate();_,errors=process.communicate(timeout=5)
        server.shutdown();server.server_close();thread.join(timeout=2)
    assert b'Unhandled' not in errors


@contextmanager
def local_chain(tmp_path,handle,handshake_ms=4000):
    """Real local sockets with a scripted upstream; no external connection."""
    attempts=[]
    class Upstream(socketserver.BaseRequestHandler):
        def handle(self):
            self.request.settimeout(6)
            incoming=header(self.request)
            if incoming:
                attempts.append(incoming)
                handle(self.request,len(attempts))
    server=socketserver.ThreadingTCPServer(('127.0.0.1',0),Upstream);server.daemon_threads=True
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    with socket.socket() as reserved:
        reserved.bind(('127.0.0.1',0));port=reserved.getsockname()[1]
    env={**os.environ,'TEMU_CHAIN_LISTEN_HOST':'127.0.0.1','TEMU_CHAIN_LISTEN_PORT':str(port),
         'TEMU_CHAIN_HANDSHAKE_TIMEOUT_MS':str(handshake_ms),'TEMU_PREPROXY_PORT':'0','PROXYSHARE_PROXY_HOST':'127.0.0.1','PROXYSHARE_PROXY_PORT':str(server.server_address[1]),
         'PROXYSHARE_PROXY_USER':'offline-fixture','PROXYSHARE_PROXY_PASSWORD':'offline-fixture',
         'TEMU_CHAIN_WORKER':'1','TEMU_CHAIN_ACTIVITY_DIR':str(tmp_path/'runtime/proxyshare-chains/activity/worker-1')}
    error_path=tmp_path/'proxy-errors.log'
    error_stream=error_path.open('wb')
    process=subprocess.Popen([shutil.which('node'),str(ROOT/'scripts/proxyshare_chain_proxy.mjs')],env=env,stdout=subprocess.DEVNULL,stderr=error_stream)
    try:
        deadline=time.monotonic()+6
        while True:
            try:
                with socket.create_connection(('127.0.0.1',port),timeout=.2):break
            except OSError:
                assert process.poll() is None and time.monotonic()<deadline
                time.sleep(.03)
        with ProxyActivityLease(tmp_path,1):yield port,attempts,process
    finally:
        process.terminate();process.wait(timeout=5)
        error_stream.close();errors=error_path.read_bytes()
        server.shutdown();server.server_close();thread.join(timeout=2)
    assert b'Unhandled' not in errors and b'offline-fixture' not in errors


@pytest.mark.parametrize('failure',['reset','eof','timeout'])
def test_handshake_failure_stops_after_three_attempts(tmp_path,failure):
    def fail(sock,attempt):
        if failure=='reset':
            sock.setsockopt(socket.SOL_SOCKET,socket.SO_LINGER,struct.pack('hh' if sys.platform=='win32' else 'ii',1,0))
        elif failure=='timeout':
            try:sock.recv(1)
            except OSError:pass
    with local_chain(tmp_path,fail) as (port,attempts,process):
        started=time.monotonic()
        with socket.create_connection(('127.0.0.1',port),timeout=15) as client:
            client.sendall(b'CONNECT www.temu.com:443 HTTP/1.1\r\nHost: www.temu.com\r\n\r\n')
            assert b'502 Bad Gateway' in header(client)
        elapsed=time.monotonic()-started
        assert len(attempts)==3 and elapsed<14.5
        assert all(attempt==attempts[0] for attempt in attempts)
        if failure=='timeout':assert elapsed>=10
        assert process.poll() is None


@pytest.mark.parametrize('status',[403,407,429,502])
def test_upstream_http_error_is_forwarded_without_retry(tmp_path,status):
    response=f'HTTP/1.1 {status} Denied\r\nContent-Length: 0\r\n\r\n'.encode()
    with local_chain(tmp_path,lambda sock,attempt:sock.sendall(response)) as (port,attempts,process):
        with socket.create_connection(('127.0.0.1',port),timeout=3) as client:
            client.sendall(b'CONNECT www.temu.com:443 HTTP/1.1\r\nHost: www.temu.com\r\n\r\n')
            assert header(client)==response
        assert len(attempts)==1 and process.poll() is None


def test_partial_http_denial_then_eof_is_not_retried(tmp_path):
    with local_chain(tmp_path,lambda sock,attempt:sock.sendall(b'HTTP/1.1 403 Forbidden\r\n')) as (port,attempts,process):
        with socket.create_connection(('127.0.0.1',port),timeout=3) as client:
            client.sendall(b'CONNECT www.temu.com:443 HTTP/1.1\r\nHost: www.temu.com\r\n\r\n')
            assert b'502 Bad Gateway' in header(client)
        assert len(attempts)==1 and process.poll() is None


def test_rejected_client_resets_do_not_kill_other_tunnels(tmp_path):
    def echo(sock, attempt):
        sock.sendall(b'HTTP/1.1 200 Connection Established\r\n\r\n')
        try:
            while data := sock.recv(4096): sock.sendall(data)
        except OSError: pass
    with local_chain(tmp_path, echo) as (port, attempts, process):
        for _ in range(40):
            with socket.create_connection(('127.0.0.1', port), timeout=3) as client:
                client.sendall(b'CONNECT unrelated.invalid:443 HTTP/1.1\r\nHost: unrelated.invalid\r\n\r\n')
                assert b'403' in header(client)
                client.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER,
                    struct.pack('hh' if sys.platform == 'win32' else 'ii', 1, 0))
        with socket.create_connection(('127.0.0.1', port), timeout=3) as client:
            client.sendall(b'CONNECT www.temu.com:443 HTTP/1.1\r\nHost: www.temu.com\r\n\r\n')
            assert b'200 Connection Established' in header(client)
            client.sendall(b'alive'); assert client.recv(100) == b'alive'
        assert process.poll() is None and len(attempts) == 1


def test_activity_revocation_during_handshake_cancels_without_retry(tmp_path):
    closed=threading.Event()
    def revoke(sock,attempt):
        revoke_worker_activity(tmp_path,1)
        try:
            if sock.recv(1)==b'':closed.set()
        except ConnectionResetError:closed.set()
    with local_chain(tmp_path,revoke) as (port,attempts,process):
        with socket.create_connection(('127.0.0.1',port),timeout=3) as client:
            client.sendall(b'CONNECT www.temu.com:443 HTTP/1.1\r\nHost: www.temu.com\r\n\r\n')
            assert header(client)==b''
        assert closed.wait(1) and len(attempts)==1 and process.poll() is None


def test_client_disconnect_during_handshake_cancels_without_retry(tmp_path):
    received=threading.Event();closed=threading.Event()
    def wait_for_disconnect(sock,attempt):
        received.set()
        try:
            if sock.recv(1)==b'':closed.set()
        except ConnectionResetError:closed.set()
    with local_chain(tmp_path,wait_for_disconnect) as (port,attempts,process):
        with socket.create_connection(('127.0.0.1',port),timeout=3) as client:
            client.sendall(b'CONNECT www.temu.com:443 HTTP/1.1\r\nHost: www.temu.com\r\n\r\n')
            assert received.wait(1)
        assert closed.wait(1) and len(attempts)==1 and process.poll() is None


def test_retry_does_not_replay_early_tunnel_bytes_to_failed_upstream(tmp_path):
    payload=b'early-TLS-fixture';received=[]
    def recover(sock,attempt):
        if attempt==1:
            sock.settimeout(.1)
            try:received.append(sock.recv(100))
            except TimeoutError:pass
            return
        sock.sendall(b'HTTP/1.1 200 Connection Established\r\n\r\n')
        received.append(sock.recv(100));sock.sendall(received[-1])
    with local_chain(tmp_path,recover) as (port,attempts,process):
        with socket.create_connection(('127.0.0.1',port),timeout=3) as client:
            client.sendall(b'CONNECT www.temu.com:443 HTTP/1.1\r\nHost: www.temu.com\r\n\r\n'+payload)
            response=header(client)
            assert b'200 Connection Established' in response
            echoed=response.partition(b'\r\n\r\n')[2]
            while len(echoed)<len(payload):
                part=client.recv(100);assert part;echoed+=part
            assert echoed==payload
        assert received==[payload] and len(attempts)==2 and process.poll() is None


def test_successful_tunnel_outlives_handshake_timeout(tmp_path):
    def echo(sock,attempt):
        sock.sendall(b'HTTP/1.1 200 Connection Established\r\n\r\n')
        sock.sendall(sock.recv(100))
    with local_chain(tmp_path,echo) as (port,attempts,process):
        with socket.create_connection(('127.0.0.1',port),timeout=3) as client:
            client.sendall(b'CONNECT www.temu.com:443 HTTP/1.1\r\nHost: www.temu.com\r\n\r\n')
            assert b'200 Connection Established' in header(client)
            time.sleep(4.2)
            client.sendall(b'after-handshake-budget')
            assert client.recv(100)==b'after-handshake-budget'
        assert len(attempts)==1 and process.poll() is None


def test_owned_sockets_keep_error_handlers_after_handshake_cleanup():
    # Inject late ECONNRESET twice after transient listeners have disappeared.
    script=r'''
import fs from 'node:fs';import vm from 'node:vm';import {EventEmitter} from 'node:events';import assert from 'node:assert/strict';
const src=fs.readFileSync(process.argv[1],'utf8');
const owner=src.slice(src.indexOf('const connections = new Set();'),src.indexOf('setInterval('));
class Socket extends EventEmitter {destroy(){this.destroyed=true;this.emit('close');}}
const context=vm.createContext({client:new Socket(),upstream:new Socket()});vm.runInContext(owner,context);
vm.runInContext("const c=ownConnection(client);c.attachUpstream(upstream); upstream.emit('error',Object.assign(new Error('reset'),{code:'ECONNRESET'})); upstream.emit('error',new Error('late reset')); client.emit('error',new Error('late client reset')); if(!client.destroyed||!upstream.destroyed||connections.size) throw new Error('leaked sockets');",context);
const retryContext=vm.createContext({client:new Socket(),old:new Socket(),current:new Socket()});vm.runInContext(owner,retryContext);
vm.runInContext("const c=ownConnection(client,true);c.attachUpstream(old);old.emit('error',new Error('reset'));c.discardUpstream();c.attachUpstream(current);c.handshaking=false;old.emit('error',new Error('late reset'));if(client.destroyed||current.destroyed)throw new Error('old attempt killed tunnel');current.emit('error',new Error('active reset'));if(!client.destroyed||!current.destroyed||connections.size)throw new Error('leaked active tunnel');",retryContext);
'''
    result=subprocess.run([shutil.which('node'),'--input-type=module','-e',script,str(ROOT/'scripts/proxyshare_chain_proxy.mjs')],capture_output=True,text=True,timeout=10)
    assert result.returncode==0,result.stderr


def test_slow_successful_residential_handshake_is_not_cut_off_at_four_seconds(tmp_path):
    def slow(sock,attempt):
        time.sleep(4.4)
        sock.sendall(b'HTTP/1.1 200 Connection Established\r\n\r\n')
        sock.sendall(sock.recv(100))
    with local_chain(tmp_path,slow,handshake_ms=12000) as (port,attempts,process):
        with socket.create_connection(('127.0.0.1',port),timeout=8) as client:
            client.sendall(b'CONNECT www.temu.com:443 HTTP/1.1\r\nHost: www.temu.com\r\n\r\n')
            assert b'200 Connection Established' in header(client)
            client.sendall(b'working');assert client.recv(100)==b'working'
        assert len(attempts)==1 and process.poll() is None
