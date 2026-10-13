"""Durable daily fresh-collection queue; never preempts a task or enables paid vision."""
from datetime import datetime, timedelta, timezone
import hashlib,json,sys
from pathlib import Path
from .commercial import read_json,report_signature
from .merge_temu_snapshots import atomic_write
from .process_lock import ProcessLock

BEIJING=timezone(timedelta(hours=8))
STARTED={'START_REQUESTED','RUNNING','WAITING_SITE_VERIFICATION','WAITING_THIRD_LAYER','COMPLETE','FAILED','STOPPED_INCOMPLETE','START_OUTCOME_UNKNOWN'}


def job_path(root,source_key,now):
    return Path(root)/'runtime/operations/collection'/now.astimezone(BEIJING).date().isoformat()/(source_key+'.json')


def collection_status(root,source_key,now=None):
    now=now or datetime.now(timezone.utc)
    return read_json(job_path(root,source_key,now))


class SelectionBackend:
    def __init__(self,root):
        sys.path.insert(0,str(root/'scripts'))
        import selection
        if selection.ROOT.resolve()!=root.resolve():raise RuntimeError('Daily collection entrypoint root mismatch')
        self.selection=selection;self.root=root
    def owner(self):return self.selection.live_owner()
    def start(self,category):
        path=self.root/'runtime/operations/collection-configs'/(category['category_key']+'.json')
        atomic_write(path,json.dumps(category,ensure_ascii=False,indent=2))
        frozen,cat,base=self.selection.prepare(path)
        return self.selection.request_start(frozen,cat,base,False)


def collection_tick(root,*,now=None,backend=None):
    root=Path(root);now=now or datetime.now(timezone.utc);local=now.astimezone(BEIJING)
    folder=root/'runtime/operations';rows=read_json(folder/'subscriptions.json',{'subscriptions':[]})['subscriptions']
    schedules={}
    for row in rows:
        if not row.get('enabled') or not row.get('collect_daily',True):continue
        hour,minute=map(int,row['time'].split(':'))
        delivery=local.replace(hour=hour,minute=minute,second=0,microsecond=0)
        start=delivery-timedelta(minutes=row.get('lead_minutes',120))
        old=schedules.get(row['category_key'])
        if old is None or start<old[0]:schedules[row['category_key']]=(start,delivery)
    changed=[]
    with ProcessLock(folder/'collection.lock'):
        for source_key,(start,delivery) in sorted(schedules.items()):
            if local<start:continue
            path=job_path(root,source_key,now);job=read_json(path);prior=dict(job)
            if job.get('status') in {'COMPLETE','FAILED','STOPPED_INCOMPLETE','START_OUTCOME_UNKNOWN','SKIPPED_WINDOW'}:continue
            backend=backend or SelectionBackend(root)
            owner=backend.owner()
            if job.get('status') in STARTED:
                run=root/'runs'/job['run_key']
                if owner and owner.get('category_key')==job['run_key']:
                    gates=[]
                    for progress in (run/'temu/workers').glob('*/*/progress.json'):
                        data=read_json(progress);status=str(data.get('status',''))
                        if data.get('manual_action_required') or any(word in status for word in ('CAPTCHA','VERIFICATION','LOGIN','STOPPED_HTTP')):
                            gates.append(status)
                    job.update(status='WAITING_SITE_VERIFICATION' if gates else 'RUNNING',
                               message='站点验证等待中，保留浏览器，不换会话重试。' if gates else '今日采集与前两层处理中。',gates=gates)
                else:
                    status=read_json(run/'status.json').get('状态','')
                    if str(status).startswith('完成'):
                        report=read_json(run/'commercial_report.json')
                        products=report.get('products',[])
                        category=read_json(run/'category.json')
                        current=category and report.get('evidence_signature')==report_signature(root,category)
                        done=bool(current and products) and all(p.get('match',{}).get('expected') and not p['match'].get('pending') and not p['match'].get('possible') for p in products)
                        job.update(status='COMPLETE' if done else 'WAITING_THIRD_LAYER',message='今日采集完成，运营候选仍须通过当前证据核验。' if done else '采集结束，第三层证据尚未完成；未自动启动付费分析。')
                    else:
                        job.update(status='START_OUTCOME_UNKNOWN' if job['status']=='START_REQUESTED' else 'STOPPED_INCOMPLETE',
                                   message='今日采集启动或运行已中断，保留记录，不自动重复创建环境。')
            elif local>=delivery:
                job={'status':'SKIPPED_WINDOW','source_key':source_key,'date':local.date().isoformat(),
                     'message':'今日推荐时刻已到，采集窗口已错过，不补开重复任务。'}
            elif owner:
                job={'status':'QUEUED_WAITING_TASK','source_key':source_key,'date':local.date().isoformat(),
                     'waiting_for':owner.get('category_key'),'message':'已有任务未结束，今日采集排队；保留当前浏览器。'}
            else:
                category=read_json(root/'runs'/source_key/'category.json')
                if not category:
                    job={'status':'FAILED','source_key':source_key,'message':'订阅品类的已审核规则不存在。'}
                else:
                    run_key='daily_'+hashlib.sha256(source_key.encode()).hexdigest()[:16]+'_'+local.strftime('%Y%m%d')
                    category={**category,'category_key':run_key,'daily_source_key':source_key}
                    job={'status':'START_REQUESTED','source_key':source_key,'run_key':run_key,'date':local.date().isoformat(),
                         'requested_at':now.isoformat(),'paid_vision':False,'message':'今日采集已登记，启动结果待确认。'}
                    atomic_write(path,json.dumps(job,ensure_ascii=False,indent=2))
                    try:
                        result=backend.start(category)
                        if result.get('状态')=='启动中':job.update(status='RUNNING',launch=result,message='今日采集已启动，使用新任务环境，未启动付费视觉。')
                        elif result.get('后台'):job.update(status='QUEUED_WAITING_TASK',waiting_for=result['后台'].get('category_key'),message='启动锁发现已有任务，继续排队。')
                        else:job.update(status='FAILED',message='今日采集启动失败，查看任务日志。',launch=result)
                    except Exception as error:job.update(status='FAILED',message='今日采集启动失败，保留断点。',error=type(error).__name__)
            if job.get('status')=='QUEUED_WAITING_TASK' or prior.get('queued_at'):
                job['queued_at']=prior.get('queued_at') or now.isoformat()
            job.update(scheduled_for=start.isoformat(),delivery_due=delivery.isoformat(),
                       status_changed_at=prior.get('status_changed_at',now.isoformat()) if prior.get('status')==job.get('status') else now.isoformat())
            if read_json(path)!=job:
                atomic_write(path,json.dumps(job,ensure_ascii=False,indent=2));changed.append(job)
    return changed
