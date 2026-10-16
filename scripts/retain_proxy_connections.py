"""Keep owned browser proxy routes online independently of collector status."""
import json
import os
from pathlib import Path
import sys
import time
import psutil
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'src'))
from selection_automation.process_lock import ProcessLock
from selection_automation.proxy_activity import ProxyActivityLease

def owned_chains(root):
    result={}
    for path in (root/'runtime/proxyshare-chains').glob('worker-*.json'):
        try:
            row=json.loads(path.read_text(encoding='utf-8-sig'))
            p=psutil.Process(row['pid'])
            if any(str(root/'scripts/proxyshare_chain_proxy.mjs').lower()==a.strip('"').lower() for a in p.cmdline()):
                result[int(row['worker'])]=(p.pid,p.create_time())
        except (OSError,ValueError,KeyError,psutil.Error):pass
    return result

def ensure_running(root):
    """Reuse or start the independent lease owner; never start/stop a chain."""
    import subprocess
    root=Path(root)
    if not owned_chains(root):return None
    script=root/'scripts/retain_proxy_connections.py'
    def current():
        try:
            row=json.loads((root/'runtime/proxy-connections.json').read_text('utf-8'))
            process=psutil.Process(row['pid'])
            if (abs(process.create_time()-row['created'])<.01 and
                any(str(script).lower()==arg.strip('"').lower() for arg in process.cmdline())):
                return process.pid
        except (OSError,ValueError,KeyError,psutil.Error):pass
        return None
    existing=current()
    if existing:return existing
    log=root/'runtime/proxy-connections-helper.log'
    with log.open('ab') as stream:
        subprocess.Popen([sys.executable,str(script)],cwd=root,stdout=stream,stderr=stream,
                         creationflags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0)
    deadline=time.monotonic()+3
    while time.monotonic()<deadline:
        existing=current()
        if existing:return existing
        time.sleep(.1)
    raise RuntimeError('Independent proxy lease owner did not become ready')

def stop_owned(root, timeout=5):
    """Stop only this install's recorded lease helper and wait for lease release."""
    root=Path(root); descriptor=root/'runtime/proxy-connections.json'
    try:
        row=json.loads(descriptor.read_text('utf-8'))
        process=psutil.Process(int(row['pid']))
        if abs(process.create_time()-float(row['created']))>=.01:return False
        script=str(root/'scripts/retain_proxy_connections.py').lower()
        if not any(script==arg.strip('"').lower() for arg in process.cmdline()):return False
    except (OSError,ValueError,KeyError,psutil.Error):
        return False
    process.terminate()
    try:process.wait(timeout=timeout)
    except psutil.TimeoutExpired:
        # The PID and creation time were checked immediately before termination.
        process.kill();process.wait(timeout=timeout)
    try:
        latest=json.loads(descriptor.read_text('utf-8'))
        if latest.get('pid')==row['pid'] and abs(float(latest.get('created',0))-float(row['created']))<.01:
            descriptor.unlink(missing_ok=True)
    except (OSError,ValueError,TypeError):pass
    return True


def main(root=ROOT):
    leases={}
    try:
        with ProcessLock(root/'runtime/proxy-connections.lock'):
            (root/'runtime/proxy-connections.json').write_text(json.dumps({'pid':os.getpid(),'created':psutil.Process().create_time()}),encoding='utf-8')
            empty=0
            while empty<30:
                chains=owned_chains(root);empty=0 if chains else empty+1
                for number,(identity,lease) in list(leases.items()):
                    if chains.get(number)!=identity or not lease.path.exists() or not lease.thread.is_alive():
                        lease.__exit__(None,None,None);del leases[number]
                for number,identity in chains.items():
                    if number not in leases:
                        leases[number]=(identity,ProxyActivityLease(root,number,'manual_verification').__enter__())
                time.sleep(.5)
    except RuntimeError:
        if leases:raise
    finally:
        for _,lease in leases.values():lease.__exit__(None,None,None)
if __name__=='__main__':main()
