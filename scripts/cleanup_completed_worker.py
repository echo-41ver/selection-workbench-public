"""Release only profiles belonging to fully completed collection lanes."""
import json
import shutil
import subprocess
import time
import urllib.parse
from pathlib import Path


def cleanup_completed_worker(root, run, worker):
    from replace_proxyshare_worker_environment import api_base, api_call, delete_profile_after_release, atomic_json
    root, run = Path(root), Path(run)
    state_path = run / 'temu' / 'supervisors' / (worker + '.json')
    state = json.loads(state_path.read_text('utf-8')) if state_path.exists() else {}
    if not state:
        assignment_path=run/'temu/supervisors/assignments.json'
        assignment=json.loads(assignment_path.read_text('utf-8')) if assignment_path.exists() else {}
        lane=next((w for w in assignment.get('workers',[]) if w['name']==worker),None)
        if lane is not None and not lane.get('queries'):
            state={'status':'WORKER_COMPLETE','completed_queries':[],'unused_lane':True}
    plan_path = run / 'temu/keyword_plan.json'
    if not plan_path.exists() or not json.loads(plan_path.read_text('utf-8')).get('finalized'):
        return False
    if state.get('status') != 'WORKER_COMPLETE':
        return False
    from selection_automation import work_dispatch
    dispatch=work_dispatch.read(run)
    jobs=dispatch.get('jobs')
    if not isinstance(jobs,list) or work_dispatch.available(dispatch,worker):
        return False
    manifest = json.loads((root / 'config' / 'sessions.json').read_text('utf-8'))
    from selection import identity
    if manifest.get('task_key') != run.name or manifest.get('machine_id') != identity():
        raise RuntimeError('Refusing cleanup of another task or machine')
    session = next((w for w in manifest['workers'] if w['name'] == worker), None)
    if not session or not session.get('profile_id'):
        return False
    profile = str(session['profile_id'])
    base = api_base()
    for attempt in range(3):
        try:
            from retain_proxy_connections import owned_chains
            lane_number=next(i for i,w in enumerate(manifest['workers'],1) if w['name']==worker)
            if lane_number in owned_chains(root):
                shell=shutil.which('pwsh')
                if not shell:raise RuntimeError('PowerShell unavailable for owned proxy chain cleanup')
                subprocess.run([shell,'-NoProfile','-File',str(root/'scripts'/'proxyshare_chain_manager.ps1'),
                                '-Action','stop','-Worker',str(lane_number)],cwd=root,check=True,
                               stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
            from selection_automation.proxy_activity import revoke_worker_activity
            revoke_worker_activity(root,lane_number)
            if state.get('deleted_profile_id') != profile:
                profiles = api_call(base, '/api/v1/user/list?' + urllib.parse.urlencode(
                    {'user_id': profile, 'page': 1, 'page_size': 100})).get('data', {}).get('list')
                if not isinstance(profiles, list):
                    raise RuntimeError('Profile lookup returned invalid data')
                if any(str(p.get('user_id') or p.get('profile_id')) == profile for p in profiles):
                    try:
                        api_call(base, '/api/v1/browser/stop?' + urllib.parse.urlencode({'user_id': profile}), timeout=12)
                    except RuntimeError as error:
                        if 'not open' not in str(error).lower():
                            raise
                    delete_profile_after_release(base, profile)
                state = {**state, 'environment_cleanup': 'RETRY_REQUIRED',
                         'deleted_profile_id': profile, 'environment_deleted_at': time.time()}
                atomic_json(state_path, state)
            from selection_automation.driver_retirement import release_retired_driver
            state['driver_cleanup'] = release_retired_driver(root, session, profile)
            state['environment_cleanup'] = 'COMPLETE'
            state.pop('environment_cleanup_reason', None)
            atomic_json(state_path, state)
            return True
        except (RuntimeError,OSError,subprocess.SubprocessError) as error:
            from selection_automation.driver_retirement import DriverCleanupRequired
            state['environment_cleanup_reason'] = (str(error) if isinstance(error, DriverCleanupRequired)
                else '环境清理尚未完成，请稍后重试；采集断点已保留')
            if isinstance(error, DriverCleanupRequired):
                state['driver_cleanup'] = {'status': 'RETRY_REQUIRED', 'reason': str(error)}
            if attempt < 2:
                time.sleep(2 * (attempt + 1))
    atomic_json(state_path, {**state, 'environment_cleanup': 'RETRY_REQUIRED'})
    return False


def supervise_cleanup(root, run):
    """Keep deletion failures visible and retry independently of collection."""
    import sys
    sys.path.insert(0,str(Path(root)/'src'))
    from selection_automation.worker_runtime import WorkerLease
    from selection_automation.merge_temu_snapshots import atomic_write
    from company_setup import ensure_company
    ensure_company(start=False)
    run=Path(run)
    while not (run/'STOP').exists():
        manifest=json.loads((Path(root)/'config/sessions.json').read_text('utf-8'))
        pending=[];waiting=[]
        for worker in manifest.get('workers',[]):
            name=worker['name']
            state_path=run/'temu/supervisors'/f'{name}.json'
            try:
                current=json.loads(state_path.read_text('utf-8')) if state_path.exists() else {}
                if current and current.get('status')!='WORKER_COMPLETE':
                    waiting.append(name);continue
            except (OSError,ValueError):
                waiting.append(name);continue
            try:
                with WorkerLease(Path(root)/'runtime/worker-leases',name,purpose='cleanup'):
                    if not cleanup_completed_worker(root,run,name): pending.append(name)
            except (RuntimeError,OSError,ValueError): pending.append(name)
        atomic_write(run/'cleanup_status.json',json.dumps({'status':'RETRY_REQUIRED' if pending else 'WAITING_COLLECTION' if waiting else 'COMPLETE',
            'waiting_workers':waiting,'pending_workers':pending,'updated_at':time.time()},ensure_ascii=False))
        if not pending and not waiting: return
        for _ in range(15):
            if (run/'STOP').exists(): return
            time.sleep(1)


if __name__=='__main__':
    import argparse,sys
    root=Path(__file__).resolve().parents[1]
    sys.path.insert(0,str(root/'src'))
    parser=argparse.ArgumentParser();parser.add_argument('--run',required=True)
    args=parser.parse_args();supervise_cleanup(root,Path(args.run))
