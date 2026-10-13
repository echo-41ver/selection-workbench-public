"""TEMU regression tests: local fakes only, no residential traffic or real profiles."""
import base64
import importlib.util
import json
import os
from pathlib import Path
import socket
import socketserver
import subprocess
import sys
import threading
import time
from types import SimpleNamespace
import pytest
from selection_automation.proxy_activity import ProxyActivityLease

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'scripts'))


def module(name):
    spec=importlib.util.spec_from_file_location(name,ROOT/'scripts'/f'{name}.py')
    value=importlib.util.module_from_spec(spec); spec.loader.exec_module(value); return value


def replacement_fixture(tmp_path,monkeypatch):
    import selection
    m=module('replace_proxyshare_worker_environment')
    policy=tmp_path/'policy.json'; policy.write_text(json.dumps({'session_rotation_policy':'NEW_ADSPOWER_PROFILE_AND_NEW_PROXY_SESSION','worker_chain_ports':[22001,22002,22003,22004],'webdriver_ports':[23001,23002,23003,23004]}))
    sessions=tmp_path/'sessions.json'; sessions.write_text(json.dumps({'machine_id':'this-machine','workers':[{'name':'worker-01-new','profile_id':'old'}]}))
    monkeypatch.setattr(m,'ROOT',tmp_path);monkeypatch.setattr(m,'POLICY',policy);monkeypatch.setattr(m,'SESSIONS',sessions)
    monkeypatch.setattr(selection,'identity',lambda:'this-machine')
    monkeypatch.setattr(m,'powershell',lambda:'fake-pwsh');monkeypatch.setattr(m,'api_base',lambda:'http://127.0.0.1:1')
    driver=tmp_path/'driver.exe';driver.write_bytes(b'fixture')
    (tmp_path/'runtime').mkdir(exist_ok=True)
    (tmp_path/'runtime/last_run.json').write_text(json.dumps({'category_key':'fixture_task'}))
    from selection_automation import profile_single_use
    monkeypatch.setattr(profile_single_use,'running_profile',lambda profile:profile=='new')
    events=[]
    def api(base,path,**kw):
        events.append(path)
        if '/user/list?' in path:return {'data':{'list':[]}}
        if 'create' in path:
            assert kw['body']['user_proxy_config']['proxy_port']=='22001'
            assert kw['body']['user_proxy_config']['proxy_password']==''
            return {'data':{'profile_id':'new'}}
        if 'active?' in path:return {'data':{'status':'Inactive'}}
        if 'start' in path:return {'data':{'webdriver':str(driver),'marionette_port':24001}}
        return {'data':{}}
    monkeypatch.setattr(m,'api_call',api)
    monkeypatch.setattr(sys,'argv',['replace','--worker','1'])
    return m,events,sessions


@pytest.mark.parametrize("failure_count", [1, 2])
def test_replace_order_and_resume_after_bind_failure(tmp_path,monkeypatch,failure_count):
    m,events,sessions=replacement_fixture(tmp_path,monkeypatch)
    snapshot=tmp_path/'runs'/'products.jsonl';snapshot.parent.mkdir();snapshot.write_text('preserved')
    failures=[failure_count]
    def run(cmd,**kw):
        if '-Command' in cmd: events.append('local-release')
        elif any('chain_manager' in str(x) for x in cmd): events.append('new-proxy' if 'prepare-replacement' in cmd else 'reuse-new-proxy')
        elif any('bind_flower' in str(x) for x in cmd):
            events.append('bind')
            if failures[0]:failures[0]-=1;return SimpleNamespace(returncode=1,stderr='fixture bind failure',stdout='')
        return SimpleNamespace(returncode=0,stderr='',stdout='')
    monkeypatch.setattr(m.subprocess,'run',run)
    monkeypatch.setattr(m,'run_background_launcher',run)
    if failure_count == 2:
        with pytest.raises(RuntimeError,match='绑定失败'):m.main()
        journal=json.loads((tmp_path/'runtime/proxyshare-chains/replacement-worker-1.json').read_text())
        assert journal['phase']=='BIND_FAILED'
        assert journal['bind_attempts']==2
    else:
        m.main()
    assert json.loads(sessions.read_text())['workers'][0]['profile_id']=='new'
    assert events.index('/api/v2/browser-profile/delete')<events.index('new-proxy')<events.index('/api/v2/browser-profile/create')<events.index('bind')
    if failure_count == 2: m.main()
    assert events.count('bind')==failure_count+1
    assert events.count('/api/v1/browser/stop?user_id=new')==0
    assert sum('browser/start?' in x and 'user_id=new' in x for x in events)==(2 if failure_count==2 else 1)
    assert events.count('/api/v2/browser-profile/delete')==1
    assert events.count('/api/v2/browser-profile/create')==1
    if failure_count == 2: assert 'reuse-new-proxy' in events
    assert snapshot.read_text()=='preserved'
    journal=json.loads((tmp_path/'runtime/proxyshare-chains/replacement-worker-1.json').read_text())
    assert journal['phase']=='REPLACED'


def test_foreign_installation_never_deletes_profile(tmp_path,monkeypatch):
    m,events,sessions=replacement_fixture(tmp_path,monkeypatch)
    value=json.loads(sessions.read_text());value['machine_id']='other-machine';sessions.write_text(json.dumps(value))
    with pytest.raises(RuntimeError,match='another installation'):m.main()
    assert events==[]


def test_first_setup_resumes_created_profiles_without_duplicates(tmp_path,monkeypatch):
    import selection
    m=module('setup_proxyshare_adspower_workers')
    monkeypatch.setattr(m,'ROOT',tmp_path);monkeypatch.setattr(m,'SESSIONS',tmp_path/'config/sessions.json')
    monkeypatch.setattr(m,'api_base',lambda:'http://fixture');monkeypatch.setattr(m,'powershell',lambda:'fake-pwsh')
    monkeypatch.setattr(selection,'identity',lambda:'this-machine')
    monkeypatch.setattr(sys,'argv',['setup'])
    policy=tmp_path/'runtime/proxy_policy.json';policy.parent.mkdir()
    policy.write_text(json.dumps({'worker_chain_ports':[22001,22002,22003,22004],'webdriver_ports':[23001,23002,23003,23004]}))
    driver=tmp_path/'driver.exe';driver.write_bytes(b'fixture')
    created=[]; failed=[0]
    monkeypatch.setattr(m.start_profile_with_retry.__globals__['time'],'sleep',lambda _:None)
    def api(base,path,**kw):
        if 'active?' in path:return {'data':{'status':'Inactive'}}
        if 'create' in path:
            created.append(kw['body']['name']);return {'data':{'profile_id':f'new-{len(created)}'}}
        if 'start' in path and 'new-2' in path and failed[0]<3:
            failed[0]+=1;raise RuntimeError('fixture browser start failure')
        return {'data':{'webdriver':str(driver),'marionette_port':24001}}
    monkeypatch.setattr(m,'api_call',api)
    monkeypatch.setattr(m,'run_background_launcher',lambda *a,**k:SimpleNamespace(returncode=0,stdout='',stderr=''))
    with pytest.raises(RuntimeError,match='fixture browser'):m.main()
    assert len(created)==2
    assert not m.SESSIONS.exists()
    m.main()
    assert len(created)==4
    assert len(json.loads(m.SESSIONS.read_text())['workers'])==4
    assert not (tmp_path/'runtime/bootstrap.json').exists()


def test_batch_replacements_are_parallel_and_failure_isolated(tmp_path,monkeypatch,capsys):
    m=module('replace_proxyshare_workers_batch');monkeypatch.setattr(m,'ROOT',tmp_path)
    monkeypatch.setattr(sys,'argv',['batch','--workers','1,2'])
    barrier=threading.Barrier(2)
    def run(cmd,**kw):
        number=int(cmd[cmd.index('--worker')+1]);barrier.wait(timeout=3)
        return SimpleNamespace(returncode=number==2,stderr='worker 2 failed',stdout=json.dumps({'worker':number,'status':'REPLACED'}))
    monkeypatch.setattr(m,'run_background_launcher',run);m.main()
    result=json.loads(capsys.readouterr().out)
    assert result['status']=='BATCH_PARTIAL_FAILED'
    assert [r['status'] for r in result['results']]==['REPLACED','FAILED']


def exact(s,n):
    data=b''
    while len(data)<n:
        chunk=s.recv(n-len(data))
        if not chunk:raise EOFError()
        data+=chunk
    return data


@pytest.fixture
def active_proxy_root(tmp_path):
    with ProxyActivityLease(tmp_path, 1):
        yield tmp_path


@pytest.mark.parametrize('via_socks',[False,True])
def test_real_node_proxy_chain_locally(via_socks,active_proxy_root):
    """Exercise actual Node CONNECT/auth routing through mock residential/SOCKS servers."""
    seen=[]
    class Upstream(socketserver.BaseRequestHandler):
        def handle(self):
            data=b''
            while b'\r\n\r\n' not in data:data+=self.request.recv(4096)
            seen.append(data)
            self.request.sendall(b'HTTP/1.1 200 Connection Established\r\n\r\n')
            self.request.sendall(exact(self.request,4))
    class Socks(socketserver.BaseRequestHandler):
        def handle(self):
            assert exact(self.request,3)==b'\x05\x01\x00';self.request.sendall(b'\x05\x00')
            head=exact(self.request,5);host=exact(self.request,head[4]).decode();port=int.from_bytes(exact(self.request,2),'big')
            assert host=='127.0.0.1' and port==up.server_address[1]
            self.request.sendall(b'\x05\x00\x00\x01\x7f\x00\x00\x01\x00\x00')
            with socket.create_connection((host,port)) as dest:
                import select
                while True:
                    readable,_,_=select.select([self.request,dest],[],[],3)
                    if not readable:return
                    for source in readable:
                        data=source.recv(4096)
                        if not data:return
                        (dest if source is self.request else self.request).sendall(data)
    class Server(socketserver.ThreadingTCPServer):
        daemon_threads=True
    up=Server(('127.0.0.1',0),Upstream);socks=Server(('127.0.0.1',0),Socks)
    for server in [up,socks]:threading.Thread(target=lambda s=server:s.serve_forever(poll_interval=.05),daemon=True).start()
    with socket.socket() as probe:probe.bind(('127.0.0.1',0));port=probe.getsockname()[1]
    env={**os.environ,'TEMU_CHAIN_LISTEN_PORT':str(port),'TEMU_PREPROXY_PORT':str(socks.server_address[1] if via_socks else 0),
         'TEMU_CHAIN_WORKER':'1','TEMU_CHAIN_ACTIVITY_DIR':str(active_proxy_root/'runtime/proxyshare-chains/activity/worker-1'),
         'PROXYSHARE_PROXY_HOST':'127.0.0.1','PROXYSHARE_PROXY_PORT':str(up.server_address[1]),'PROXYSHARE_PROXY_USER':'fixture_session-abc','PROXYSHARE_PROXY_PASSWORD':'fixture-password'}
    child=subprocess.Popen(['node',str(ROOT/'scripts/proxyshare_chain_proxy.mjs')],env=env,stdout=subprocess.DEVNULL,stderr=subprocess.PIPE)
    try:
        deadline=time.monotonic()+5
        while True:
            try: client=socket.create_connection(('127.0.0.1',port),timeout=2);break
            except OSError:
                if time.monotonic()>deadline:raise
                time.sleep(.05)
        with client:
            client.sendall(b'CONNECT www.temu.com:443 HTTP/1.1\r\nHost: www.temu.com:443\r\n\r\n')
            response=b''
            while b'\r\n\r\n' not in response:response+=client.recv(4096)
            assert response.startswith(b'HTTP/1.1 200')
            assert b'fixture' not in response
            client.sendall(b'PING');assert exact(client,4)==b'PING'
        assert b'CONNECT www.temu.com:443' in seen[0]
        assert base64.b64encode(b'fixture_session-abc:fixture-password') in seen[0]
    finally:
        child.terminate();child.wait(timeout=5)
        for server in [up,socks]:server.shutdown();server.server_close()


def test_create_response_lost_adopts_profile_without_duplicate(tmp_path,monkeypatch):
    from selection_automation import profile_capacity
    m,events,sessions=replacement_fixture(tmp_path,monkeypatch)
    original=m.api_call;created=[]
    def api(base,path,**kwargs):
        if 'browser-profile/create' in path:
            created.append({'name':kwargs['body']['name'],'user_id':'new','serial_number':'1'})
            raise RuntimeError('API response lost')
        return original(base,path,**kwargs)
    monkeypatch.setattr(m,'api_call',api)
    monkeypatch.setattr(profile_capacity,'list_profiles',lambda *a:list(created))
    monkeypatch.setattr(m.subprocess,'run',lambda *a,**kw:SimpleNamespace(returncode=0,stderr='',stdout=''))
    monkeypatch.setattr(m,'run_background_launcher',lambda *a,**kw:SimpleNamespace(returncode=0,stderr='',stdout=''))
    m.main()
    assert len(created)==1
    assert json.loads(sessions.read_text())['workers'][0]['profile_id']=='new'
    assert json.loads((tmp_path/'runtime/proxyshare-chains/replacement-worker-1.json').read_text())['phase']=='REPLACED'

@pytest.mark.parametrize('error',['Profile ID did not resolve to one unique FlowerBrowser root process.',
    'Selected profile Marionette was not ready before the bounded deadline; browser retained.',
    'Maximum number of active sessions'])
def test_local_rebind_failure_preserves_open_browser(tmp_path,monkeypatch,error):
    r,events,sessions=replacement_fixture(tmp_path,monkeypatch)
    sessions.write_text(json.dumps({'machine_id':'this-machine','workers':[{'name':'worker-01-new','profile_id':'new','webdriver_url':'http://127.0.0.1:23001'}]}))
    r.atomic_json(tmp_path/'runtime/proxyshare-chains/replacement-worker-1.json',{'phase':'BIND_FAILED','new_profile_id':'new'})
    helper=module('resume_worker_binding');monkeypatch.setattr(helper,'r',r)
    results=[SimpleNamespace(returncode=1,stderr=error,stdout=''),SimpleNamespace(returncode=0,stderr='',stdout='')]
    monkeypatch.setattr(r,'run_background_launcher',lambda *a,**kw:results.pop(0))
    with pytest.raises(RuntimeError,match=__import__('re').escape(error)):
        helper.resume(1)
    assert sum('browser/stop?user_id=new' in x for x in events)==0
    assert not any('browser-profile/create' in x or 'browser-profile/delete' in x for x in events)
    journal=json.loads((tmp_path/'runtime/proxyshare-chains/replacement-worker-1.json').read_text())
    assert journal['phase']=='BIND_FAILED' and 'local_binding_recovered' not in journal


def test_profile_restart_waits_for_inactive_api_and_actual_process_exit(tmp_path, monkeypatch):
    r,events,sessions=replacement_fixture(tmp_path,monkeypatch)
    import psutil
    states=iter(['Active','Inactive','Inactive']);processes=iter([True,True,False]);polls=[]
    def api(base,path,**kwargs):
        events.append(path)
        if '/active?' in path:
            state=next(states);polls.append(state);return {'data':{'status':state}}
        return {'data':{}}
    def processes_now(*args):
        alive=next(processes)
        return [SimpleNamespace(info={'name':'FlowerBrowser.exe','cmdline':['--user-data-dir=C:/profiles/new_flower']})] if alive else []
    monkeypatch.setattr(psutil,'process_iter',processes_now)
    monkeypatch.setattr(r.time,'sleep',lambda _:None)
    r.stop_profile_and_wait('local','new',api=api)
    assert polls==['Active','Inactive','Inactive']
    assert events[0].endswith('stop?user_id=new')
