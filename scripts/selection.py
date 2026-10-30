"""One local entrypoint used by Skills, MCP and the Windows launcher."""
from __future__ import annotations
import argparse
import hashlib
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import time
import uuid
import math

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT/'src'))
from selection_automation.category_config import load_category
from selection_automation.workers import category_worker_count
from selection_automation.rules import revision
from selection_automation.merge_temu_snapshots import atomic_write
from selection_automation.collection_contract import collection_only, validate_collection, source_completion
from selection_automation import rotation_queue
from selection_automation.runtime_audit import process_version, record_start, recovery_event
LOADED_VERSION=process_version(str(ROOT))


def save(path, value):
    atomic_write(path, json.dumps(value, ensure_ascii=False, indent=2))


def save_progress(path, value):
    """A busy progress view must not tear down the live collection processes."""
    try:
        save(path, value)
    except PermissionError as exc:
        # atomic_write already retries for two seconds and preserves the old JSON.
        # The next supervisor tick publishes the newest state instead of exiting.
        print(f'进度文件暂时无法更新，保留采集进程，下轮重试：{path.name} ({exc})', flush=True)
        return False
    return True


def read(path, default=None):
    try: return json.loads(path.read_text(encoding='utf-8-sig'))
    except (OSError, ValueError): return default


def request_task_stop(base):
    """Stop independently recovered analysis services as well as the supervisor."""
    base.mkdir(parents=True, exist_ok=True)
    # Gate recovery first. Service-local markers are needed by existing watcher
    # versions too; those processes may be children of the web service.
    atomic_write(base/'STOP', '用户请求停止')
    atomic_write(base/'stage12/STOP', '用户请求停止整个任务')
    if not (base/'stage123/STOP').exists():
        save(base/'stage123/STOP', {'time':time.time(),'reason':'whole-task-user-request'})


def identity():
    path=ROOT/'runtime'/'machine.json'
    if not path.exists(): save(path, {'id': uuid.uuid4().hex})
    return read(path)['id']


def spawn(command, log, env=None):
    if any(Path(arg).name=='collect_amazon_search_frontend.mjs' for arg in command):
        from selection_automation.browser_transport import amazon_environment
        env={**(env or {}),**amazon_environment(ROOT)}
    log.parent.mkdir(parents=True, exist_ok=True)
    child_env=os.environ.copy(); child_env.update({'PYTHONUTF8':'1','PYTHONIOENCODING':'utf-8'}); child_env.update(env or {})
    with log.open('ab') as stream:
        return subprocess.Popen(command,cwd=ROOT,stdout=stream,stderr=subprocess.STDOUT,env=child_env,
                                creationflags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0)


def preserved_connection_process(process):
    """Never terminate the retained browser/control/route subtree with collectors."""
    name=process.name().lower()
    if name in {'flowerbrowser.exe','firefox.exe','geckodriver.exe'}:return True
    return any(Path(arg.strip('"')).name.lower() in {'proxyshare_chain_proxy.mjs','retain_proxy_connections.py'}
               for arg in process.cmdline())


def terminate_owned(child):
    import psutil
    if child.poll() is not None: return
    try:
        process=psutil.Process(child.pid)
        descendants=process.children(recursive=True)
        protected=set()
        for p in descendants:
            try:
                if preserved_connection_process(p):
                    protected.add(p.pid)
                    protected.update(q.pid for q in p.children(recursive=True))
            except psutil.NoSuchProcess:pass
        children=[p for p in descendants if p.pid not in protected]
        for p in reversed(children):
            try: p.terminate()
            except psutil.NoSuchProcess: pass
        process.terminate()
        psutil.wait_procs(children+[process],timeout=5)
        for p in children+[process]:
            try:
                if p.is_running(): p.kill()
            except psutil.NoSuchProcess: pass
    except psutil.NoSuchProcess: pass


def live_owner():
    import psutil
    for filename in ('active.json', 'starting.json'):
        prior=read(ROOT/'runtime'/filename, {})
        try:
            p=psutil.Process(prior['pid'])
            if p.is_running() and p.status() != psutil.STATUS_ZOMBIE and abs(p.create_time()-prior['created'])<.01:
                return prior
        except (KeyError,psutil.Error): pass
    return None


def request_start(frozen, cat, base, paid, *, background=True, preserve_prepared=False):
    """Serialize starts and clear only the previous run's STOP before spawning."""
    import psutil
    from selection_automation.process_lock import ProcessLock
    with ProcessLock(ROOT/'runtime'/'start.lock'):
        current=live_owner()
        if current:
            owner_base=ROOT/'runs'/current['category_key']
            return {'状态':'停止中' if (owner_base/'STOP').exists() else '已有后台任务', '后台':current}
        (base/'STOP').unlink(missing_ok=True)
        save(base/'status.json', {'状态':'启动中','品类':cat['category_name_zh'], '付费第三层':paid})
        save(ROOT/'runtime'/'last_run.json', {'category_key':cat['category_key'],'started_at':time.time()})
        def failed_start(code=None, reason=None):
            failure={'状态':'启动失败','退出码':code,'日志':str(base/'logs'/'supervisor.log')}
            if reason: failure['原因']=reason
            save(base/'status.json',failure)
            pending_path=ROOT/'runtime'/'starting.json'
            if child is not None and read(pending_path,{}).get('pid')==child.pid:
                pending_path.unlink(missing_ok=True)
            return failure
        child=None
        if background and os.name=='nt':
            from persistent_web_service import start as persistent_start
            launch=persistent_start(8765,root=ROOT,resume_category=frozen,paid=paid,preserve_prepared=preserve_prepared)
            save(ROOT/'runtime/supervisor-launch.json',{**launch,'category_key':cat['category_key'],'started_at':time.time()})
            deadline=time.monotonic()+10
            while time.monotonic()<deadline:
                owner=live_owner()
                if owner and owner.get('category_key')==cat['category_key']:
                    save(ROOT/'runtime/starting.json',owner)
                    return {'状态':'启动中','PID':owner['pid'],'状态文件':str(base/'status.json'),**launch}
                time.sleep(.1)
            return {'状态':'启动中','状态文件':str(base/'status.json'),**launch}
        if background:
            cmd=[sys.executable,str(Path(__file__).resolve()),'run','--prepared-start','--category',str(frozen)]+(['--paid'] if paid else [])+(['--preserve-prepared'] if preserve_prepared else [])
            try:
                child=spawn(cmd,base/'logs'/'supervisor.log')
                if child.poll() is not None:
                    return failed_start(child.returncode)
                process=psutil.Process(child.pid)
                created=process.create_time()
            except psutil.NoSuchProcess:
                return failed_start(child.poll(),'监督进程在启动登记前退出')
            except OSError as exc:
                return failed_start(reason=type(exc).__name__)
        else:
            process=psutil.Process()
            created=process.create_time()
        pending={'pid':process.pid,'created':created,'category_key':cat['category_key']}
        save(ROOT/'runtime'/'starting.json',pending)
        if child is not None and child.poll() is not None:
            return failed_start(child.returncode,'监督进程在启动登记后退出')
        return {'状态':'启动中','PID':process.pid,'状态文件':str(base/'status.json')}


def saved_api_key_stamp():
    path=ROOT/'runtime'/'secrets'/'deepseek.dpapi'
    try:
        stat=path.stat()
        return (stat.st_mtime_ns,stat.st_size)
    except FileNotFoundError:
        return None


def refresh_saved_api_key(previous_stamp):
    path=ROOT/'runtime'/'secrets'/'deepseek.dpapi'
    if not path.exists(): return previous_stamp
    stamp=(path.stat().st_mtime_ns,path.stat().st_size)
    if stamp != previous_stamp:
        from selection_automation.secrets import load
        saved=load(path)
        value=saved['key'].strip()
        if not value: raise ValueError('保存的 API key 为空')
        os.environ['DEEPSEEK_API_KEY']=value
        from selection_automation.company_bundle import apply_model_settings
        apply_model_settings(saved,os.environ)
    return stamp


def hold_provider_block(base, *, children, finished, attempts, next_at, state):
    marker=base/'stage123'/'provider_blocked.json'
    blocked=read(marker,{})
    current_key=hashlib.sha256(os.getenv('DEEPSEEK_API_KEY','').encode()).hexdigest()
    if blocked and blocked.get('key_identity') != current_key:
        marker.unlink(missing_ok=True); blocked={}
    was_blocked=state.get('stage3',{}).get('provider_blocked',False)
    if blocked or was_blocked:
        finished.discard('stage3'); attempts['stage3']=0; next_at['stage3']=0
    if blocked:
        child=children.get('stage3')
        if child is not None and child.poll() is not None: del children['stage3']
        state['stage3']={'状态':'API服务阻断','阻断':True,'需人工介入':True,
                         'provider_blocked':True,'HTTP状态':blocked.get('status'),
                         '建议动作':'更新 API key；同 key 充值后通过恢复指令继续'}
        return True
    if was_blocked: state['stage3']={'状态':'API阻断已解除，等待恢复'}
    return False


def sync_manual_proxy(base, session, number, children, leases):
    """Keep retained browsers online while a stopped worker needs recovery."""
    name=session['name']
    _,view,_=worker_snapshot(base,name)
    child=children.get(name)
    running=child is not None and child.poll() is None
    manual=view['采集状态'] in {'WAITING_CAPTCHA','WAITING_SEARCH_VERIFICATION','WAITING_LOGIN',
                                 'STOPPED_LOGIN_REQUIRED','LOGIN_REQUIRED','CAPTCHA_REQUIRED',
                                 'STOPPED_TRY_AGAIN_STALLED','STOPPED_PROXY_OR_NETWORK_FAILURE',
                                 'STOPPED_PROXY_CONNECTION','STOPPED_BOOTSTRAP_NAVIGATION',
                                 'STOPPED_HTTP_429','STOPPED_HTTP_403','STOPPED_ACCESS_DENIED'}
    if name in leases and not leases[name].path.exists():
        # Environment replacement revokes and deletes the previous permission.
        # Never retain that dead lease instead of permitting the current browser.
        leases.pop(name).__exit__(None,None,None)
    if manual and not running and name not in leases:
        from selection_automation.proxy_activity import ProxyActivityLease
        leases[name]=ProxyActivityLease(ROOT,number,purpose='manual_verification').__enter__()
    elif (not manual or running) and name in leases:
        leases.pop(name).__exit__(None,None,None)


def union_owner_matches(child, owner_pid):
    import psutil
    if not isinstance(owner_pid,int) or isinstance(owner_pid,bool): return False
    try:
        process=psutil.Process(child.pid)
        return owner_pid in {process.pid,*(p.pid for p in process.children(recursive=True))}
    except (psutil.NoSuchProcess,psutil.AccessDenied): return False


def union_supervision(base, health, *, children, finished, attempts, state, terminate, now=None, owner_matches=union_owner_matches):
    """Reset consecutive exits only after this launch actually publishes."""
    now=time.time() if now is None else now
    health=health if isinstance(health,dict) else {}
    launched=state.get('temu_union',{}).get('launched_wall_at',now)
    child=children.get('temu_union')
    if child is not None and health.get('owner_pid') is not None:
        identity=(child.pid,health['owner_pid'])
        launch_state=state.setdefault('temu_union',{})
        if launch_state.get('verified_health_owner')!=identity:
            if owner_matches(child,health['owner_pid']): launch_state['verified_health_owner']=identity
            else: health={}
    success=health.get('last_success_at')
    success_valid=(isinstance(success,(int,float)) and not isinstance(success,bool)
                   and math.isfinite(success) and launched<=success<=now+2)
    if child is not None and success_valid:
        attempts['temu_union']=0
    if not health and child is not None and now-launched>90:
        health={'stale':True,'missing_health':True,'状态':'汇总未发布有效健康状态，恢复检查中'}
    if child is not None and health.get('stale') and now-launched>90:
        event={'event':'SUPERVISOR_TERMINATE','source':'union_health_deadline',
               'pid':child.pid,'utc_epoch':now,'heartbeat_age_seconds':health.get('heartbeat_age_seconds'),
               'merge_age_seconds':health.get('merge_age_seconds'),'missing_health':health.get('missing_health',False)}
        with (base/'logs/union_lifecycle.jsonl').open('a',encoding='utf-8') as stream:
            stream.write(json.dumps(event)+'\n');stream.flush()
        terminate(child)
    if child is None and 'temu_union' in finished:
        health={**health,'recovery_exhausted':True,'自动恢复':False,
                '状态':'汇总恢复预算耗尽，需要单独恢复','建议动作':'保留原始快照，检查退出记录后恢复汇总进程'}
    return health


def union_existing_owner(base, health):
    """Verify the exact independent owner already publishing this task's union."""
    pid=health.get('owner_pid');created=health.get('owner_created')
    if pid is None:return False
    if (type(pid) is not int or pid<=0 or type(created) not in (int,float)
            or not math.isfinite(created) or created<=0):return None
    identity=(pid,created)
    path=base/'temu/union/watch_state.json'
    def recorded_identity():
        value=read(path,{})
        return (value.get('owner_pid'),value.get('owner_created')) if isinstance(value,dict) else None
    if recorded_identity()!=identity:return None
    from local_service_recovery import service_alive
    # service_alive verifies native liveness, creation time, exact script root,
    # exactly one category argument and this task's resolved category path.
    live=service_alive(ROOT,base,'temu_union')
    if recorded_identity()!=identity:return None
    return live


def union_service_step(base, cmd, health, *, children, finished, attempts, next_at,
                       state, launch, log, terminate, now=None, existing_owner=union_existing_owner):
    """Use an independently recovered same-task writer without launching duplicates."""
    now=time.time() if now is None else now
    health=health if isinstance(health,dict) else {}
    child=children.get('temu_union')
    identity_error=None
    try:live=existing_owner(base,health) if child is None or child.poll() is not None else False
    except (OSError,ValueError,TypeError,KeyError) as error:
        live=None;identity_error=type(error).__name__
    if live is not False:
        if live is None:
            return {**health,'状态':'正在核对本任务汇总进程身份，暂不启动重复进程',
                    '阻断':True,'owner_verification_pending':True,
                    **({'owner_verification_error_type':identity_error} if identity_error else {})}
        prior_attempts=attempts.get('temu_union',0)
        if child is not None:
            # Preserve its genuine exit record and budget before considering
            # whether the verified independent owner has actually published.
            service_step('temu_union',cmd,children=children,finished=finished,
                attempts=attempts,next_at=next_at,state=state,launch=lambda *args:None,log=log)
        created=health['owner_created'];success=health.get('last_success_at')
        success_valid=(type(success) in (int,float) and math.isfinite(success)
                       and created<=success<=now+2 and health.get('stale') is False)
        if success_valid:
            attempts['temu_union']=0;finished.discard('temu_union')
        else:
            attempts['temu_union']=max(prior_attempts,attempts.get('temu_union',0))
        # This row is the verified owner's real health, not the failed duplicate's
        # retained exit. Failed/stale/missing health remains blocking.
        adopted={**health,'PID':health['owner_pid'],'owner_created':created,
                 'external_service':True,'launched_wall_at':created}
        if not health.get('状态') or health.get('stale'):
            adopted.update(阻断=True,状态='本任务现有汇总进程的健康状态待恢复',
                建议动作='保留原快照；停止任务后继续，等待现有汇总进程安全退出。')
        state['temu_union']=adopted
        return adopted
    service_step('temu_union',cmd,children=children,finished=finished,attempts=attempts,
        next_at=next_at,state=state,launch=launch,log=log,interval=10)
    return union_supervision(base,health,children=children,finished=finished,
        attempts=attempts,state=state,terminate=terminate,now=now)


def service_step(name, cmd, *, children, finished, attempts, next_at, state, launch, log, repeat=False, interval=10):
    child=children.get(name)
    if child and child.poll() is not None:
        if name=='temu_union':
            with (log/'union_lifecycle.jsonl').open('a',encoding='utf-8') as stream:
                stream.write(json.dumps({'event':'PROCESS_EXIT_OBSERVED','pid':child.pid,
                    'returncode':child.returncode,'utc_epoch':time.time(),
                    'launched_wall_at':state.get(name,{}).get('launched_wall_at')})+'\n')
        code=child.returncode; del children[name]
        if code==0:
            attempts[name]=0
            state[name]={'状态':'本轮完成'}
            if not repeat: finished.add(name)
        else:
            attempts[name]=attempts.get(name,0)+1
            state[name]={'状态':'失败，等待有界恢复','退出码':code,'日志':str(log/f'{name}.log')}
            if attempts[name]>=3: finished.add(name); state[name]['状态']='失败，需要处理'
        next_at[name]=time.monotonic()+interval
    if name not in children and name not in finished and time.monotonic()>=next_at.get(name,0): launch(name,cmd)


def stage12_existing_owner(base):
    """Return only the native, same-task watch owner with a stable identity."""
    import psutil
    from restart_web import native_process_active
    config=(base/'stage12_config.json').resolve()
    config_data=read(config,{})
    if not isinstance(config_data,dict):return None
    output_value=config_data.get('output_dir',base/'stage12')
    if not isinstance(output_value,(str,Path)):return None
    output=Path(output_value)
    if not output.is_absolute():output=ROOT/output
    if not output.resolve().is_relative_to(base.resolve()):return None
    path=output/'stage12_watch_owner.json'
    if not path.exists():return False
    record=read(path,{})
    if not isinstance(record,dict):return None
    pid=record.get('pid');created=record.get('created')
    if type(pid) is not int or pid<=0 or type(created) not in (int,float) or not math.isfinite(created) or created<=0:return None
    if not isinstance(record.get('config'),str) or Path(record['config']).resolve()!=config:return False
    try:
        process=psutil.Process(pid)
        if not native_process_active(pid) or abs(process.create_time()-created)>=.01:return False
        args=process.cmdline();script=(ROOT/'scripts/run_incremental_stage12.py').resolve()
        indexes=[i for i,arg in enumerate(args) if arg.endswith(script.name) and (ROOT/arg).resolve()==script]
        if (len(indexes)!=1 or indexes[0]+1>=len(args) or args[indexes[0]+1]!='watch'
                or args.count('--config')!=1 or '--no-process' in args):return False
        flag=args.index('--config')
        if flag+1>=len(args) or (ROOT/args[flag+1]).resolve()!=config:return False
        current=read(path,{})
        if not isinstance(current,dict):return None
        if (current.get('pid'),current.get('created'),current.get('config'))!=(pid,created,record['config']):return None
        return record
    except psutil.AccessDenied:return None
    except psutil.NoSuchProcess:return False


def stage12_service_step(base, cmd, *, children, finished, attempts, next_at, state,
                         launch, log, existing_owner=stage12_existing_owner):
    """Do not exhaust supervisor retries on a verified recovered writer's lock."""
    name='stage12';child=children.get(name)
    if child is not None and child.poll() is None:
        service_step(name,cmd,children=children,finished=finished,attempts=attempts,
            next_at=next_at,state=state,launch=launch,log=log)
        return state.get(name,{})
    identity_error=None
    try:owner=existing_owner(base)
    except (OSError,ValueError,TypeError,KeyError) as error:
        owner=None;identity_error=type(error).__name__
    prior=state.get(name,{})
    previous_exit=prior.get('previous_exit')
    if child is not None:
        previous_exit={'pid':child.pid,'returncode':child.returncode,'utc_epoch':time.time()}
        with (log/'stage12_lifecycle.jsonl').open('a',encoding='utf-8') as stream:
            stream.write(json.dumps({'event':'PROCESS_EXIT_OBSERVED',**previous_exit})+'\n')
    elif '退出码' in prior:
        previous_exit={'returncode':prior['退出码']}
    if owner is not False:
        prior_attempts=attempts.get(name,0)
        if child is not None:
            service_step(name,cmd,children=children,finished=finished,attempts=attempts,
                next_at=next_at,state=state,launch=lambda *args:None,log=log)
            # A live process alone cannot reset a genuine prior failure budget.
            attempts[name]=max(prior_attempts,attempts.get(name,0))
        if owner is None:
            row={**state.get(name,{}),'状态':'正在核对本任务第一二层进程身份，暂不启动重复进程',
                 '阻断':True,'owner_verification_pending':True,
                 **({'owner_verification_error_type':identity_error} if identity_error else {})}
        else:
            row={'状态':'运行中，沿用本任务已恢复的第一二层后台程序','阻断':False,
                 'PID':owner['pid'],'owner_created':owner['created'],'external_service':True}
        if previous_exit is not None:row['previous_exit']=previous_exit
        state[name]=row
        return row
    if prior.get('external_service'):
        state[name]={'状态':'本任务已恢复的第一二层后台程序已退出，等待有界恢复'}
        if previous_exit is not None:state[name]['previous_exit']=previous_exit
    service_step(name,cmd,children=children,finished=finished,attempts=attempts,
        next_at=next_at,state=state,launch=launch,log=log)
    if name not in children and name in finished and attempts.get(name,0)>=3:
        state[name]={**state.get(name,{}),'状态':'第一二层后台程序已退出，自动恢复预算已用尽',
                     '阻断':True,'recovery_exhausted':True}
    return state.get(name,{})


def prepare(category_path, amazon_count=None, per_keyword=None):
    cat=load_category(category_path, compile_authored_policy=True)
    if amazon_count is not None: cat['amazon']['target_count']=amazon_count
    if per_keyword is not None: cat['temu']['max_products_per_keyword']=per_keyword
    if collection_only(cat): validate_collection(cat)
    else:
        from selection_automation.rules import validate
        validate(cat)
    base=ROOT/'runs'/cat['category_key']; frozen=base/'category.json'
    previous=read(frozen)
    if previous and revision(previous)!=revision(cat):
        raise ValueError('本任务已冻结规则或数量；请为不同规则/数量使用新的 category_key，避免混用断点')
    save(frozen,cat)
    if not collection_only(cat):
        from pipeline import write_stage12_config
        write_stage12_config(cat)
    return frozen,cat,base


def doctor():
    import importlib.util, shutil
    company=ROOT/'runtime'/'secrets'/'company.dpapi'
    node=shutil.which('node')
    browser={'ready':False,'status':'NODE_MISSING'}
    if node:
        try:
            probe=subprocess.run([node,'-e',
                "const fs=require('fs'); const p=require('playwright').chromium.executablePath(); "
                "console.log(JSON.stringify({ready:fs.existsSync(p),path:p,status:fs.existsSync(p)?'READY':'BROWSER_MISSING'}));"],
                cwd=ROOT,capture_output=True,text=True,timeout=10, creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
            browser=json.loads(probe.stdout) if probe.returncode==0 else {'ready':False,'status':'PLAYWRIGHT_PROBE_FAILED'}
        except (OSError,ValueError,subprocess.TimeoutExpired):
            browser={'ready':False,'status':'PLAYWRIGHT_PROBE_FAILED'}
    return {'Python':sys.version.split()[0], 'Node':bool(node),
            'PowerShell7':bool(shutil.which('pwsh')), '依赖':{n:importlib.util.find_spec(n) is not None for n in ['torch','torchvision','openai','psutil','xlsxwriter','openpyxl']},
            'Playwright浏览器':browser,
            '公司配置已导入':company.exists(), '浏览器清单存在':(ROOT/'config'/'sessions.json').exists(),
            'API请求数':0}


class _VerificationDriverProtocolError(RuntimeError):
    pass


def _verification_tab_kind(value, query):
    """Classify only a same-query/empty TEMU search document; never log the URL."""
    from urllib.parse import urlsplit, parse_qs
    if not isinstance(value,str): return None
    try:
        url=urlsplit(value)
        if (url.scheme!='https' or url.hostname not in {'temu.com','www.temu.com'}
                or url.username is not None or url.password is not None or url.port not in {None,443}
                or url.path!='/search_result.html'): return None
        terms=parse_qs(url.query,keep_blank_values=True).get('search_key',[''])
        if len(terms)!=1: return None
        if not str(terms[0]).strip(): return 'EMPTY_SEARCH'
        if query and terms[0]==query: return 'CURRENT_QUERY'
    except (ValueError,TypeError): pass
    return None


def _verification_is_temu(value):
    from urllib.parse import urlsplit
    if not isinstance(value,str): return False
    try:
        url=urlsplit(value)
        return url.scheme in {'http','https'} and url.hostname in {'temu.com','www.temu.com'}
    except (ValueError,TypeError): return False


def _select_verification_window(request, query, result):
    """Stopped manual recovery only. Select within this SID; never create/navigate/close."""
    current_url=request('GET','/url')
    if not isinstance(current_url,str): raise ValueError('invalid driver URL reply')
    # A current TEMU gate or wrong query is not permission to choose another tab.
    if _verification_is_temu(current_url):
        result['window_selection']='CURRENT_TEMU_NOT_READY'
        return False
    original=request('GET','/window')
    handles=request('GET','/window/handles')
    if (not isinstance(original,str) or not original or not isinstance(handles,list)
            or not 1<=len(handles)<=8 or any(not isinstance(h,str) or not 1<=len(h)<=512 for h in handles)
            or len(set(handles))!=len(handles) or original not in handles):
        result['window_selection']='WINDOW_SET_REJECTED'
        return False
    selected=original; temu=[]; candidates=[]
    try:
        for handle in handles:
            if handle==original: continue
            request('POST','/window',{'handle':handle}); selected=handle
            value=request('GET','/url')
            if not isinstance(value,str): raise ValueError('invalid driver URL reply')
            if _verification_is_temu(value): temu.append(handle)
            kind=_verification_tab_kind(value,query)
            if kind: candidates.append((handle,kind))
        if len(temu)==1 and len(candidates)==1:
            target,kind=candidates[0]
            if selected!=target: request('POST','/window',{'handle':target})
            result.update(window_selection='SELECTED_SAME_SESSION',window_kind=kind,
                          selected_handle_sha256=hashlib.sha256(target.encode()).hexdigest())
            return True
        if selected!=original: request('POST','/window',{'handle':original})
        result['window_selection']='AMBIGUOUS_TEMU_WINDOWS' if len(temu)>1 else 'NO_MATCHING_TEMU_WINDOW'
        return False
    except Exception:
        # A timed-out switch may still be queued. Never issue a compensating
        # switch after an uncertain request; the next read must fence it first.
        result['window_selection']='WINDOW_CHECK_FAILED'
        raise


def ready_after_verification(session, query=None, *, allow_empty_search=False, diagnostic=None):
    import urllib.error
    import urllib.request
    checked_at=time.time(); started=time.monotonic(); stage='SESSION'
    result={'checked_at':checked_at,'ready':False,'check_ok':False}
    try:
        sid=(ROOT/session['session_id_file']).read_text(encoding='utf-8').strip()
        body={'script':r"""
            const target = arguments[0], allowEmpty = arguments[1], url = new URL(location.href);
            const visible = e => { const r=e.getBoundingClientRect(), s=getComputedStyle(e); return r.width>0 && r.height>0 && s.display!=='none' && s.visibility!=='hidden'; };
            if (!/(^|\.)temu\.com$/.test(url.hostname) || !url.pathname.includes('search_result')) return false;
            const emptySearch = allowEmpty === true && url.pathname === '/search_result.html' && !String(url.searchParams.get('search_key') || '').trim();
            if (!emptySearch && target && url.searchParams.get('search_key') !== target) return false;
            if (/captcha|verification|login|access.denied|proxyconnectfailure|neterror/i.test(url.pathname+' '+document.title)) return false;
            if (/access denied|verify you are human|unusual traffic|too many requests|proxyconnectfailure|安全验证|安全驗證|请完成验证|請完成驗證|人机验证|人機驗證|拒绝访问|拒絕存取/i.test((document.body?.innerText||'').slice(0,5000))) return false;
            if (/no connection|please check your network connection/i.test((document.body?.innerText||'').slice(0,5000))) return false;
            if ([...document.querySelectorAll('button,a,[role="button"],div,span')].some(e => visible(e) && /^(?:try again|retry|重试)$/i.test((e.textContent||'').trim()))) return false;
            if ([...document.querySelectorAll('[role="dialog"],iframe[src*="captcha"],input[type="password"]')].some(e => visible(e) && (e.tagName==='IFRAME' || e.tagName==='INPUT' || /captcha|verify|verification|sign in|log in|access denied/i.test(e.innerText||'')))) return false;
            if (emptySearch) return ['interactive','complete'].includes(document.readyState) && [...document.querySelectorAll('input[type="search"]')].some(e => visible(e) && !e.disabled && !e.readOnly);
            return [...document.querySelectorAll('[data-goods-id],a[href*="-g-"]')].some(visible);
        """,'args':[query,allow_empty_search]}
        deadline=started+15
        opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))
        def request(method,suffix,payload=None):
            nonlocal stage
            remaining=deadline-time.monotonic()
            if remaining<=0: raise TimeoutError('verification window budget exhausted')
            stage='REQUEST'
            req=urllib.request.Request(session['webdriver_url']+'/session/'+sid+suffix,method=method,
                data=None if payload is None else json.dumps(payload).encode(),
                headers={} if payload is None else {'Content-Type':'application/json'})
            stage='DRIVER'
            with opener.open(req,timeout=min(5,remaining)) as r:
                stage='RESPONSE'; reply=json.load(r)
            value=reply.get('value')
            if isinstance(value,dict) and 'error' in value and suffix!='/execute/sync':
                raise _VerificationDriverProtocolError('local driver protocol error')
            return value
        value=request('POST','/execute/sync',body)
        # Preserve every old DOM condition. Window selection is only a fallback
        # for a stopped manual wait whose current document is outside TEMU.
        if value is False and allow_empty_search:
            if _select_verification_window(request,query,result):
                value=request('POST','/execute/sync',body)
        ready=value is True
        if isinstance(value,bool):
            result.update(ready=ready,check_ok=True,code='PAGE_READY' if ready else 'PAGE_NOT_READY')
        else:
            result['code']='DRIVER_PROTOCOL_ERROR' if isinstance(value,dict) and 'error' in value else 'DRIVER_RESPONSE_FORMAT_ERROR'
    except Exception as exc:
        ready=False
        # Never persist exception text, URLs, query terms, or session identifiers.
        if stage=='SESSION': code='SESSION_FILE_ERROR'
        elif isinstance(exc,_VerificationDriverProtocolError): code='DRIVER_PROTOCOL_ERROR'
        elif isinstance(exc,urllib.error.HTTPError): code='DRIVER_HTTP_ERROR'
        elif isinstance(exc,(TimeoutError,socket.timeout)) or (isinstance(exc,urllib.error.URLError) and isinstance(exc.reason,(TimeoutError,socket.timeout))): code='DRIVER_TIMEOUT'
        elif isinstance(exc,(urllib.error.URLError,ConnectionError)): code='DRIVER_CONNECTION_ERROR'
        elif isinstance(exc,json.JSONDecodeError): code='DRIVER_RESPONSE_JSON_ERROR'
        elif stage=='RESPONSE': code='DRIVER_RESPONSE_FORMAT_ERROR'
        elif stage=='REQUEST': code='DRIVER_REQUEST_ERROR'
        else: code='DRIVER_CHECK_ERROR'
        safe_types={'FileNotFoundError','PermissionError','UnicodeDecodeError','KeyError','ValueError','TypeError',
                    'TimeoutError','ConnectionRefusedError','ConnectionResetError','URLError','HTTPError',
                    'JSONDecodeError','AttributeError','OSError','RuntimeError'}
        result.update(code=code,error_type=type(exc).__name__ if type(exc).__name__ in safe_types else 'Exception')
        if isinstance(exc,urllib.error.HTTPError) and isinstance(exc.code,int) and 100<=exc.code<=599:
            result['http_status']=exc.code
    result['elapsed_seconds']=round(max(0,time.monotonic()-started),3)
    if diagnostic is not None: diagnostic.update(result)
    return ready


def verification_probe_view(probe, recovery=None):
    """Show a failed control check separately from a still-blocked page."""
    view={'恢复检查':probe}
    if recovery: view.update(状态=recovery['reason'],建议动作=recovery['action'])
    if probe.get('check_ok') is False:
        labels={'SESSION_FILE_ERROR':'无法读取当前浏览器连接记录','DRIVER_HTTP_ERROR':'浏览器控制请求未成功',
                'DRIVER_TIMEOUT':'浏览器控制检查超时','DRIVER_CONNECTION_ERROR':'浏览器控制连接未成功',
                'DRIVER_RESPONSE_JSON_ERROR':'浏览器控制返回格式异常','DRIVER_RESPONSE_FORMAT_ERROR':'浏览器控制返回格式异常',
                'DRIVER_PROTOCOL_ERROR':'浏览器控制返回错误','DRIVER_REQUEST_ERROR':'浏览器控制请求未能构造',
                'DRIVER_CHECK_ERROR':'浏览器恢复检查失败'}
        label=labels.get(probe.get('code'),'浏览器恢复检查失败')
        view.update(状态=label+'，未确认验证状态',建议动作=label+'；当前浏览器和断点已保留，程序会继续检查。')
    return view


def worker_snapshot(base, name):
    """Read the active query's collector progress, never a different query's snapshot."""
    from pipeline import collector_block, slug
    worker=read(base/'temu'/'supervisors'/f'{name}.json',{})
    if worker.get('status')=='RESUMING':
        # A legacy supervisor may retain RESUMING after the lane resumed itself.
        # Only its live, exact worker lease permits using the fresh collector view.
        owner=read(ROOT/'runtime'/'worker-leases'/f'{name}.owner.json',{})
        try:
            import psutil
            args=psutil.Process(owner['pid']).cmdline()
            if (owner.get('worker')==name and owner.get('purpose')=='collector'
                    and len(args)>1 and Path(args[1]).resolve()==(ROOT/'scripts/pipeline.py').resolve()
                    and '--worker-name' in args and args[args.index('--worker-name')+1]==name):
                worker={**worker,'status':'RUNNING'}
        except (KeyError,ValueError,IndexError,OSError,psutil.Error): pass
    pause=read(ROOT/'runtime'/'worker-leases'/f'{name}.stop.json',{})
    if pause.get('reason')=='DIAGNOSTIC_BASELINE' and pause.get('category_key')==base.name:
        return worker,{'状态':'对照诊断中，暂缓本路，断点保留','采集状态':'PAUSED_DIAGNOSTIC',
            '关键词':worker.get('current_query'),'阻断':False,'需人工介入':False,
            '建议动作':'','浏览器保留':True,'采集进程运行中':False},None
    if worker.get('status') in {'BINDING_LOCAL','BINDING_FAILED','BINDING_RETRY_WAIT'}:
        checkpoint=read(Path(worker['output'])/'progress.json',{}) if worker.get('output') else {}
        return worker,{'状态':'连接中断，正在自动重连','采集状态':'BINDING_LOCAL',
            '阻断':False,'需人工介入':False,'建议动作':'',
            '采集条数':checkpoint.get('raw_collected_count'),
            '当前排序目标':checkpoint.get('max_products'),
            '关键词':worker.get('current_query')},None
    replacing=str(worker.get('status','')).startswith('REPLACING_') and str(worker.get('status','')).endswith('_ENVIRONMENT')
    progress={}; query=worker.get('current_query')
    output=worker.get('output')
    if not output and query: output=str(base/'temu'/'workers'/name/slug(query))
    if output and not replacing and worker.get('status') not in {'SESSION_REPLACED','RESUMING','STARTING','WAITING_HTTP_COOLDOWN','WAITING_SITE_COOLDOWN','WAITING_SORT_RETRY','WAITING_INITIAL_PRODUCTS_RETRY'}:
        progress_path=Path(output)/'progress.json'
        # A restarted collector initially shares the old progress file. Do not
        # mistake that previous run's terminal status for its current result.
        try:
            if worker.get('status')!='RUNNING' or progress_path.stat().st_mtime>=worker.get('collector_started_at',0):
                progress=read(progress_path,{})
        except OSError: pass
    if worker.get('status')=='WORKER_COMPLETE':query=query or progress.get('search_query')
    code=progress.get('status') or worker.get('query_status') or worker.get('status','STARTING')
    if code in {'STOPPED_BROWSER_CONTROL_RECOVERY_TIMEOUT','STOPPED_BROWSER_CONTROL_RECOVERY_BUDGET'}:
        code='WAITING_BROWSER_CONTROL_RECOVERY'
    if worker.get('status')=='WAITING_BROWSER_CONTROL_RECOVERY': code=worker['status']
    if replacing or worker.get('status') in {'SESSION_REPLACED','RESUMING','STARTING','WORKER_COMPLETE','WAITING_HTTP_COOLDOWN','WAITING_SITE_COOLDOWN','WAITING_SORT_RETRY','WAITING_INITIAL_PRODUCTS_RETRY'}: code=worker['status']
    recovery=collector_block(code)
    if not recovery and worker.get('status')=='HUMAN_ACTION_REQUIRED':
        recovery={'reason':'采集阻断，等待人工处理','action':worker.get('recovery_action','请检查当前浏览器；确认当前关键词商品页正常后继续'),'verify_to_resume':worker.get('verify_to_resume',True)}
    waiting={
        'WAITING_FLEET_SLOT':'并发调度排队，自动继续',
        'WAITING_INITIAL_PRODUCTS_RETRY':'加载超时，等待后在原环境重新搜索',
        'WAITING_INITIAL_PRODUCTS':'等待首屏商品，尚未确认有效结果',
        'WAITING_TARGET_SEARCH_RESULTS':'等待当前关键词商品页就绪',
        'WAITING_EMPTY_SEARCH_STABLE':'等待搜索页稳定，尚未确认有效结果',
        'WAITING_POST_VERIFICATION_SEARCH_BOX':'验证后等待搜索页就绪',
        'WAITING_HTTP_COOLDOWN':'请求限流，等待后自动重试',
        'WAITING_SITE_COOLDOWN':'站点拒绝当前会话，冷却后自动换环境',
        'WAITING_SORT_RETRY':'排序未就绪，正在原窗口自动重试',
        'WAITING_TRY_AGAIN':'等待页面重试恢复',
        'WAITING_BROWSER_CONTROL_RECOVERY':'自动检查浏览器连接，恢复后从断点继续',
        'WAITING_SEE_MORE_CLICK':'See more 尚未展开，正在自动重新定位并重试',
        'WAITING_SEE_MORE':'采集中，正在定位加载更多',
        'RETRYING_SALES_SORT':'正在重试排序',
        'RETRYING_NETWORK':'连接异常，正在自动重试',
    }
    label=recovery['reason'] if recovery else waiting.get(code)
    if code=='WAITING_INITIAL_PRODUCTS':
        seconds=max(0,int((progress.get('initial_products_wait_ms') or 0)/1000))
        label=('正在确认空结果，保留当前关键词' if progress.get('loading_reason')=='CONFIRMING_EMPTY_RESULTS'
            else '等待首屏商品加载，保留当前关键词')+f' · 已等待 {seconds} 秒'
    if code=='WAITING_FLEET_SLOT':
        info=progress.get('fleet_wait') or {}
        if info.get('reason')=='HTTP_COOLDOWN':
            seconds=max(0,int((float(info.get('cooldown_until') or 0)/1000)-time.time()))
            label=f'服务器限流等待，约 {seconds} 秒后重试'
        else:
            label={'navigation':'排队打开页面','search':'排队提交关键词','sort':'排队设置排序','load_more':'等待加载更多'}.get(info.get('operation'),'等待启动调度')

    if replacing: label='正在自动更换环境，保留当前关键词断点'
    if not label: label={'WORKER_COMPLETE':'本路采集完成','SESSION_REPLACED':'新session已就绪，等待恢复断点','RESUMING':'页面已验证，恢复原关键词','STARTING':'启动中'}.get(code,'运行中')
    if code=='WORKER_COMPLETE' and worker.get('environment_cleanup')=='COMPLETE':label='本路采集完成，环境已回收'
    view={'状态':label,'采集状态':code,'关键词':query,'排序方式':{'sales':'销量最高','recent':'最近上架'}.get(worker.get('current_sort'),'销量最高'),'输出目录':output,
          '已完成关键词':worker.get('completed_queries',[]),'阻断':bool(recovery),'需人工介入':bool(recovery),
          '建议动作':'','浏览器保留':False}
    for key,label in [('raw_collected_count','采集条数'),('unique_goods','唯一商品数'),('unique_unclassified_goods','未归类商品数'),
                      ('filtered_severe_off_topic_goods','明确排除商品数'),
                      ('unique_progress_goods','相关及未归类商品数'),('round','采集轮次'),('updated_at','采集更新时间')]:
        value=progress.get(key,worker.get(key))
        if value is not None: view[label]=value
    if progress.get('max_products'):
        view['当前排序目标']=progress['max_products']
    if recovery: view['建议动作']=recovery['action']; view['浏览器保留']=True
    captcha=progress.get('captcha')
    if recovery and code in {'WAITING_CAPTCHA','WAITING_SEARCH_VERIFICATION','WAITING_EMPTY_SEARCH_VERIFICATION','WAITING_HUMAN_VERIFICATION','CAPTCHA_REQUIRED'} and isinstance(captcha,dict):
        from selection_automation.captcha import MESSAGES
        state=captcha.get('status')
        if state in MESSAGES:
            view.update(状态=MESSAGES[state],验证码状态=state,浏览器保留=True,
                        需人工介入=state not in {'PENDING','ANSWER_READY','VERIFIED'},
                        建议动作=MESSAGES[state])
    if recovery and worker.get('status')=='HUMAN_ACTION_REQUIRED' and isinstance(worker.get('verification_probe'),dict):
        view.update(verification_probe_view(worker['verification_probe'],recovery))
    return worker,view,recovery


def control_command_finished(session, query):
    """A responsive serialized read fences the earlier timed-out command; never resend it."""
    from selection_automation.browser_control_recovery import control_fenced
    return control_fenced(ROOT,session)


def hold_blocked_worker(base, session, *, children, finished, attempts, next_at, state, verify=ready_after_verification, bind_launch=None):
    """Keep live verification waits intact; never count a blocked zero exit as done."""
    name=session['name']
    worker_path=base/'temu'/'supervisors'/f'{name}.json'
    worker=read(worker_path,{})
    child=children.get(name)
    running=child is not None and child.poll() is None
    if worker.get('status')=='RESUMING' and not running:
        children.pop(name,None);finished.discard(name);attempts[name]=0;next_at[name]=0
        state[name]={'状态':'连接已恢复，正在续采','阻断':False,'需人工介入':False}
        return False
    if worker.get('status')=='BINDING_LOCAL' and not running:
        if child is not None and child.returncode==0:
            children.pop(name,None); attempts[name]=0; next_at[name]=0; finished.discard(name)
            save(worker_path,{**worker,'status':'RESUMING','manual_action_required':False})
            return False
        save(worker_path,{**worker,'status':'BINDING_FAILED','manual_action_required':True})
        worker=read(worker_path,{})
    if bind_launch and not running and (str(worker.get('status','')).endswith('_RECOVERY_FAILED') or worker.get('status')=='BINDING_FAILED'):
        number=int(name.split('-')[1])
        journal_path=ROOT/'runtime'/'proxyshare-chains'/f'replacement-worker-{number}.json'
        journal=read(journal_path,{})
        manifest=read(ROOT/'config'/'sessions.json',{})
        current=next((x for x in manifest.get('workers',[]) if x.get('name')==name),{})
        if (manifest.get('machine_id')==identity() and journal.get('phase') in {'PROFILE_CREATED','BIND_FAILED'}
                and journal.get('new_profile_id')==current.get('profile_id') and int(journal.get('local_bind_attempts',0))<2):
            save(journal_path,{**journal,'local_bind_attempts':int(journal.get('local_bind_attempts',0))+1})
            save(worker_path,{**worker,'status':'BINDING_LOCAL','manual_action_required':False})
            children[name]=bind_launch(name,[sys.executable,str(ROOT/'scripts'/'resume_worker_binding.py'),'--worker',str(number)])
            finished.discard(name)
    worker,view,recovery=worker_snapshot(base,name)
    if worker.get('status') in {'BINDING_LOCAL','BINDING_FAILED','BINDING_RETRY_WAIT'}:
        state[name]=view
        return True
    # Old runs may have stopped before automatic site recovery was deployed.
    # Relaunch their owning pipeline without requiring CAPTCHA completion.
    from selection_automation.recovery_policy import session_recovery_kind
    progress=read(Path(worker['output'])/'progress.json',{}) if worker.get('output') else {}
    if view['采集状态']=='STOPPED_INITIAL_PRODUCTS_TIMEOUT' and not running and not progress.get('http_block') and not progress.get('confirmed_login_redirect'):
        children.pop(name,None); finished.discard(name); attempts[name]=0; next_at[name]=0
        save(worker_path,{**worker,'status':'RESUMING','manual_action_required':False,'automatic_resume_pending':True})
        state[name]={**view,'状态':'首屏等待超时，原环境重新搜索','需人工介入':False}
        return False
    site_kind=session_recovery_kind(view['采集状态'],progress)
    if site_kind and view['采集状态'] not in {'STARTING','RESUMING','RUNNING'} and not running and not worker.get('recovery_budget_exhausted'):
        children.pop(name,None)
        finished.discard(name); attempts[name]=0; next_at[name]=0
        save(worker_path,{**worker,'status':'RESUMING','manual_action_required':False,
                          'automatic_resume_pending':True,'session_recovery_kind':site_kind})
        state[name]={**view,'状态':'自动恢复采集会话，断点保留','需人工介入':False}
        recovery_event(base,name,'LEGACY_SITE_STOP_AUTOMATIC_RESUME',kind=site_kind,
                       previous_status=view['采集状态'])
        return False
    if not recovery: return False
    finished.discard(name)
    child=children.get(name); running=child is not None and child.poll() is None
    view['采集进程运行中']=running
    if running:
        view['PID']=child.pid
    elif child is not None:
        del children[name]
    state[name]=view
    # Live collectors own manual verification and resume themselves. The
    # supervisor only restarts stopped collectors after a positive page check.
    # Old product cards survive rejected search requests. A DOM-only check
    # cannot prove that a 403/429 recovered; keep the stop for the existing
    # session-replacement path instead of repeatedly restarting that request.
    progress=read(Path(worker['output'])/'progress.json',{}) if worker.get('output') else {}
    verification_wait=view['采集状态'] in {'WAITING_CAPTCHA','WAITING_SEARCH_VERIFICATION',
        'WAITING_EMPTY_SEARCH_VERIFICATION','WAITING_HUMAN_VERIFICATION','CAPTCHA_REQUIRED'}
    rejected_search=view['采集状态'] in {'STOPPED_HTTP_429','STOPPED_HTTP_403','STOPPED_HTTP_BLOCK'}
    rejected_search=rejected_search or (not verification_wait and
        (progress.get('http_block') or {}).get('status') in {403,429})
    if rejected_search:
        return True
    if running:
        return True
    if verification_wait and worker.get('output') and not (base/'STOP').exists():
        # A crashed/stopped collector cannot poll its paid task. Restart only
        # the owning pipeline in this same session, once new settings arrive
        # or a saved answer/task still needs polling.
        captcha_config=ROOT/'runtime/secrets/twocaptcha.dpapi'
        progress_path=Path(worker['output'])/'progress.json'
        pending=not progress.get('captcha') or (progress.get('captcha') or {}).get('status') in {'PENDING','ANSWER_READY'}
        if captcha_config.is_file() and (pending or captcha_config.stat().st_mtime_ns>progress_path.stat().st_mtime_ns):
            from selection_automation.captcha import settings
            config=settings(ROOT)
            if config['enabled'] and config['key']:
                children.pop(name,None);finished.discard(name);attempts[name]=0;next_at[name]=0
                save(worker_path,{**worker,'status':'RESUMING','manual_action_required':False,'automatic_resume_pending':True})
                state[name]={**view,'状态':'恢复原验证码任务，浏览器与采集断点保留','需人工介入':False}
                return False
    command_timeout=view['采集状态']=='STOPPED_BROWSER_COMMAND_TIMEOUT'
    if command_timeout and not control_command_finished(session,worker.get('current_query')):
        return True
    if command_timeout:
        from selection_automation.browser_control_recovery import recovery_page_ready
        ready=recovery_page_ready(ROOT,session,worker.get('current_query')) if verify is ready_after_verification else verify(session,worker.get('current_query'))
    else:
        probe={}
        ready=recovery['verify_to_resume'] and (verify(session,worker.get('current_query'),allow_empty_search=verification_wait,diagnostic=probe)
            if verify is ready_after_verification else verify(session,worker.get('current_query')))
        if probe:
            worker={**worker,'verification_probe':probe}
            save_progress(worker_path,worker)
            state[name].update(verification_probe_view(probe,recovery))
    if not ready:
        return True
    attempts[name]=0; next_at[name]=0
    save(base/'temu'/'supervisors'/f'{name}.json',{**worker,'status':'RESUMING','manual_action_required':False})
    return False


def supervision_status(state):
    state={name:info for name,info in state.items() if isinstance(info,dict)}
    blocked=[name for name,info in state.items() if (info.get('阻断') and info.get('采集状态') not in {
        'WAITING_INITIAL_PRODUCTS','WAITING_HTTP_COOLDOWN','WAITING_SITE_COOLDOWN','WAITING_TRY_AGAIN',
        'WAITING_SEE_MORE_CLICK','RETRYING_NETWORK','WAITING_FLEET_SLOT'}) or info.get('状态') in ('失败，需要处理','换session失败，保留替换进度')]
    human=[name for name,info in state.items() if info.get('需人工介入')]
    return {'状态':'等待人工介入' if human else '存在阻断' if blocked else '运行中',
            '阻断阶段':blocked,'需人工介入':bool(human),'人工介入阶段':human,
            '建议动作':{name:state[name]['建议动作'] for name in blocked if state[name].get('建议动作')}}


def completion(config_path, paid):
    """Confirm current input hashes and every current candidate before declaring done."""
    import sqlite3
    from selection_automation.vision import compatible_vision_keys
    config=read(config_path); output=Path(config['output_dir']); db=output/'stage12.sqlite3'
    if not db.exists(): return None
    cat=read(config_path.parent/'category.json')
    try:
        worker_root=config_path.parent/'temu'/'workers'
        if worker_root.exists():
            from watch_temu_union import snapshot_signature
            snapshots=sorted(worker_root.glob('*/*/products.jsonl'))
            provider=config_path.parent/'market/geekbi.json'
            if provider.is_file(): snapshots.append(provider)
            union_state=read(config_path.parent/'temu'/'union'/'watch_state.json',{})
            if not snapshots or union_state.get('status')!='PUBLISHED' or union_state.get('signature')!=snapshot_signature(snapshots):
                return None
        with sqlite3.connect(f'{db.as_uri()}?mode=ro',uri=True,timeout=1) as con:
            con.row_factory=sqlite3.Row
            con.execute('BEGIN')
            sources=list(con.execute('SELECT * FROM sources'))
            if len(sources)!=2: return None
            for source in sources:
                path=Path(source['input_path'])
                if source['last_error'] or not path.exists(): return None
                if hashlib.sha256(path.read_bytes()).hexdigest()!=source['last_snapshot_hash']: return None
            states=dict(con.execute('SELECT status,count(*) FROM pairs GROUP BY status'))
            if any(states.get(k,0) for k in ['PENDING','FAILED_RETRYABLE','WAIT_IMAGE_RETRY']): return None
            if con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='image_retry_queue'").fetchone():
                if con.execute("SELECT 1 FROM image_retry_queue WHERE status IN ('RETRY','RUNNING') LIMIT 1").fetchone():return None
            counts=[r['received_records'] for r in sources]
            if not all(counts) or sum(states.values())!=counts[0]*counts[1]: return None
            unresolved=sum(states.get(k,0) for k in ['WAIT_IMAGE_SOURCE','WAIT_IMAGE_PROXY','FAILED_BOUNDED','FAILED_IMAGE_ACTION'])
            amazon_snapshot=read(config_path.parent/'amazon'/'amazon_frontend_latest.json',{})
            missing_details=amazon_snapshot.get('missing_details',[])
            unresolved+=len(missing_details)
            if paid:
                from run_category_layer3 import result_index
                prior=result_index(config_path.parent/'stage123'/'results.jsonl')
                for row in con.execute('SELECT payload_json FROM candidates'):
                    candidate=json.loads(row[0]); keys=compatible_vision_keys(cat,candidate)
                    matches=[prior[k] for k in keys if k in prior]
                    result=next((r for r in matches if r.get('api_status')=='success'), max(matches,key=lambda r:r.get('attempt',0),default={}))
                    if result.get('api_status')!='success':
                        if result.get('attempt',0)<2: return None
                        unresolved+=1
            return {'商品对':sum(states.values()),'缺失或失败':unresolved,'Amazon详情缺失':len(missing_details),
                    'Amazon缺失详情清单':missing_details,'第三层启用':paid}
    except (sqlite3.Error,OSError,ValueError): return None


def close_terminal_browsers(base, category_key):
    """Close only this finished run's local AdsPower browsers before proxy shutdown."""
    manifest=read(ROOT/'config'/'sessions.json',{})
    if manifest.get('machine_id')!=identity() or manifest.get('task_key')!=category_key:
        return
    from company_setup import ensure_company
    from replace_proxyshare_worker_environment import api_call, api_base
    from urllib.parse import urlencode
    ensure_company(start=False)
    endpoint=api_base()
    failures=[]
    for worker in manifest.get('workers',[]):
        profile_id=worker.get('profile_id')
        if not profile_id: continue
        cleaned=read(base/'temu/supervisors'/f"{worker['name']}.json",{})
        if cleaned.get('deleted_profile_id')==str(profile_id): continue
        try:
            api_call(endpoint,'/api/v1/browser/stop?'+urlencode({'user_id':profile_id}),timeout=12)
        except RuntimeError:
            failures.append(worker.get('name','unknown'))
    if failures:
        save(base/'status.json',{**read(base/'status.json',{}),'浏览器收尾待处理':failures})


def reset_prepared_worker_states(base, names):
    """Newly bound browsers must not inherit an older browser's CAPTCHA stop."""
    for name in names:
        path=base/'temu/supervisors'/f'{name}.json'
        prior=read(path,{})
        if prior.get('status')=='WORKER_COMPLETE': continue
        save(path,{**prior,'worker':name,'status':'STARTING','query_status':None,
                   'manual_action_required':False,'browser_preserved':False,
                   'recovery_action':'','prepared_at':time.time()})


def provision_company_process(base, cat, paid, *, children, state,preserve_prepared=False):
    """Supervise cancellable local provisioning without blocking STOP handling."""
    if (base/'STOP').exists():
        state['bootstrap']={'状态':'启动前已停止'}
        return False
    log=base/'logs'/'bootstrap.log'
    manifest=read(ROOT/'config'/'sessions.json',{})
    from selection_automation.retired_collection import retired_task_receipt
    rebuilding=bool(preserve_prepared and retired_task_receipt(ROOT,cat['category_key'],identity(),category_worker_count(cat)))
    resuming=preserve_prepared and not rebuilding
    if resuming and (manifest.get('task_key')!=cat['category_key'] or manifest.get('machine_id')!=identity()):
        raise RuntimeError('维护续接必须属于本任务与本安装')
    task_option='--preserve-task' if preserve_prepared else '--new-task'
    child=spawn([sys.executable,str(ROOT/'scripts'/'company_setup.py'),'--start',task_option,cat['category_key'],'--worker-count',str(category_worker_count(cat))],log)
    children['bootstrap']=child
    started=time.monotonic()
    while True:
        if (base/'STOP').exists():
            state['bootstrap']={**state.get('bootstrap',{}),'状态':'停止中'}
            return False
        code=child.poll()
        journal=read(ROOT/'runtime'/'bootstrap.json',{})
        workers=journal.get('workers') or []
        registered=read(ROOT/'config'/'sessions.json',{}).get('workers') or []
        created=sum(bool(worker.get('profile_id')) for worker in workers)
        bound=sum(worker.get('bootstrap_status')=='BOUND' for worker in workers)
        if code==0 and registered:
            created=bound=len(registered)
        phase=('原任务新环境准备完成，沿用采集断点' if rebuilding and registered and code==0 else
               '为原任务创建新环境，沿用采集断点' if rebuilding else
               '原环境续接完成' if resuming and registered and code==0 else
               '续接未完成采集路的原环境' if resuming else
               '新环境准备完成' if registered and code==0 else
               '归档并清理旧环境' if registered else
               '启动并绑定浏览器' if created else
               '准备本地代理链与浏览器环境' if journal else
               '检查公司环境与 AdsPower 服务')
        state['bootstrap']={'状态':'运行中' if code is None else '启动准备完成' if code==0 else '启动准备失败',
                            '启动阶段':phase,'初始化已创建':created,'初始化已绑定':bound,
                            '登记浏览器数':len(registered),'经过秒':round(time.monotonic()-started,1),
                            'PID':child.pid,'日志':str(log)}
        save_progress(base/'status.json',{'状态':'启动中','时间':time.time(),'品类':cat['category_name_zh'],
                               '付费第三层':paid,'阶段':state,'结果目录':str(base/'excel')})
        if code is not None:
            if code:
                state['bootstrap']['退出码']=code
                raise RuntimeError(f'公司环境准备失败（退出码 {code}）；查看 {log}')
            children.pop('bootstrap',None)
            reset_prepared_worker_states(base,[w['name'] for w in read(ROOT/'config/sessions.json',{}).get('workers',[])])
            return True
        time.sleep(1)


def rotation_step(base,category_path,sessions,owner,*,jobs,children,state,finished,attempts,next_at,manual_leases,launch,py):
    """Keep requests durable and serialize replacement through the shared route."""
    for request_id,job in list(jobs.items()):
        child=children[job['child']]
        if child.poll() is None: continue
        children.pop(job['child']);state.pop(job['child'],None)
        batch=read(job['result'],{}) if child.returncode==0 else {}
        success=({r['worker'] for r in batch.get('results',[]) if r.get('status')=='REPLACED'}
                 if batch.get('workers')==sorted(job['request']['workers']) else set())
        success.intersection_update(job['request']['workers'])
        rotation_queue.finish(base,job['request'],success)
        for number in job['request']['workers']:
            name=sessions[number-1]['name']
            if number not in success:
                state[name]={'状态':'换session失败，保留替换进度','日志':str(base/'logs'/f"{job['child']}.log")}
                finished.add(name);attempts[name]=3
                journal=read(ROOT/'runtime/proxyshare-chains'/f'replacement-worker-{number}.json',{})
                if journal.get('phase') in {'PROFILE_CREATED','BIND_FAILED'}:
                    worker_path=base/'temu/supervisors'/f'{name}.json'
                    prior=read(worker_path,{})
                    save(worker_path,{**prior,'status':'BINDING_FAILED','manual_action_required':True})
            else:
                finished.discard(name);attempts[name]=0;next_at[name]=0
                # Only explicit manual replacement resets the durable automatic budget.
                for ledger in (base/'temu/workers'/name).glob('*/automatic_recovery.json'):
                    ledger.unlink(missing_ok=True)
                recovery_event(base,name,'MANUAL_REPLACEMENT_COMPLETE',request_id=job['request']['id'])
                prior=read(base/'temu/supervisors'/f'{name}.json',{})
                from selection_automation import work_dispatch
                lane_passes=[(r,order,state) for r in work_dispatch.read(base).get('jobs',[])
                    for order,state in (r.get('sort_passes') or {}).items() if state.get('owner')==name]
                lane_done=bool(lane_passes) and all(state.get('complete') for _,_,state in lane_passes)
                save(base/'temu/supervisors'/f'{name}.json',{**prior,'status':'WORKER_COMPLETE' if lane_done else 'SESSION_REPLACED','manual_action_required':False})
        sessions=read(ROOT/'config/sessions.json')['workers']
        del jobs[request_id]
    while True:
        request=rotation_queue.claim(base,owner,in_flight=jobs)
        if request is None: break
        numbers=request['workers']
        if any(n not in range(1,len(sessions)+1) for n in numbers):
            rotation_queue.finish(base,request,set());continue
        child_name=f"rotation-{request['id']}"
        result_path=base/'commands/rotation-results'/f"{request['id']}.json"
        try:
            for number in numbers:
                name=sessions[number-1]['name']
                if name in children: terminate_owned(children.pop(name))
                if name in manual_leases: manual_leases.pop(name).__exit__(None,None,None)
                from selection_automation.proxy_activity import revoke_worker_activity
                revoke_worker_activity(ROOT,number)
                state[name]={'状态':'正在换session'}
            launch(child_name,py('replace_proxyshare_workers_batch.py','--workers',','.join(map(str,numbers)),
                                '--category',category_path,'--result',result_path))
            jobs[request['id']]={'request':request,'child':child_name,'result':result_path}
        except OSError as exc:
            rotation_queue.finish(base,request,set())
            for number in numbers:
                name=sessions[number-1]['name'];finished.add(name);attempts[name]=3
                state[name]={'状态':'换session失败，保留替换进度','建议动作':str(exc)}
    return sessions,{w for job in jobs.values() for w in job['request']['workers']}


def supervise(category_path, paid,*,preserve_prepared=False):
    import psutil
    cat=load_category(category_path); base=ROOT/'runs'/cat['category_key']; log=base/'logs'
    active=ROOT/'runtime'/'active.json'
    # OS lock is released on process exit, including crash; PID reuse cannot release it.
    from selection_automation.process_lock import ProcessLock
    with ProcessLock(ROOT/'runtime'/'supervisor.lock'):
        record_start(ROOT,base,'supervisor',cat,version=LOADED_VERSION)
        save(active,{'pid':os.getpid(),'created':psutil.Process().create_time(),'category_key':cat['category_key']})
        children={}; finished=set(); attempts={}; next_at={}; state={}; changes={}
        # Respect this run's supplied key. Only a subsequent saved-key update
        # may replace it; an older saved key must not override the web form.
        api_stamp=saved_api_key_stamp(); manual_leases={}
        python=sys.executable
        def py(script,*args): return [python,str(ROOT/'scripts'/script),*map(str,args)]
        def launch(name,cmd):
            children[name]=spawn(cmd,log/f'{name}.log'); state[name]={'状态':'运行中','PID':children[name].pid,'started_at':time.monotonic()}
            state[name]['launched_wall_at']=time.time()
            return children[name]
        def service(name,cmd,repeat=False,interval=10):
            service_step(name,cmd,children=children,finished=finished,attempts=attempts,next_at=next_at,state=state,launch=launch,log=log,repeat=repeat,interval=interval)
        try:
            if (base/'STOP').exists():
                save(base/'status.json',{'状态':'已停止','结果目录':str(base/'excel')})
                return
            from company_setup import ensure_company
            ensure_company(start=False)
            identity()
            from selection_automation.completed_collection import completed_checkpoint, unchanged
            analysis_resume=completed_checkpoint(ROOT,base,cat)
            if analysis_resume:
                # Successful collection cleanup may have removed its manifest.
                # Keep retired browsers retired; only real downstream work resumes.
                sessions=[];finished.update(('amazon','cleanup'))
                state['bootstrap']={'状态':'采集已完成，仅续接汇总、分析和导出',
                                    '续接依据':analysis_resume['mode'],
                                    'Amazon商品数':analysis_resume['amazon_count'],
                                    '已完成关键词数':analysis_resume['keyword_count']}
                state['amazon']={'状态':'完整前端快照已核验，沿用采集断点'}
                state['cleanup']={'状态':'原采集环境已清理，不重新创建或绑定浏览器'}
            else:
                if not provision_company_process(base,cat,paid,children=children,state=state,preserve_prepared=preserve_prepared):
                    save(base/'status.json',{'状态':'已停止','阶段':state,'结果目录':str(base/'excel')})
                    return
                manifest=read(ROOT/'config'/'sessions.json', {})
                if manifest.get('machine_id')!=identity():
                    raise RuntimeError('浏览器清单不属于本机；不能操作其他电脑的环境')
                manifest['used']=True
                save(ROOT/'config'/'sessions.json',manifest)
                sessions=manifest['workers']
            # Start only the primary query while Amazon loads. Its completed
            # titles then add evidenced searches without moving existing work.
            plan_path=base/'temu'/'keyword_plan.json'
            from pipeline import write_stage12_config, build_category_keyword_plan, keyword_coverage
            if not plan_path.exists(): save(plan_path,{'keywords':build_category_keyword_plan([],cat)})
            only_collection=collection_only(cat)
            if not only_collection:
                from attach_local_analysis import handoff
                handoff(base)
            cfg=None if only_collection else write_stage12_config(cat)
            output=base/'stage12' if only_collection else Path(read(cfg)['output_dir'])
            if not only_collection: (output/'STOP').unlink(missing_ok=True)
            amazon=base/'amazon'/'amazon_frontend_latest.json'
            last_amazon=None; last_plan=None; last_export=0; last_completion=0; rotation_jobs={}
            while not (base/'STOP').exists():
                if analysis_resume and not unchanged(analysis_resume):
                    raise RuntimeError('已完成采集的续接依据发生变化；已保留断点，请重新检查后继续')
                if not analysis_resume:
                    service('binding_recovery',py('resume_worker_binding.py','--monitor','--category',category_path))
                    service('amazon',py('collect_amazon_until_complete.py','--category',category_path))
                service('market',py('collect_market_evidence.py','--category',category_path))
                if not analysis_resume and amazon.exists():
                    stamp=(amazon.stat().st_mtime_ns,amazon.stat().st_size)
                    if stamp!=last_amazon:
                        payload=read(amazon)
                        if payload and payload.get('records'):
                            target=amazon.with_suffix('.jsonl')
                            atomic_write(target,''.join(json.dumps({**r,'source':payload['source'],'source_url':payload['source_url']},ensure_ascii=False)+'\n' for r in payload['records']))
                            keywords=build_category_keyword_plan(payload['records'],cat)
                            # Preserve existing positions, because the original
                            # round-robin queue maps positions to browser workers.
                            prior=read(plan_path,{'keywords':[]})['keywords']; positions={r['query']:i for i,r in enumerate(prior)}
                            for row in keywords:
                                index=positions.get(row['query'])
                                if index is None:
                                    positions[row['query']]=len(prior); prior.append(row)
                                else:
                                    prior[index]=row
                            save(plan_path,{'keywords':prior,'coverage':keyword_coverage(payload['records'],prior,cat)}); last_amazon=stamp
                plan=read(plan_path,{'keywords':[]})
                # Amazon can append title-derived queries until its process finishes.
                if 'amazon' in finished and attempts.get('amazon',0)==0 and not plan.get('finalized'):
                    plan['finalized']=True; save(plan_path,plan)
                from selection_automation import work_dispatch
                dispatch=work_dispatch.read(base) if analysis_resume else work_dispatch.sync(base,plan['keywords'],[s['name'] for s in sessions])
                plan_key=revision(plan)
                if plan_key!=last_plan:
                    for session in sessions: finished.discard(session['name'])
                    last_plan=plan_key
                rotating=set()
                if not analysis_resume:
                    sessions,rotating=rotation_step(base,category_path,sessions,read(active),jobs=rotation_jobs,
                        children=children,state=state,finished=finished,attempts=attempts,next_at=next_at,
                        manual_leases=manual_leases,launch=launch,py=py)
                for session in sessions:
                    if sessions.index(session)+1 in rotating: continue
                    if not work_dispatch.available(dispatch,session['name']) and session['name'] not in children:
                        _,idle_view,_=worker_snapshot(base,session['name'])
                        state[session['name']]=idle_view if idle_view.get('采集状态')=='WORKER_COMPLETE' else {'状态':'等待新增关键词'}
                        continue
                    name=session['name']
                    if work_dispatch.available(dispatch,name) and attempts.get(name,0)<3: finished.discard(name)
                    sync_manual_proxy(base,session,sessions.index(session)+1,children,manual_leases)
                    if hold_blocked_worker(base,session,children=children,finished=finished,attempts=attempts,next_at=next_at,state=state,bind_launch=launch): continue
                    service(name,py('pipeline.py','collect-temu','--category',category_path,'--sessions',ROOT/'config'/'sessions.json','--worker-name',name))
                    _,view,recovery=worker_snapshot(base,name)
                    child=children.get(name)
                    running=child is not None and child.poll() is None
                    if running or recovery:
                        state[name]={**state.get(name,{}),**view,'采集进程运行中':running}
                        if running: state[name]['PID']=child.pid
                        if recovery: finished.discard(name)
                    else:
                        state[name]={**view,**state.get(name,{}),'采集进程运行中':False}
                    # A process started before new keywords were appended may finish its old queue.
                    if name in finished and attempts.get(name,0)<3:
                        current=read(base/'temu'/'supervisors'/f'{name}.json',{})
                        index=sessions.index(session)
                        assigned={(q['query'],order) for q in dispatch['jobs']
                            for order,pass_state in (q.get('sort_passes') or {}).items()
                            if pass_state.get('owner')==name}
                        completed={(q['query'],order) for q in dispatch['jobs']
                            for order,pass_state in (q.get('sort_passes') or {}).items()
                            if pass_state.get('owner')==name and pass_state.get('complete')}
                        if not assigned.issubset(completed): finished.discard(name)
                from watch_temu_union import watch_health
                union_health=watch_health(base)
                union_health=union_service_step(base,py('watch_temu_union.py','--category',category_path),union_health,
                    children=children,finished=finished,attempts=attempts,next_at=next_at,state=state,
                    launch=launch,log=log,terminate=terminate_owned)
                union_age=time.monotonic()-state.get('temu_union',{}).get('started_at',time.monotonic())
                if not union_health and union_age>90:
                    union_health={'状态':'汇总监督器无心跳，等待自动重启','阻断':True,'stale':True}
                state['temu_union']={**state.get('temu_union',{}),**union_health}
                if not only_collection:
                    stage12_service_step(base,py('run_incremental_stage12.py','watch','--config',cfg),
                        children=children,finished=finished,attempts=attempts,next_at=next_at,
                        state=state,launch=launch,log=log)
                candidates=output/f'layer3_candidates_{cat["category_key"]}.jsonl'
                stage3_marker=read(base/'stage123'/'paid_enabled.json',{})
                stage3_authorized=not only_collection and (base/'stage123'/'paid_enabled.json').exists() and not (base/'stage123/STOP').exists()
                if stage3_marker.get('controller')=='independent':
                    state['stage3']={'状态':'第三层由独立进程处理，可在网页启动或停止'}
                    stage3_authorized=False
                if stage3_authorized and candidates.exists():
                    if not os.getenv('DEEPSEEK_API_KEY','').strip():
                        state['stage3']={'状态':'已授权，等待 API Key','需人工介入':True}
                    else:
                        api_stamp=refresh_saved_api_key(api_stamp)
                        if not hold_provider_block(base,children=children,finished=finished,attempts=attempts,next_at=next_at,state=state):
                            service('stage3',py('run_category_layer3.py','--category',category_path,'--candidates',candidates,'--results',base/'stage123'/'results.jsonl'),repeat=True,interval=30)
                elif not only_collection and stage3_marker.get('controller')!='independent':
                    state['stage3']={'状态':'等待候选' if stage3_authorized else '等待点击网页按钮启动第三层分析'}
                if time.monotonic()-last_export>=60 and 'excel' not in children:
                    launch('excel',py('publish_all.py','--category',category_path)); last_export=time.monotonic()
                if 'excel' in children and children['excel'].poll() is not None:
                    code=children.pop('excel').returncode
                    publication=read(base/'publish_state.json',{})
                    state['excel']={'状态':('查看快照已更新，固定文件待解锁覆盖' if publication.get('pending_views') else '已更新') if code==0 else '更新失败，保留上一版','退出码':code,
                                    '阻断':code!=0,'建议动作':json.dumps(publication.get('errors',{}),ensure_ascii=False) if code else ''}
                save_progress(base/'status.json',{**supervision_status(state),'时间':time.time(),'品类':cat['category_name_zh'],'付费第三层':stage3_authorized,'阶段':state,'结果目录':str(base/'excel')})
                current_dispatch=work_dispatch.read(base)
                collectors_done=('amazon' in finished and attempts.get('amazon',0)==0 and bool(current_dispatch.get('jobs')) and
                    all(r['complete'] for r in current_dispatch['jobs']) and not any(s['name'] in children for s in sessions))
                pending_commands=any(r['status'] in rotation_queue.PENDING for r in rotation_queue.read_requests(base))
                # Each idle lane can retire once the keyword plan is final.
                # A different lane's CAPTCHA must not retain completed browsers.
                if not analysis_resume and plan.get('finalized') and not pending_commands:
                    service('cleanup',py('cleanup_completed_worker.py','--run',base))
                    cleanup=read(base/'cleanup_status.json',{})
                    state['cleanup']={**state.get('cleanup',{}),'状态':'环境清理完成' if cleanup.get('status')=='COMPLETE' else ('环境清理重试中' if cleanup.get('status')=='RETRY_REQUIRED' else '环境清理中'),
                        '待清理路数':len(cleanup.get('pending_workers',[])),'阻断':cleanup.get('status')=='RETRY_REQUIRED'}
                if collectors_done and not pending_commands and 'cleanup' in finished and read(base/'cleanup_status.json',{}).get('status')=='COMPLETE' and time.monotonic()-last_completion>20:
                    last_completion=time.monotonic(); done=source_completion(base,cat) if only_collection else completion(cfg,stage3_authorized)
                    if done and 'stage3' not in children and 'market' in finished:
                        if 'excel' in children: terminate_owned(children.pop('excel'))
                        result=subprocess.run(py('publish_all.py','--category',category_path),cwd=ROOT,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL, creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
                        publication=read(base/'publish_state.json',{})
                        if result.returncode==0 and not publication.get('pending_views'):
                            market_complete=read(base/'market/status.json',{}).get('status')=='COMPLETE'
                            save(base/'status.json',{'状态':'完成（存在待补证或失败项）' if done['缺失或失败'] or not market_complete else '完成',
                                **done,'品类':cat['category_name_zh'],'阶段':state,'时间':time.time(),
                                '结果目录':str(base/'excel'),'范围':'本次榜单和关键词限额内的商品；不是平台全量'})
                            break
                time.sleep(2)
            if (base/'STOP').exists():
                save(base/'status.json',{'状态':'已停止','阶段':state,'结果目录':str(base/'excel')})
        except Exception as exc:
            save(base/'status.json',{'状态':'启动或运行失败','原因':str(exc),'阶段':state,'API请求数':'查看第三层请求账本'})
            raise
        finally:
            rotation_queue.interrupt_pending(base)
            for child in children.values(): terminate_owned(child)
            pending=ROOT/'runtime'/'starting.json'
            if read(active,{}).get('pid')==os.getpid(): active.unlink(missing_ok=True)
            if read(pending,{}).get('pid')==os.getpid(): pending.unlink(missing_ok=True)
            for lease in manual_leases.values(): lease.__exit__(None,None,None)
            try:
                status=read(base/'status.json',{}).get('状态','')
                explicitly_stopped=(base/'STOP').exists()
                naturally_completed=status.startswith('完成')
                if explicitly_stopped or naturally_completed:
                    if explicitly_stopped:
                        from selection_automation.proxy_activity import revoke_all_activity
                        revoke_all_activity(ROOT)
                    from company_setup import cleanup_ended_task
                    cleanup_ended_task(base.name)
                else:
                    from retain_proxy_connections import ensure_running
                    ensure_running(ROOT)
            except (OSError,RuntimeError,subprocess.SubprocessError) as exc:
                save(base/'status.json',{**read(base/'status.json',{}),'代理保留检查异常':type(exc).__name__})


def selected_workers(manifest, specification=None):
    count=len(manifest.get('workers') or [])
    if not count: raise ValueError('浏览器尚未准备完成，当前没有可替换的worker')
    numbers=list(range(1,count+1)) if specification is None else [int(n) for n in specification.split(',')]
    if not numbers or len(set(numbers)) != len(numbers) or any(n not in range(1,count+1) for n in numbers):
        raise ValueError(f'当前可用worker为1..{count}，不能选择不存在或重复的worker')
    return numbers


def request_rotation(base,numbers):
    owner=live_owner()
    if not owner or owner.get('category_key')!=base.name or (base/'STOP').exists():
        raise ValueError('当前没有运行中的采集监督器，换session未受理；请先启动采集任务')
    manifest=read(ROOT/'config'/'sessions.json',{})
    for number in numbers:
        if number not in range(1,len(manifest.get('workers',[]))+1):
            raise ValueError(f'worker {number} 不在当前环境清单中')
        session=manifest['workers'][number-1]
        lane=read(base/'temu/supervisors'/f"{session['name']}.json",{})
        if lane.get('deleted_profile_id')==str(session.get('profile_id') or ''):
            raise ValueError(f"{session['name']} 的浏览器环境已删除，不能再次换session或启动")
    request=rotation_queue.enqueue(base,numbers,owner)
    # Close the common exit-between-check-and-write race; dashboard also checks liveness.
    if live_owner()!=owner:
        rotation_queue.update(base,request,'INTERRUPTED',worker_status={str(w):'INTERRUPTED' for w in numbers})
        raise ValueError('监督器已退出，换session未执行')
    return request


def main():
    p=argparse.ArgumentParser(); p.add_argument('action',choices=['doctor','prepare','start','run','status','stop','rotate','resume-api'])
    p.add_argument('--category',type=Path); p.add_argument('--category-key'); p.add_argument('--amazon-count',type=int); p.add_argument('--per-keyword',type=int)
    p.add_argument('--paid',action='store_true'); p.add_argument('--workers')
    p.add_argument('--prepared-start',action='store_true',help=argparse.SUPPRESS)
    p.add_argument('--preserve-prepared',action='store_true',help='维护同一任务时保留已核实的浏览器与 session')
    a=p.parse_args()
    if a.action=='doctor': print(json.dumps(doctor(),ensure_ascii=False)); return
    if a.action in ('prepare','start','run'):
        if not a.category: p.error('--category required')
        frozen,cat,base=prepare(a.category.resolve(),a.amazon_count,a.per_keyword)
    else:
        import re
        key=a.category_key or (live_owner() or {}).get('category_key')
        if not key or not re.fullmatch('[a-z][a-z0-9_]{0,63}',key): p.error('valid --category-key required')
        base=ROOT/'runs'/key
    if a.action=='prepare': print(json.dumps({'配置':str(frozen),'API请求数':0},ensure_ascii=False))
    elif a.action=='run':
        if not a.prepared_start:
            result=request_start(frozen,cat,base,a.paid,background=False)
            if '后台' in result: print(json.dumps(result,ensure_ascii=False)); return
        supervise(frozen,a.paid,preserve_prepared=a.preserve_prepared)
    elif a.action=='start':
        print(json.dumps(request_start(frozen,cat,base,a.paid,preserve_prepared=a.preserve_prepared),ensure_ascii=False))
    elif a.action=='status': print(json.dumps({'后台':live_owner(),'状态':read(base/'status.json',{'状态':'尚未启动'})},ensure_ascii=False))
    elif a.action=='stop':
        request_task_stop(base)
        owner=live_owner()
        stopping=bool(owner and owner.get('category_key')==base.name)
        save(base/'status.json',{**read(base/'status.json',{}),'状态':'停止中' if stopping else '已停止'})
        if not stopping:
            from company_setup import cleanup_ended_task
            cleanup_ended_task(base.name)
    elif a.action=='resume-api':
        (base/'stage123'/'provider_blocked.json').unlink(missing_ok=True)
        print(json.dumps({'状态':'已请求恢复API，监督器将在下一轮检查'},ensure_ascii=False))
    elif a.action=='rotate':
        try: numbers=selected_workers(read(ROOT/'config'/'sessions.json',{}),a.workers)
        except ValueError as exc: p.error(str(exc))
        try: request=request_rotation(base,numbers)
        except ValueError as exc: p.error(str(exc))
        print(json.dumps({'request_id':request['id'],'status':request['status']},ensure_ascii=False))


if __name__=='__main__': main()
