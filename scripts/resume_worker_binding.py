"""Repair only the local driver for an existing unfinished profile binding."""
import argparse
import json
from pathlib import Path
import subprocess
import sys
import time
import urllib.error
import urllib.request
import replace_proxyshare_worker_environment as r
from selection_automation.process_lock import ProcessLock
from selection_automation.worker_runtime import replacement_guard


def resume(worker, allow_rebind=False):
    name=f'worker-{worker:02d}-new'
    manifest=json.loads(r.SESSIONS.read_text('utf-8'))
    from selection import identity
    if manifest.get('machine_id')!=identity():
        raise RuntimeError('Browser manifest belongs to another computer')
    row=next(x for x in manifest['workers'] if x['name']==name)
    path=r.ROOT/'runtime'/'proxyshare-chains'/f'replacement-worker-{worker}.json'
    journal=json.loads(path.read_text('utf-8')) if path.exists() else {}
    if allow_rebind and not journal:journal={'phase':'REPLACED','new_profile_id':row['profile_id']}
    phases={'PROFILE_CREATED','BIND_FAILED'} | ({'REPLACED'} if allow_rebind else set())
    if journal.get('phase') not in phases or journal.get('new_profile_id')!=row['profile_id']:
        raise RuntimeError('No matching unfinished binding; refusing to change profiles')
    with ProcessLock(r.ROOT/'runtime'/f'replacement-{worker}.lock'):
        with replacement_guard(r.ROOT/'runtime'/'worker-leases',name,None):
            try:
                for attempt in range(1):
                    started=r.start_profile_with_retry(r.api_base(),row['profile_id'],attempts=1)
                    driver=str(started.get('webdriver') or '')
                    if not Path(driver).is_file(): raise RuntimeError('Browser returned no usable driver')
                    port=int(row['webdriver_url'].rsplit(':',1)[1])
                    result=r.run_background_launcher([r.powershell(),'-NoProfile','-File',str(r.ROOT/'scripts'/'bind_flower_worker.ps1'),
                        '-WorkerName',name,'-ProfileId',row['profile_id'],'-WebDriverPort',str(port),
                        '-GeckoDriverPath',driver,'-MarionettePort',str(started.get('marionette_port') or 0),'-WaitSeconds','60'],cwd=r.ROOT,timeout=75)
                    if not result.returncode: break
                    error=(result.stderr or result.stdout or 'Driver binding failed')[-1000:]
                    raise RuntimeError(error)
                r.atomic_json(path,{**journal,'phase':'REPLACED','local_binding_recovered':True})
                mark_rebound_worker(row)
                print(json.dumps({'worker':name,'status':'BOUND_EXISTING_PROFILE'}))
            except Exception as error:
                r.atomic_json(path,{**journal,'phase':'BIND_FAILED','bind_error':str(error)[-1000:]})
                raise

def mark_rebound_worker(row):
    manifest=json.loads(r.SESSIONS.read_text(encoding='utf-8'))
    key=manifest.get('task_key')
    if not key:return
    path=r.ROOT/'runs'/key/'temu/supervisors'/f"{row['name']}.json"
    if not path.exists():return
    state=json.loads(path.read_text(encoding='utf-8'))
    r.atomic_json(path,{**state,'status':'RESUMING','query_status':'RESUMING',
        'manual_action_required':False,'automatic_resume_pending':True,'recovery_budget_exhausted':False})


def driver_connected(row):
    try:
        sid=(r.ROOT/row['session_id_file']).read_text(encoding='utf-8').strip()
        request=urllib.request.Request(row['webdriver_url']+'/session/'+sid+'/url')
        with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(request,timeout=3) as response:
            return isinstance(json.load(response).get('value'),str)
    except (OSError,ValueError,KeyError):return False


def invalid_session(row):
    """A real verification page is never evidence of a broken driver session."""
    try:
        sid=(r.ROOT/row['session_id_file']).read_text(encoding='utf-8').strip()
        request=urllib.request.Request(row['webdriver_url']+'/session/'+sid+'/url')
        with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(request,timeout=5):return False
    except urllib.error.HTTPError as error:
        try:return error.code==404 and json.load(error).get('value',{}).get('error')=='invalid session id'
        except (ValueError,OSError):return False
    except urllib.error.URLError as error:
        return isinstance(error.reason,ConnectionRefusedError) or getattr(error.reason,'winerror',None)==10061
    except ConnectionRefusedError:return True
    except (OSError,KeyError,ValueError):return False


def profile_closed(row):
    import psutil
    import urllib.parse
    from selection_automation.profile_single_use import closed_used_profile
    if closed_used_profile(r.ROOT,row.get('profile_id')):return True
    data=r.api_call(r.api_base(),'/api/v1/browser/active?'+urllib.parse.urlencode({'user_id':row['profile_id']}),timeout=5).get('data') or {}
    if str(data.get('status','')).lower()!='inactive':return False
    for process in psutil.process_iter(['name','cmdline']):
        if str(process.info['name']).lower()!='flowerbrowser.exe':continue
        for arg in process.info['cmdline'] or []:
            if arg.startswith('--user-data-dir='):
                folder=arg.split('=',1)[1].replace('\\','/').rsplit('/',1)[-1].strip(chr(34))
                if folder==row['profile_id'] or folder.startswith(row['profile_id']+'_'):return False
    return True


def repair_waiting(base, row, last_attempt, now=None):
    now=time.monotonic() if now is None else now
    name=row['name']
    state=json.loads((base/'temu/supervisors'/f'{name}.json').read_text(encoding='utf-8'))
    if state.get('status') not in {'HUMAN_ACTION_REQUIRED','WAITING_BROWSER_CONTROL_RECOVERY','BINDING_FAILED','RUNNING','RESUMING','STARTING'}:return False
    if state.get('status')=='BINDING_FAILED' and driver_connected(row):
        mark_rebound_worker(row)
        return 'BOUND_EXISTING_PROFILE'
    if now-last_attempt.get(name,-60)<60 or not invalid_session(row):return False
    last_attempt[name]=now
    number=int(name.split('-')[1])
    if profile_closed(row):
        from selection import request_rotation
        request_rotation(base,[number])
        return 'REPLACEMENT_QUEUED'
    resume(number,allow_rebind=True)
    return 'BOUND_EXISTING_PROFILE'


def monitor(category, worker=None):
    from selection import live_owner, identity
    base=Path(category).resolve().parent
    if base!=(r.ROOT/'runs'/base.name).resolve():raise RuntimeError('Task directory mismatch')
    last_attempt={}
    with ProcessLock(base/'temu/supervisors/binding-recovery-monitor.lock'):
        while not (base/'STOP').exists():
            owner=live_owner()
            if not owner or owner.get('category_key')!=base.name:return
            manifest=json.loads(r.SESSIONS.read_text(encoding='utf-8'))
            if manifest.get('machine_id')!=identity():raise RuntimeError('Browser manifest belongs to another computer')
            for row in manifest.get('workers',[]):
                if worker and row['name']!=f'worker-{worker:02d}-new':continue
                try:
                    result=repair_waiting(base,row,last_attempt)
                    if result:
                        r.atomic_json(base/'temu/supervisors/binding-recovery-monitor.json',{'worker':row['name'],'status':result,'updated_at':time.time()})
                except (OSError,ValueError,RuntimeError) as error:
                    r.atomic_json(base/'temu/supervisors/binding-recovery-monitor.json',{'worker':row['name'],'status':'RETRY_PENDING','error_type':type(error).__name__,'updated_at':time.time()})
            time.sleep(10)


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--worker',type=int,choices=range(1,33))
    parser.add_argument('--allow-rebind',action='store_true');parser.add_argument('--monitor',action='store_true');parser.add_argument('--category',type=Path)
    args=parser.parse_args()
    if args.monitor:
        if not args.category:parser.error('--monitor requires --category')
        monitor(args.category,args.worker)
    elif args.worker:resume(args.worker,args.allow_rebind)
    else:parser.error('--worker is required for one-shot repair')
