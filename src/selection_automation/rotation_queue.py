"""Durable, per-request controls. One supervisor consumes; HTTP callers only append."""
import json
import time
import uuid
from .merge_temu_snapshots import atomic_write

PENDING={'QUEUED','RUNNING'}


def read_requests(base):
    values=[]
    for path in (base/'commands/rotate').glob('*.json'):
        try: values.append(json.loads(path.read_text('utf-8')))
        except (OSError,ValueError): continue
    return sorted(values,key=lambda r:(r.get('queue_order',0),r['created_at'],r['id']))


def write(base,request):
    atomic_write(base/'commands/rotate'/f"{request['id']}.json",json.dumps(request,ensure_ascii=False))
    return request


def enqueue(base,workers,owner):
    from .process_lock import ProcessLock
    guard=ProcessLock(base/'commands/enqueue.lock')
    deadline=time.monotonic()+3
    while True:
        try: guard.__enter__();break
        except RuntimeError:
            if time.monotonic()>=deadline: raise ValueError('换session提交繁忙，请重试')
            time.sleep(.02)
    try:
        identity={k:owner[k] for k in ('pid','created')}
        requests=read_requests(base)
        occupied={w for r in requests if r['status'] in PENDING and r['owner']==identity for w in r['workers']}
        if occupied.intersection(workers):
            raise ValueError('该采集路已在排队或更换session，请等待本次结果；其他路可提交排队')
        # Wall-clock timestamps can tie on Windows or move backwards. The
        # existing append lock also serializes this durable FIFO sequence.
        order=max((request.get('queue_order',0) for request in requests),default=0)+1
        return write(base,{'id':uuid.uuid4().hex,'action':'rotate','workers':list(workers),'queue_order':order,
                          'owner':identity,'created_at':time.time(),
                          'status':'QUEUED','worker_status':{str(w):'QUEUED' for w in workers}})
    finally: guard.__exit__(None,None,None)


def update(base,request,status,**fields):
    return write(base,{**request,**fields,'status':status,'updated_at':time.time()})


def claim(base,owner,in_flight=()):
    # Different lanes may replace concurrently; per-lane ownership and the
    # shared route commit lock remain enforced by the replacement process.
    identity={k:owner[k] for k in ('pid','created')}
    requests=read_requests(base)
    occupied={w for r in requests if r['id'] in in_flight for w in r['workers']}
    for request in requests:
        if request['status'] not in PENDING: continue
        if request['id'] in in_flight: continue
        if request['owner']!=identity or request['status']=='RUNNING':
            update(base,request,'INTERRUPTED',worker_status={str(w):'INTERRUPTED' for w in request['workers']},
                   message='原监督进程已结束，请按当前任务重新提交')
            continue
        if occupied.intersection(request['workers']): continue
        return update(base,request,'RUNNING',worker_status={str(w):'RUNNING' for w in request['workers']})
    return None


def finish(base,request,success):
    states={str(w):'COMPLETE' if w in success else 'FAILED' for w in request['workers']}
    return update(base,request,'COMPLETE' if all(s=='COMPLETE' for s in states.values()) else 'FAILED',worker_status=states)


def interrupt_pending(base):
    for request in read_requests(base):
        if request['status'] in PENDING:
            update(base,request,'INTERRUPTED',worker_status={str(w):'INTERRUPTED' for w in request['workers']},
                   message='任务已停止或监督进程退出，未完成的操作未被执行成功')


def worker_requests(base):
    result={}
    for request in read_requests(base):
        for worker,status in request['worker_status'].items():
            message=request.get('message')
            if status=='FAILED':
                try:
                    batch=json.loads((base/'commands/rotation-results'/f"{request['id']}.json").read_text('utf-8'))
                    failure=next((r for r in batch.get('results',[]) if str(r.get('worker'))==worker),{})
                    reason=failure.get('reason','')
                    if reason:
                        message=reason.strip().splitlines()[-1][:500]
                except (OSError,ValueError,TypeError):
                    pass
            result[f'worker-{int(worker):02d}-new']={'request_id':request['id'],'status':status,
                'message':message,'updated_at':request.get('updated_at',request['created_at'])}
    return result
