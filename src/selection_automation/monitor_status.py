"""Single background status sample; HTTP polls never wait on report/disk reads."""
from copy import deepcopy
import threading
import time


class MonitorStatus:
    def __init__(self,loader,identity,interval=2,loading_key='status_loading'):
        self.loader=loader;self.identity=identity;self.interval=interval
        self.loading_key=loading_key
        self.lock=threading.Lock();self.running=False;self.value=None;self.token=None
        self.sampled_at=0;self.attempted_at=0;self.error=False

    def get(self):
        token=self.identity();now=time.time()
        with self.lock:
            if not self.running and (token!=self.token or now-self.attempted_at>=self.interval):
                self.running=True;self.attempted_at=now
                threading.Thread(target=self._refresh,args=(token,),daemon=True).start()
            matches=self.value is not None and token==self.token
            result=deepcopy(self.value) if matches else {'ok':True,self.loading_key:True}
            result['status_sample']={'sampled_at':self.sampled_at if matches else None,
                'age_seconds':round(max(0,now-self.sampled_at),3) if matches else None,
                'refreshing':self.running,'error':self.error}
            return result

    def _refresh(self,token):
        try:
            value=self.loader()
            if not isinstance(value,dict) or value.get('ok') is not True:raise ValueError('invalid status')
            with self.lock:
                if token==self.identity():
                    self.value=value;self.token=token;self.sampled_at=time.time();self.error=False
        except Exception:
            with self.lock:self.error=True
        finally:
            with self.lock:self.running=False
