"""Company provisioning and automatic local-network setup (never prints keys)."""
import argparse
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import time

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'src'))
from selection_automation.secrets import store,load
from selection_automation.workers import worker_count as validate_worker_count
from selection_automation.background_launch import run_background_launcher


def rebind_existing_worker(base, worker, port, api):
    """Rebind a running profile without ever closing and reopening it."""
    import urllib.parse
    from replace_proxyshare_worker_environment import powershell
    profile = str(worker['profile_id'])
    for attempt in range(1):
        started = api(base, '/api/v1/browser/start?' + urllib.parse.urlencode({
            'user_id':profile,'ip_tab':'0','last_opened_tabs':'1','proxy_detection':'0','headless':'0'}), timeout=45).get('data',{})
        driver = started.get('webdriver')
        if not driver or not Path(driver).is_file():
            raise RuntimeError('AdsPower 未返回 Firefox WebDriver；检查公司内核权限')
        result = run_background_launcher([powershell(),'-NoProfile','-ExecutionPolicy','Bypass','-File',str(ROOT/'scripts/bind_flower_worker.ps1'),
            '-WorkerName',worker['name'],'-ProfileId',profile,'-MarionettePort',str(started.get('marionette_port') or 0),
            '-WebDriverPort',str(port),'-GeckoDriverPath',driver], cwd=ROOT, timeout=145)
        if not result.returncode: return
        message = (result.stderr or '') + (result.stdout or '')
        raise RuntimeError(f"{worker['name']} 浏览器绑定失败；保留仍打开的环境，禁止关闭后复用；原因：{message[-1000:]}")


def find_adspower_executable(company=None):
    company = company or {}
    candidates = [os.getenv('ADSPOWER_EXECUTABLE', ''), company.get('adspower_executable', '')]
    for location in ('ProgramFiles', 'ProgramFiles(x86)', 'LOCALAPPDATA'):
        directory = os.getenv(location)
        if not directory:
            continue
        for folder in ('AdsPower Global', 'AdsPower', 'Programs/AdsPower Global', 'Programs/AdsPower'):
            for name in ('AdsPower Global.exe', 'AdsPower.exe'):
                candidates.append(str(Path(directory) / folder / name))
    for value in candidates:
        if value:
            candidate = Path(os.path.expandvars(value)).expanduser()
            if candidate.is_file():
                return str(candidate)
    return None


def free_block(start,count=4):
    for base in range(start,start+1000,count):
        sockets=[]
        try:
            for port in range(base,base+count):
                s=socket.socket(); sockets.append(s); s.bind(('127.0.0.1',port))
            return list(range(base,base+count))
        except OSError: pass
        finally:
            for s in sockets: s.close()
    raise RuntimeError('No free local port block')


def socks_available(host,port):
    try:
        with socket.create_connection((host,port),timeout=1) as s:
            s.sendall(b'\x05\x01\x00'); return s.recv(2)==b'\x05\x00'
    except OSError: return False


def ensure_adspower_api(company):
    from replace_proxyshare_worker_environment import api_call
    base=company.get('adspower_api_url','http://127.0.0.1:50325')
    # Authentication must be present on the first probe, including a fresh install.
    os.environ['ADSPOWER_API_KEY']=company['adspower_api_key']
    try:
        api_call(base,'/status',timeout=3)
    except RuntimeError:
        exe=find_adspower_executable(company)
        if not exe: raise RuntimeError('AdsPower 未安装，运行 deploy.cmd')
        from urllib.parse import urlparse
        port=urlparse(base).port or 50325
        arguments=[exe,'--headless=true','--api-key='+company['adspower_api_key'],f'--api-port={port}']
        if os.getenv('ADSPOWER_USER_DATA_DIR'):
            arguments.append('--user-data-dir='+os.environ['ADSPOWER_USER_DATA_DIR'])
        from selection_automation.flower_background import startup_environment
        subprocess.Popen(arguments,cwd=str(Path(exe).parent),env=startup_environment(os.environ),
                         stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,creationflags=subprocess.CREATE_NO_WINDOW)
        deadline=time.monotonic()+60
        while True:
            try:
                api_call(base,'/status',timeout=2)
                break
            except RuntimeError:
                if time.monotonic()>=deadline: raise RuntimeError('AdsPower API 未就绪；检查公司 API 权限或首次账号授权')
                time.sleep(2)
    os.environ['ADSPOWER_API_KEY']=company['adspower_api_key']
    api_call(base,'/api/v1/user/list?page=1&page_size=1',timeout=5)


def resume_workers(root, task_key, manifest, machine_id, worker_count):
    """Keep completed lanes retired and return only this run's remaining profiles."""
    import re
    from selection import read
    if (not re.fullmatch('[a-z][a-z0-9_]{0,63}',task_key) or
            manifest.get('machine_id')!=machine_id or manifest.get('task_key')!=task_key or
            len(manifest.get('workers',[]))!=worker_count):
        raise RuntimeError('不能续接其他任务、安装或不同路数的浏览器清单')
    run=root/'runs'/task_key
    finalized=read(run/'temu/keyword_plan.json',{}).get('finalized') is True
    from selection_automation.work_dispatch import available
    assignments=read(run/'temu/supervisors/assignments.json',{})
    remaining=[]
    for worker in manifest['workers']:
        state=read(root/'runs'/task_key/'temu/supervisors'/f"{worker['name']}.json",{})
        if finalized and state.get('status')=='WORKER_COMPLETE' and not available(assignments,worker['name']):
            continue
        remaining.append(worker)
    return remaining


def prepared_for_task(root,task_key,machine_id,read):
    """Authorize saved-route validation, never reuse of the checkpoint's profiles."""
    if not task_key:return False
    manifest=read(root/'config/sessions.json',{})
    journal=read(root/'runtime/bootstrap.json',{})
    for checkpoint in (manifest,journal):
        if (checkpoint.get('machine_id')==machine_id and checkpoint.get('task_key')==task_key
            and checkpoint.get('workers')
            and (checkpoint is manifest or checkpoint.get('proxy_sessions_ready') is True)):
            return True
    return False


def verify_prepared_bindings(root,read):
    import urllib.request
    manifest=read(root/'config/sessions.json',{})
    if not manifest.get('workers'):raise RuntimeError('初始化未生成浏览器清单')
    opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))
    for worker in manifest['workers']:
        try:
            sid=(root/worker['session_id_file']).read_text(encoding='utf-8').strip()
            with opener.open(worker['webdriver_url']+'/session/'+sid+'/url',timeout=3) as response:
                healthy=isinstance(json.load(response).get('value'),str)
            if not healthy:raise ValueError('no healthy browser URL')
        except (OSError,ValueError) as error:
            raise RuntimeError(f"{worker['name']} 已登记但绑定未验证；环境与断点已保留") from error
    return manifest


def cleanup_ended_task(task_key, *, preparing_task=None):
    """Idempotently release one explicitly ended task's owned profiles and proxy leases."""
    from selection import identity,live_owner,read,save
    from retain_proxy_connections import owned_chains,stop_owned
    manifest_path=ROOT/'config'/'sessions.json'
    manifest=read(manifest_path,{})
    if not manifest or manifest.get('task_key')!=task_key:return False
    if manifest.get('machine_id')!=identity():raise RuntimeError('拒绝清理其他安装的环境')
    owner=live_owner()
    if owner and (not preparing_task or preparing_task==task_key or owner.get('category_key')!=preparing_task):
        raise RuntimeError('监督器仍在运行，结束后再清理浏览器环境')
    numbers={int(w.get('worker',i+1)) for i,w in enumerate(manifest.get('workers',[]))}
    chains=owned_chains(ROOT)
    if not set(chains).issubset(numbers):raise RuntimeError('发现不属于该任务环境清单的代理链，拒绝停止')
    stop_owned(ROOT)
    if chains:
        shell=shutil.which('pwsh')
        if not shell:raise RuntimeError('缺少 PowerShell，未能停止任务代理链')
        for number in sorted(chains):
            subprocess.run([shell,'-NoProfile','-File',str(ROOT/'scripts'/'proxyshare_chain_manager.ps1'),
                            '-Action','stop','-Worker',str(number)],cwd=ROOT,check=True,stdout=subprocess.DEVNULL)
    from selection_automation.proxy_activity import revoke_worker_activity
    for number in numbers:revoke_worker_activity(ROOT,number)
    manifest['used']=True
    save(manifest_path,manifest)
    from setup_proxyshare_adspower_workers import retire_for_task
    retire_for_task(preparing_task or task_key)
    return True


def reject_retired_task_profiles(root, task_key):
    """A deleted lane profile is terminal for this task and cannot be rebound."""
    from selection import read
    directory=Path(root)/'runs'/task_key/'temu'/'supervisors'
    for path in directory.glob('*.json'):
        state=read(path,{})
        if state.get('deleted_profile_id'):
            raise RuntimeError(f"任务 {task_key} 的采集环境已退役，不能再次启动或绑定已删除环境：{path.stem}")


def restore_closed_task_workers(task_key, workers):
    from selection import read,save
    from selection_automation.profile_single_use import closed_used_profile
    names={worker['name'] for worker in workers}
    for worker in workers:
        if closed_used_profile(ROOT,worker.get('profile_id')):
            number=int(worker['name'].split('-')[1])
            subprocess.run([sys.executable,str(ROOT/'scripts/replace_proxyshare_worker_environment.py'),
                '--worker',str(number),'--task-key',task_key],cwd=ROOT,check=True)
            state_path=ROOT/'runs'/task_key/'temu/supervisors'/f"{worker['name']}.json"
            state=read(state_path,{})
            if state:save(state_path,{**state,'status':'RESUMING','query_status':'RESUMING',
                'manual_action_required':False,'automatic_resume_pending':True,'recovery_budget_exhausted':False})
    manifest=read(ROOT/'config/sessions.json',{})
    return [worker for worker in manifest.get('workers',[]) if worker['name'] in names]


def ensure_company(start=False,new_task=None,worker_count=4,resume_task=None,preserve_task=None):
    worker_count=validate_worker_count(worker_count)
    from selection import identity,read,save
    secret=ROOT/'runtime'/'secrets'/'company.dpapi'
    if not secret.exists(): raise RuntimeError('缺少公司初始化配置：运行 install.ps1 -CompanyConfig <公司配置文件>')
    company=load(secret)
    from selection_automation.browser_transport import local_vpn
    os.environ['ADSPOWER_API_KEY']=company['adspower_api_key']
    os.environ['ADSPOWER_LOCAL_API_URL']=company.get('adspower_api_url','http://127.0.0.1:50325')
    model_secret=ROOT/'runtime'/'secrets'/'deepseek.dpapi'
    if model_secret.exists():
        from selection_automation.company_bundle import apply_model_settings
        saved=load(model_secret)
        if not os.environ.get('DEEPSEEK_API_KEY'):apply_model_settings(saved,os.environ)
    if not start: return
    if new_task and resume_task: raise ValueError('新任务与续接任务不能同时指定')
    if preserve_task:
        if new_task or resume_task:raise ValueError('维护续接不能同时创建新环境')
        resume_task=preserve_task
    network_task=resume_task or new_task
    if resume_task and not preserve_task:
        new_task, resume_task = resume_task, None
    if new_task:
        reject_retired_task_profiles(ROOT,new_task)
        from selection import identity,live_owner,read as task_read
        manifest=task_read(ROOT/'config'/'sessions.json',{})
        previous=manifest.get('task_key') if manifest.get('machine_id')==identity() else None
        if previous and previous!=new_task:
            status=task_read(ROOT/'runs'/previous/'status.json',{}).get('状态','')
            if status.startswith('完成') or status=='已停止':
                owner=live_owner()
                if owner and owner.get('category_key')!=new_task:
                    raise RuntimeError('旧任务监督器仍在运行，暂不退役其浏览器环境')
                cleanup_ended_task(previous,preparing_task=new_task)
    resumed_workers=None
    if resume_task:
        resumed_workers=resume_workers(ROOT,resume_task,read(ROOT/'config/sessions.json',{}),identity(),worker_count)
        from selection_automation.worker_runtime import clear_stale_stop_requests
        clear_stale_stop_requests(ROOT/'runtime'/'worker-leases',[w['name'] for w in resumed_workers])
    from replace_proxyshare_worker_environment import api_call
    base=os.environ['ADSPOWER_LOCAL_API_URL']
    from selection_automation.local_proxy_prepare import prepare,reuse_prepared
    if prepared_for_task(ROOT,network_task,identity(),read):
        transport,route=reuse_prepared(ROOT,company,read)
    else:
        transport,route=prepare(ROOT,company,read,save)
    print(json.dumps({'network_readiness':route},ensure_ascii=False),flush=True)
    ensure_adspower_api(company)
    from selection_automation.adspower_kernel import ensure_kernel
    ensure_kernel(base,api_call,ROOT)
    from selection_automation.flower_background import install_defaults
    background=install_defaults(company)
    save(ROOT/'runtime/background-execution.json',background)
    print(json.dumps({'background_execution':background},ensure_ascii=False),flush=True)
    task_args=['--new-task',new_task] if new_task else []
    if new_task:
        from setup_proxyshare_adspower_workers import retire_for_task
        retire_for_task(new_task)
        from selection_automation.worker_runtime import clear_stale_stop_requests
        clear_stale_stop_requests(ROOT/'runtime'/'worker-leases',
                                  [f'worker-{number:02d}-new' for number in range(1,worker_count+1)])
    manifest=read(ROOT/'config'/'sessions.json',{})
    if preserve_task:
        resumed_workers=restore_closed_task_workers(preserve_task,resumed_workers)
        manifest=read(ROOT/'config/sessions.json',{})
    if resume_task:
        from selection_automation.profile_capacity import list_profiles
        known={str(row.get('user_id') or row.get('profile_id') or '') for row in list_profiles(api_call,base)}
        if any(str(worker.get('profile_id')) not in known for worker in resumed_workers):
            raise RuntimeError('未完成采集路的原浏览器环境缺失，已保留断点；未自动重建环境')
    if manifest and new_task:
        if manifest.get('machine_id')!=identity(): raise RuntimeError('拒绝复用其他安装的 AdsPower 环境')
        from selection_automation.profile_capacity import list_profiles
        known={str(row.get('user_id') or row.get('profile_id') or '') for row in list_profiles(api_call,base)}
        missing=[w for w in manifest.get('workers',[]) if str(w.get('profile_id')) not in known]
        if missing:
            # Retire only this installation's manifest, then rebuild from a clean journal.
            # Never bind an ID just because an old checkpoint says BOUND.
            manifest['used']=True
            save(ROOT/'config'/'sessions.json',manifest)
            retire_for_task(new_task)
            manifest={}
    if manifest:
        if manifest.get('machine_id')!=identity(): raise RuntimeError('拒绝复用其他安装的 AdsPower 环境')
        if new_task and len(manifest.get('workers',[]))!=worker_count:
            raise RuntimeError('本任务环境路数与请求不一致，请重新创建任务')
        # Restart local proxy chains; never infer browser readiness from a saved manifest.
        if not local_vpn(company):
            subprocess.run([shutil.which('pwsh'),' -NoProfile'.strip(),'-File',str(ROOT/'scripts'/'proxyshare_chain_manager.ps1'),'-Action','start'],cwd=ROOT,check=True,stdout=subprocess.DEVNULL)
        import urllib.request, urllib.parse
        policy=read(ROOT/'runtime'/'proxy_policy.json')
        for index,worker in enumerate(manifest['workers']):
            if resumed_workers is not None and worker not in resumed_workers: continue
            healthy=False
            try:
                sid=(ROOT/worker['session_id_file']).read_text(encoding='utf-8').strip()
                with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(worker['webdriver_url']+'/session/'+sid+'/url',timeout=3) as response:
                    healthy=isinstance(json.load(response).get('value'),str)
            except (OSError,ValueError): pass
            if healthy: continue
            port=policy.get('webdriver_ports',list(range(4451,4451+worker_count)))[index]
            # A stale driver may still own its port: choose another free port rather than kill an unknown process.
            try:
                with socket.socket() as probe: probe.bind(('127.0.0.1',port))
            except OSError: port=free_block(4451+index*100,1)[0]
            rebind_existing_worker(base, worker, port, api_call)
            worker['webdriver_url']=f'http://127.0.0.1:{port}'; policy['webdriver_ports'][index]=port
        save(ROOT/'runtime'/'proxy_policy.json',policy); save(ROOT/'config'/'sessions.json',manifest)
        return
    bootstrap=read(ROOT/'runtime'/'bootstrap.json',{})
    if bootstrap:
        if bootstrap.get('machine_id')!=identity(): raise RuntimeError('不能续建其他安装的环境')
        if bootstrap.get('requested_worker_count',worker_count)!=worker_count:
            raise RuntimeError('断点路数与请求不一致，请重新创建任务')
        subprocess.run([sys.executable,str(ROOT/'scripts'/'setup_proxyshare_adspower_workers.py'),*task_args],cwd=ROOT,check=True)
        if not (ROOT/'config'/'sessions.json').exists(): raise RuntimeError('续建未生成浏览器清单')
        verify_prepared_bindings(ROOT,read)
        return
    proxy=company.get('proxy',{})
    if new_task and (ROOT/'runtime'/'proxy_policy.json').exists():
        subprocess.run([shutil.which('pwsh'),'-NoProfile','-File',str(ROOT/'scripts'/'proxyshare_chain_manager.ps1'),'-Action','stop'],cwd=ROOT,check=True,stdout=subprocess.DEVNULL)
    ports=free_block(17891,worker_count); drivers=free_block(4451,worker_count)
    policy=read(ROOT/'config'/'proxy_policy.json'); policy['worker_chain_ports']=ports; policy['webdriver_ports']=drivers
    policy['worker_count']=worker_count
    save(ROOT/'runtime'/'proxy_policy.json',policy)
    settings={'schema_version':'proxyshare-chain-settings-v1','proxy_host':proxy.get('host'),'proxy_port':proxy.get('port',0),
              'proxy_architecture':company.get('proxy_architecture','LOCAL_CHAIN_PROXYSHARE'),
              **transport,'chain_host':'127.0.0.1',
              'chain_base_port':ports[0],'worker_count':worker_count}
    save(ROOT/'runtime'/'secrets'/'proxyshare.settings.json',settings)
    # Pipe credentials into PowerShell; never put them in command arguments or logs.
    command="$v=[Console]::In.ReadToEnd()|ConvertFrom-Json; $p=ConvertTo-SecureString $v.password -AsPlainText -Force; [PSCredential]::new($v.username,$p)|Export-Clixml -LiteralPath $v.path"
    if not local_vpn(company):
        subprocess.run([shutil.which('pwsh'),'-NoProfile','-Command',command],input=json.dumps({'username':proxy['username'],'password':proxy['password'],'path':str(ROOT/'runtime'/'secrets'/'proxyshare.credential.xml')}),text=True,check=True,stdout=subprocess.DEVNULL)
    subprocess.run([sys.executable,str(ROOT/'scripts'/'setup_proxyshare_adspower_workers.py'),*task_args],cwd=ROOT,check=True)
    if not (ROOT/'config'/'sessions.json').exists(): raise RuntimeError('初始化未生成浏览器清单')
    verify_prepared_bindings(ROOT,read)


if __name__=='__main__':
    p=argparse.ArgumentParser(); p.add_argument('--import-file',type=Path); p.add_argument('--api-key-stdin',action='store_true'); p.add_argument('--start',action='store_true')
    p.add_argument('--prepare-kernel',action='store_true'); p.add_argument('--new-task'); p.add_argument('--resume-task'); p.add_argument('--preserve-task'); p.add_argument('--worker-count',type=int,default=4); p.add_argument('--find-adspower',action='store_true'); p.add_argument('--company-config',type=Path); a=p.parse_args()
    if a.find_adspower:
        company=json.loads(a.company_config.read_text(encoding='utf-8-sig')) if a.company_config else {}
        print(find_adspower_executable(company) or '')
        raise SystemExit(0)
    if a.import_file:
        from selection_automation.company_bundle import import_bundle
        value=json.loads(a.import_file.read_text(encoding='utf-8-sig'))
        import_bundle(ROOT,value,store)
        print('公司配置已按本机当前用户加密保存')
    if a.api_key_stdin:
        key=sys.stdin.read().strip()
        if not key: raise SystemExit('empty API key')
        store(ROOT/'runtime'/'secrets'/'deepseek.dpapi',{'key':key}); print('API key已加密保存')
    if a.prepare_kernel:
        ensure_company(False)
        company=load(ROOT/'runtime/secrets/company.dpapi')
        ensure_adspower_api(company)
        from replace_proxyshare_worker_environment import api_base,api_call
        from selection_automation.adspower_kernel import ensure_kernel
        print(json.dumps(ensure_kernel(api_base(),api_call,ROOT),ensure_ascii=False))
    if a.start: ensure_company(True,new_task=a.new_task,worker_count=a.worker_count,resume_task=a.resume_task,preserve_task=a.preserve_task)
