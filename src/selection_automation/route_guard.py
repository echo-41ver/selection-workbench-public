"""One OS byte lock shared by route commits, collector admission and chain adoption."""
import json,time
from pathlib import Path
from .process_lock import ProcessLock

class RouteLock:
    def __init__(self,runtime,timeout=120):
        self.path=Path(runtime)/'local-proxy-route.lock';self.timeout=timeout;self.lock=None
    def __enter__(self):
        deadline=time.monotonic()+self.timeout
        while True:
            lock=ProcessLock(self.path)
            try:lock.__enter__();self.lock=lock;return self
            except RuntimeError:
                if time.monotonic()>=deadline:raise RuntimeError('本机出口正在准备或采用中，请稍后重试') from None
                time.sleep(.05)
    def __exit__(self,*args):
        if self.lock:self.lock.__exit__(*args)

def collection_active(root):
    import psutil
    runtime=Path(root)/'runtime'
    for path in (runtime/'worker-leases').glob('*.owner.json'):
        try:
            value=json.loads(path.read_text(encoding='utf-8'))
            if value.get('purpose') in {'collector','manual_verification'} and psutil.pid_exists(value['pid']):return True
        except (OSError,ValueError,KeyError,TypeError):return True
    for path in (runtime/'proxyshare-chains/activity').glob('worker-*/*.json'):
        try:
            value=json.loads(path.read_text(encoding='utf-8'))
            if value.get('purpose') in {'collector','manual_verification'} and value['expires_at']>time.time() and psutil.pid_exists(value['pid']):return True
        except (OSError,ValueError,KeyError,TypeError):return True
    return False
