import pathlib,sys,threading,time,json,os,pytest
sys.path.insert(0,str(pathlib.Path(__file__).resolve().parents[1]/'scripts'))
from selection_automation.route_guard import RouteLock,collection_active
from selection_automation.worker_runtime import WorkerLease
from selection_automation.proxy_activity import ProxyActivityLease
from selection_automation.local_proxy_prepare import prepare
from selection_automation.network_readiness import NetworkNotReady

def observation():
    return {'status':'READY','candidates':[{'host':'127.0.0.1','port':45678,'protocol':'SOCKS5',
        'pid':101,'created':100,'name':'clash-meta.exe'}]}

def test_waiting_worker_does_not_block_other_route_admissions(tmp_path):
    directory=tmp_path/'runtime/worker-leases'
    waiting=threading.Event();finished=threading.Event();errors=[]
    def replacement():
        waiting.set()
        try:
            with WorkerLease(directory,'worker-01-new',purpose='replacement',timeout=3):
                finished.set()
        except Exception as error:errors.append(error)
    with WorkerLease(directory,'worker-01-new'):
        thread=threading.Thread(target=replacement);thread.start()
        assert waiting.wait(1)
        time.sleep(.2)
        with RouteLock(directory.parent,timeout=.5):
            assert not finished.is_set()
        with WorkerLease(directory,'worker-02-new'):
            assert not finished.is_set()
    thread.join(4)
    assert finished.is_set() and not errors

def test_route_contention_longer_than_old_timeout_is_transient(tmp_path):
    admitted=threading.Event();errors=[]
    def admission():
        try:
            with WorkerLease(tmp_path/'runtime/worker-leases','worker-03-new',purpose='replacement'):
                admitted.set()
        except Exception as error:errors.append(error)
    with RouteLock(tmp_path/'runtime'):
        thread=threading.Thread(target=admission);thread.start()
        assert not admitted.wait(16)
    thread.join(3)
    assert admitted.is_set() and not errors

def test_collector_admission_waits_until_route_commit_finishes(tmp_path):
    runtime=tmp_path/'runtime';runtime.mkdir()
    saved={'proxy_host':'proxy.example','proxy_port':5959,'preproxy_host':'127.0.0.1','preproxy_port':0}
    attempted=threading.Event();admitted=threading.Event();release=threading.Event();errors=[];writes=[]
    def collector():
        attempted.set()
        try:
            with WorkerLease(runtime/'worker-leases','worker-01-new'):
                admitted.set();release.wait(3)
        except Exception as error:errors.append(type(error).__name__)
    worker=threading.Thread(target=collector)
    def discover_after_first_idle_check():
        worker.start();assert attempted.wait(1)
        time.sleep(.1)
        assert not admitted.is_set()
        return observation()
    def read(path,default):return saved if path.name=='proxyshare.settings.json' else default
    def save(path,value):
        if path.name=='proxyshare.settings.json':
            assert not admitted.is_set();writes.append(path.name)
    try:
        prepare(tmp_path,{'proxy':{}},read,save,observation=discover_after_first_idle_check,check_owner=lambda c:True)
        assert admitted.wait(1)
        assert collection_active(tmp_path)  # A later differing chain adoption must fail.
        assert writes==['proxyshare.settings.json'] and not errors
    finally:release.set();worker.join(3)

def test_already_admitted_collector_blocks_commit_before_discovery(tmp_path):
    writes=[]
    with WorkerLease(tmp_path/'runtime/worker-leases','worker-01-new'):
        with pytest.raises(NetworkNotReady):prepare(tmp_path,{'proxy':{}},lambda p,d:{},lambda p,v:writes.append(p),
            observation=lambda:pytest.fail('must not discover while collector owns route'))
    assert not writes

def test_manual_activity_admission_uses_same_route_lock(tmp_path):
    admitted=threading.Event();release=threading.Event();errors=[]
    def manual():
        try:
            with ProxyActivityLease(tmp_path,1,purpose='manual_verification'):
                admitted.set();release.wait(3)
        except Exception as error:errors.append(type(error).__name__)
    worker=threading.Thread(target=manual)
    try:
        with RouteLock(tmp_path/'runtime'):
            worker.start();time.sleep(.1);assert not admitted.is_set()
        assert admitted.wait(1) and collection_active(tmp_path)
        with pytest.raises(NetworkNotReady):prepare(tmp_path,{'proxy':{}},lambda p,d:{},lambda p,v:pytest.fail('must not commit'),observation=lambda:pytest.fail('must not probe'))
        assert not errors
    finally:release.set();worker.join(3)

def test_chain_adoption_declares_same_byte_lock_and_checks_activity_before_stop():
    root=pathlib.Path(__file__).resolve().parents[1]
    script=(root/'scripts/proxyshare_chain_manager.ps1').read_text(encoding='utf-8')
    assert "runtime\\local-proxy-route.lock" in script and '.Lock(0, 1)' in script
    segment=script[script.index('$routeStamp ='):]
    assert segment.index('--route-change-check')<segment.index('Stop-WorkerChain $entry')
    assert script.index('$routeGuard.Lock(0, 1)')<script.index('# Reload after locking')

@pytest.mark.skipif(os.name!='nt',reason='Windows byte-lock interoperability')
def test_powershell_and_python_contend_for_same_os_byte_lock(tmp_path):
    import shutil,subprocess
    shell=shutil.which('pwsh')
    if not shell:pytest.skip('PowerShell 7 unavailable')
    fixture=tmp_path/'byte-lock.ps1'
    fixture.write_text('''param([string]$Path)
$ErrorActionPreference='Stop'
$stream=[System.IO.File]::Open($Path,'OpenOrCreate','ReadWrite','ReadWrite')
try { $stream.Lock(0,1) } catch [System.IO.IOException] { $stream.Dispose(); exit 3 }
$stream.Unlock(0,1);$stream.Dispose();exit 0
''',encoding='utf-8')
    with RouteLock(tmp_path/'runtime') as lock:
        blocked=subprocess.run([shell,'-NoProfile','-File',str(fixture),str(lock.path)],capture_output=True,timeout=10)
        assert blocked.returncode==3
    accepted=subprocess.run([shell,'-NoProfile','-File',str(fixture),str(lock.path)],capture_output=True,timeout=10)
    assert accepted.returncode==0
