import threading
import time
from selection_automation.monitor_status import MonitorStatus


def test_slow_disk_status_does_not_block_http_polls_or_duplicate_work():
    started=threading.Event();release=threading.Event();calls=[]
    def load():
        calls.append(1);started.set();release.wait(2)
        return {'ok':True,'workers':[{'status':'WAITING_INITIAL_PRODUCTS'}]}
    cache=MonitorStatus(load,lambda:'task')
    before=time.monotonic();assert cache.get()['status_loading']
    assert time.monotonic()-before<.2 and started.wait(1)
    for _ in range(20):assert cache.get()['status_loading']
    assert len(calls)==1
    release.set()
    for _ in range(100):
        result=cache.get()
        if not result.get('status_loading'):break
        time.sleep(.005)
    assert result['workers'][0]['status']=='WAITING_INITIAL_PRODUCTS'
    result['workers'].clear()
    assert cache.get()['workers']


def test_changed_task_never_receives_previous_task_status():
    started=threading.Event();release=threading.Event();identity=['old']
    def load():started.set();release.wait(2);return {'ok':True,'category_key':'old'}
    cache=MonitorStatus(load,lambda:identity[0]);cache.get();assert started.wait(1)
    identity[0]='new';assert cache.get()['status_loading'];release.set()
    time.sleep(.02)
    assert cache.get().get('category_key')!='old'


def test_stale_status_has_age_and_refresh_state():
    cache=MonitorStatus(lambda:{'ok':True},lambda:'task',interval=100)
    cache.value={'ok':True};cache.token='task';cache.sampled_at=time.time()-30;cache.attempted_at=time.time()
    assert cache.get()['status_sample']['age_seconds']>=30
