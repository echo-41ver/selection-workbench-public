#!/usr/bin/env python3
"""Replace one TEMU worker with a new AdsPower profile and ProxyShare session.

Project invariant: a user request to "换session" always means the old AdsPower
profile is permanently deleted first, then a new ProxyShare upstream session
and a new AdsPower profile are created. Collector checkpoints remain on disk.
"""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime


ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from selection_automation.worker_runtime import replacement_guard, stop_owned_collectors
from selection_automation.background_launch import run_background_launcher
from selection_automation.collection_environment import profile_browser_settings
from selection_automation.adspower_diagnostics import AdsPowerAPIError, provider_error
SESSIONS = ROOT / "config" / "sessions.json"
POLICY = ROOT / "runtime" / "proxy_policy.json"
if not POLICY.exists(): POLICY = ROOT / "config" / "proxy_policy.json"
API_FILE = pathlib.Path(os.environ.get("APPDATA", "")) / "adspower_global" / "cwd_global" / "source" / "local_api"


def atomic_json(path: pathlib.Path, value: object) -> None:
    from selection_automation.merge_temu_snapshots import atomic_write
    atomic_write(path, json.dumps(value, ensure_ascii=False, indent=2))


def record_binding_failure(root, task_key, worker_name):
    """An already-running supervisor must observe failure instead of stale RUNNING."""
    path = pathlib.Path(root) / 'runs' / task_key / 'temu/supervisors' / f'{worker_name}.json'
    try:
        prior = json.loads(path.read_text('utf-8'))
    except (OSError, ValueError):
        prior = {}
    atomic_json(path, {**prior, 'status': 'BINDING_FAILED', 'manual_action_required': True})


def update_sessions_profile(worker_name: str, profile_id: str, serial_number: str | None = None) -> None:
    """Cross-process safe manifest update for parallel replacements."""
    lock = ROOT / "runtime" / "sessions-json.lock"
    lock.parent.mkdir(parents=True, exist_ok=True)
    sys.path.insert(0, str(ROOT / 'src'))
    from selection_automation.process_lock import ProcessLock
    guard = ProcessLock(lock)
    acquired = False
    deadline = time.monotonic() + 15
    while not acquired:
        try:
            guard.__enter__()
            acquired = True
        except RuntimeError:
            if time.monotonic() >= deadline:
                raise TimeoutError("sessions.json update lock timeout")
            time.sleep(0.1)
    try:
        latest = json.loads(SESSIONS.read_text("utf-8"))
        target = next((item for item in latest.get("workers", []) if item.get("name") == worker_name), None)
        if not target:
            raise RuntimeError(f"sessions.json lost worker during replacement: {worker_name}")
        if str(target.get('profile_id') or '') != str(profile_id):
            # A different profile must never inherit the old browser number.
            target.pop('serial_number',None)
            target.pop('profile_serial',None)
        target["profile_id"] = str(profile_id)
        if serial_number is not None: target['serial_number']=str(serial_number)
        atomic_json(SESSIONS, latest)
    finally:
        guard.__exit__(None, None, None)


def api_base() -> str:
    explicit = os.environ.get("ADSPOWER_LOCAL_API_URL", "").strip()
    if explicit:
        return explicit.rstrip("/")
    try:
        value = API_FILE.read_text("utf-8").strip()
    except OSError as error:
        raise RuntimeError("AdsPower Local API 未启用；请先在 AdsPower 自动化/API 页面启用") from error
    if not value.startswith(("http://", "https://")):
        raise RuntimeError(f"AdsPower Local API 地址无效: {value!r}")
    return value.rstrip("/")


def api_call(base: str, path: str, *, method: str = "GET", body: dict | None = None, timeout: int = 30) -> dict:
    import re
    opening = path.split('?', 1)[0] == '/api/v1/browser/start'
    if opening:
        from selection_automation.profile_single_use import reserve_start
        profile = urllib.parse.parse_qs(urllib.parse.urlsplit(path).query).get('user_id', [''])[0]
        reserve_start(ROOT, profile, lambda: api_call(base,
            '/api/v1/browser/active?' + urllib.parse.urlencode({'user_id': profile}), timeout=8))
    payload = json.dumps(body).encode("utf-8") if body is not None else None
    headers = {"Accept": "application/json"}
    key = os.environ.get("ADSPOWER_API_KEY", "").strip()
    if key:
        headers["Authorization"] = f"Bearer {key}"
    if payload is not None:
        headers["Content-Type"] = "application/json"
    method = method.upper()
    request = urllib.request.Request(base + path, data=payload, headers=headers, method=method)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    for attempt in range(3):
        try:
            with opener.open(request, timeout=timeout) as response:
                result = json.load(response)
        except urllib.error.HTTPError as error:
            # Never echo response bodies or request URLs: they may contain credentials.
            raise AdsPowerAPIError(path,'HTTP_REJECTED',f'HTTP {error.code}',http_status=error.code) from None
        except (urllib.error.URLError, TimeoutError, OSError):
            # A failed POST may already have created a profile. Do not resend it.
            if method == 'GET' and not opening and attempt < 2:
                time.sleep(attempt + 1)
                continue
            raise AdsPowerAPIError(path,'CONNECTIVITY','connection unavailable; request outcome unconfirmed',retryable=method=='GET') from None
        except (ValueError, TypeError):
            raise RuntimeError("AdsPower Local API 返回无效 JSON；本次请求未确认完成") from None
        if not isinstance(result, dict):
            raise RuntimeError("AdsPower Local API 返回无效结构")
        if result.get("code") not in (None, 0):
            message = str(result.get('msg') or '')
            limited = bool(re.search(r'too many requests?|request(?:s)? frequency|请求(?:频率|过于频繁)|访问过于频繁', message, re.I))
            if limited and not opening and attempt < 2:
                time.sleep(attempt + 1)
                continue
            raise provider_error(path,result.get('code'),message) from None

        return result


def stop_profile_and_wait(base, profile_id, *, api=None, timeout=25):
    """Do not reopen a profile while its previous browser is still closing."""
    from selection_automation.profile_single_use import running_profile
    call = api or api_call
    suffix = urllib.parse.urlencode({'user_id': profile_id})
    try:
        call(base, '/api/v1/browser/stop?' + suffix, timeout=12)
    except RuntimeError as error:
        if 'not open' not in str(error).lower():
            raise
    deadline = time.monotonic() + timeout
    while True:
        state = call(base, '/api/v1/browser/active?' + suffix, timeout=5).get('data') or {}
        alive = running_profile(profile_id)
        if str(state.get('status', '')).lower() == 'inactive' and not alive:
            return
        if time.monotonic() >= deadline:
            raise RuntimeError('Selected profile has not finished closing; retained for retry')
        time.sleep(.25)


def start_profile_with_retry(base, profile_id, attempts=3, api=None):
    """Retry opening the already-created profile; never create another profile."""
    endpoint='/api/v1/browser/start?'+urllib.parse.urlencode({
        'user_id':profile_id,'ip_tab':'0','last_opened_tabs':'0',
        'proxy_detection':'0','headless':'0'})
    for attempt in range(attempts):
        try:
            return (api or api_call)(base,endpoint,timeout=45).get('data') or {}
        except RuntimeError as error:
            if isinstance(error,AdsPowerAPIError) and not error.diagnostic['retryable']:raise
            if attempt+1==attempts: raise
            time.sleep(2*(attempt+1))


def powershell() -> str:
    executable = shutil.which("pwsh") or shutil.which("powershell")
    if not executable:
        raise RuntimeError("未找到 PowerShell")
    return executable


def delete_profile_after_release(base: str, profile_id: str, attempts: int = 60) -> None:
    """Wait for AdsPower to release a stopped browser before deleting its profile."""
    last_error: RuntimeError | None = None
    for attempt in range(attempts):
        try:
            api_call(
                base,
                "/api/v2/browser-profile/delete",
                method="POST",
                body={"profile_id": [profile_id]},
                timeout=15,
            )
            return
        except RuntimeError as error:
            last_error = error
            message = str(error).lower()
            category = error.diagnostic['category'] if isinstance(error, AdsPowerAPIError) else None
            if category in {'CONNECTIVITY', 'PROFILE_IN_USE'} or "无法连接" in message:
                # AdsPower can report "being used" for an already absent profile.
                # Resolve this exact id before repeating an idempotent deletion.
                try:
                    result = api_call(base, "/api/v1/user/list?" + urllib.parse.urlencode(
                        {"user_id": profile_id, "page": 1, "page_size": 100}), timeout=12)
                    profiles = (result.get("data") or {}).get("list")
                    if isinstance(profiles, list):
                        identifiers = []
                        for profile in profiles:
                            if not isinstance(profile, dict):
                                break
                            identifier = next((str(profile[key]).strip()
                                for key in ("user_id", "profile_id", "id")
                                if isinstance(profile.get(key), (str, int))
                                and not isinstance(profile.get(key), bool)
                                and str(profile[key]).strip()), None)
                            if identifier is None:
                                break
                            identifiers.append(identifier)
                        else:
                            # Any malformed element leaves the outcome unknown.
                            if str(profile_id).strip() not in identifiers:
                                return
                except RuntimeError:
                    pass
                if attempt >= 2 and category != 'PROFILE_IN_USE':
                    raise
            elif "being used" not in message and "cannot be deleted" not in message:
                raise
            # AdsPower may acknowledge the first stop request before its
            # browser process releases the profile. Reassert stop periodically
            # while retaining the same bounded replacement transaction.
            if attempt and attempt % 10 == 9:
                try:
                    api_call(
                        base,
                        "/api/v1/browser/stop?" + urllib.parse.urlencode({"user_id": profile_id}),
                        timeout=12,
                    )
                except RuntimeError as stop_error:
                    if "not open" not in str(stop_error).lower():
                        last_error = stop_error
            if attempt + 1 < attempts:
                time.sleep(1)
    raise RuntimeError(f"AdsPower 环境关闭后重试 {attempts} 次仍未释放，无法删除: {last_error}")


def timed_phase(path, worker, stage, action, *args, **kwargs):
    started=time.monotonic();error_type=None
    try:
        return action(*args,**kwargs)
    except Exception as exc:
        error_type=type(exc).__name__
        raise
    finally:
        event={'worker':worker,'stage':stage,'utc_epoch':time.time(),
               'elapsed_seconds':round(time.monotonic()-started,3),'error_type':error_type}
        try:
            path.parent.mkdir(parents=True,exist_ok=True)
            with path.open('a',encoding='utf-8') as stream:
                stream.write(json.dumps(event)+'\n')
        except OSError:
            # A diagnostic write must not alter browser recovery or its exception.
            pass


def replace_worker(args) -> dict:
    replacement_started = time.monotonic()
    timing_path=ROOT/'runtime/proxyshare-chains'/f'replacement-timing-worker-{args.worker}.jsonl'
    def measured(stage,action,*positional,**named):
        return timed_phase(timing_path,args.worker,stage,action,*positional,**named)

    policy = json.loads(POLICY.read_text("utf-8"))
    if policy.get("session_rotation_policy") != "NEW_ADSPOWER_PROFILE_AND_NEW_PROXY_SESSION":
        raise RuntimeError("proxy_policy.json 不允许执行非完整环境替换")
    manifest = json.loads(SESSIONS.read_text("utf-8"))
    from selection import identity
    if manifest.get('machine_id') != identity():
        raise RuntimeError('Refusing to delete profiles owned by another installation')
    journal_path = ROOT / 'runtime' / 'proxyshare-chains' / f'replacement-worker-{args.worker}.json'
    try:
        journal = json.loads(journal_path.read_text('utf-8'))
    except (OSError, ValueError):
        journal = {}
    resume = bool(journal.get('old_profile_id')) and journal.get('phase') in ('OLD_DELETED', 'PROXY_READY', 'PROFILE_CREATE_PENDING', 'PROFILE_CREATED', 'BIND_FAILED')
    phase = journal.get('phase') if resume else ''
    def checkpoint(next_phase, **fields):
        nonlocal phase, journal
        phase = next_phase
        journal = {**journal, **fields, 'phase': phase, 'worker': args.worker, 'updated_at': datetime.now().isoformat(timespec='seconds')}
        atomic_json(journal_path, journal)
        if next_phase == 'BIND_FAILED':
            record_binding_failure(ROOT, task_key, worker_name)
    worker_name = f"worker-{args.worker:02d}-new"
    current = next((item for item in manifest.get("workers", []) if item.get("name") == worker_name), None)
    if not current:
        raise RuntimeError(f"sessions.json 缺少 {worker_name}")
    old_profile = str(current.get("profile_id") or "")
    if not old_profile:
        raise RuntimeError(f"{worker_name} 缺少旧 profile_id")
    # A previous task's unfinished replacement cannot supply this task's
    # profile or proxy session. Resume only the currently registered lineage.
    if resume and old_profile not in {str(journal.get('old_profile_id') or ''), str(journal.get('new_profile_id') or '')}:
        resume=False
        phase=''
        journal={}

    from selection_automation.profile_single_use import closed_used_profile
    if resume and old_profile == str(journal.get('new_profile_id') or '') and closed_used_profile(ROOT, old_profile):
        # An interrupted binding must not keep retrying a profile already closed.
        # Start a fresh replacement lineage, including a fresh proxy session.
        resume=False
        phase=''
        journal={}

    shell = powershell()
    base = api_base()

    # Latest user invariant: deletion is the FIRST external action. Persisted
    # collector checkpoints remain on disk, but the AdsPower profile itself is
    # permanently removed through the same Local API used by the AdsPower MCP.
    port = policy.get('webdriver_ports', list(range(4451,4483)))[args.worker-1]
    current_task = json.loads((ROOT/'runtime/last_run.json').read_text('utf-8'))['category_key']
    task_key = getattr(args, 'task_key', None) or current_task
    if task_key != current_task:
        raise RuntimeError('Refusing collector stop for a different current task')
    measured('stop_old_collector',stop_owned_collectors,ROOT,worker_name,task_key)
    if not resume:
        try:
            measured('close_old_browser',api_call,base, "/api/v1/browser/stop?" + urllib.parse.urlencode({"user_id": old_profile}), timeout=12)
        except RuntimeError as error:
            if "not open" not in str(error).lower():
                raise
        measured('release_delete_old_profile',delete_profile_after_release,base, old_profile)
        checkpoint('OLD_DELETED', old_profile_id=old_profile, new_profile_id=None, bind_restart_attempted=False)
    else:
        old_profile = journal['old_profile_id']

    from selection_automation.browser_transport import local_vpn,adspower_proxy
    from selection import read
    transport=read(ROOT/'runtime/secrets/proxyshare.settings.json',{})
    local=local_vpn(transport)
    chain = None if local else measured('proxy_session_ready',run_background_launcher,
        [shell, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(ROOT / "scripts" / "proxyshare_chain_manager.ps1"),
         "-Action", "start" if phase in ('PROXY_READY','PROFILE_CREATE_PENDING','PROFILE_CREATED','BIND_FAILED') else "prepare-replacement", "-Worker", str(args.worker),
         "-StateFile", f"runtime/proxyshare-chains/state-worker-{args.worker}.json"],
        cwd=ROOT, timeout=180,
    )
    if chain is not None and chain.returncode:
        raise RuntimeError(f"新 ProxyShare session 验收失败: {(chain.stderr or chain.stdout)[-1000:]}")
    if phase not in ('PROFILE_CREATE_PENDING', 'PROFILE_CREATED', 'BIND_FAILED'): checkpoint('PROXY_READY')

    stamp = datetime.now().astimezone()
    chain_port = policy['worker_chain_ports'][args.worker-1]
    create_body = {
        "group_id": "0",
        "name": journal.get("profile_create_name") or f"TEMU-{identity()[:10]}-W{args.worker}-replacement-{stamp:%Y%m%d-%H%M%S}",
        **profile_browser_settings(),
        "remark": "NEW profile + NEW ProxyShare chained session; automatic replacement",
        "user_proxy_config": {
            "proxy_soft": "other", "proxy_type": "http", "proxy_host": "127.0.0.1",
            "proxy_port": str(chain_port), "proxy_user": "", "proxy_password": "", "proxy_url": "",
        },
    }
    new_profile = journal.get('new_profile_id') if phase in ('PROFILE_CREATED', 'BIND_FAILED') else None
    if local:
        create_body['user_proxy_config']=adspower_proxy(transport)
        create_body['remark']='Replacement for bounded transport recovery; unchanged local VPN route'
    if not new_profile:
        from selection_automation.profile_capacity import list_profiles
        def already_created():
            matches=[row for row in list_profiles(api_call,base) if row.get('name')==create_body['name']]
            if len(matches)>1: raise RuntimeError('Multiple profiles match the saved creation request; refusing to duplicate')
            return str(matches[0].get('user_id') or matches[0].get('profile_id') or '') if matches else None
        checkpoint('PROFILE_CREATE_PENDING',profile_create_name=create_body['name'])
        new_profile=already_created()
        if not new_profile:
            try:
                created = measured('create_new_profile',api_call,base, "/api/v2/browser-profile/create", method="POST", body=create_body, timeout=45)
                data = created.get("data") or {}
                new_profile = data if isinstance(data, str) else data.get("profile_id") or data.get("user_id") or data.get("id")
            except RuntimeError:
                new_profile=already_created()
                if not new_profile: raise
    if not new_profile:
        raise RuntimeError("AdsPower 创建新环境后没有返回 profile_id")
    checkpoint('PROFILE_CREATED', new_profile_id=str(new_profile))
    serial_number=None
    try:
        from selection_automation.profile_capacity import list_profiles
        serial_number=next((row.get('serial_number') for row in list_profiles(api_call,base)
                            if str(row.get('user_id') or row.get('profile_id') or '')==str(new_profile)),None)
    except RuntimeError:
        pass
    update_sessions_profile(worker_name, str(new_profile),serial_number)
    try:
        started = measured('start_new_browser',start_profile_with_retry,base,new_profile)
        marionette = int(started.get("marionette_port") or 0)
        geckodriver = str(started.get("webdriver") or "")
        if not pathlib.Path(geckodriver).is_file():
            raise RuntimeError("新环境未返回可用 geckodriver，保留现有环境待重绑")
    except Exception as error:
        checkpoint('BIND_FAILED', bind_error=str(error)[-800:])
        raise

    bind_error = ''
    for bind_attempt in range(1, 3):
        try:
            bind = measured(f'bind_browser_attempt_{bind_attempt}',run_background_launcher,
                [shell, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(ROOT / "scripts" / "bind_flower_worker.ps1"),
                 "-WorkerName", worker_name, "-ProfileId", str(new_profile), "-MarionettePort", str(marionette), "-WebDriverPort", str(port),
                 "-GeckoDriverPath", geckodriver, "-WaitSeconds", "60"],
                cwd=ROOT, timeout=145,
            )
            if not bind.returncode:
                break
            bind_error = (bind.stderr or bind.stdout or 'binding failed')[-1000:]
        except subprocess.TimeoutExpired:
            bind_error = 'WebDriver binding launcher timed out after 75 seconds'
        checkpoint('BIND_FAILED', new_profile_id=str(new_profile), bind_attempts=bind_attempt,
                   bind_error=bind_error)
        # Retry binding only while the newly opened browser is still running.
        # Closing it here consumes the profile and must never be followed by start.
        from selection_automation.profile_single_use import running_profile
        if not running_profile(str(new_profile)):
            raise RuntimeError('新环境已关闭，必须新建环境和session；禁止再次打开')

    else:
        raise RuntimeError(f"新环境 WebDriver 绑定失败（同一环境已尝试2次，保留待重绑）: {bind_error}")

    update_sessions_profile(worker_name, str(new_profile),serial_number)
    audit = {
        "schema_version": "proxyshare-environment-replacement-v1",
        "worker": args.worker,
        "worker_name": worker_name,
        "old_profile_id": old_profile,
        "new_profile_id": new_profile,
        "new_proxy_session": True,
        "local_chain_port": chain_port,
        "old_profile_deleted": True,
        "replaced_at": stamp.isoformat(timespec="seconds"),
        "phase": "REPLACED",
    }
    atomic_json(ROOT / "runtime" / "proxyshare-chains" / f"replacement-worker-{args.worker}.json", audit)

    elapsed = round(time.monotonic() - replacement_started, 3)
    speed_target = int(policy.get("replacement_speed_target_seconds") or 60)
    return {
        "status": "REPLACED", **audit, "collector_started": False,
        "elapsed_seconds": elapsed, "speed_target_seconds": speed_target,
        "speed_target_met": elapsed < speed_target,
    }


def main():
    global SESSIONS
    parser = argparse.ArgumentParser()
    parser.add_argument('--worker', type=int, required=True, choices=range(1, 33))
    parser.add_argument('--start-collector', action='store_true')
    parser.add_argument('--category', type=pathlib.Path)
    parser.add_argument('--sessions', type=pathlib.Path, default=SESSIONS)
    parser.add_argument('--supervisor-token')
    parser.add_argument('--task-key')
    args = parser.parse_args()
    SESSIONS = args.sessions.resolve()
    worker_name = f"worker-{args.worker:02d}-new"
    from selection_automation.process_lock import ProcessLock
    with replacement_guard(ROOT / 'runtime' / 'worker-leases', worker_name, args.supervisor_token):
        with ProcessLock(ROOT / 'runtime' / f'replacement-{args.worker}.lock'):
            result = replace_worker(args)
    if args.start_collector:
        child = subprocess.Popen(
            [sys.executable, str(ROOT / "scripts" / "pipeline.py"), "collect-temu",
             "--category", str(args.category or ROOT / "config" / "categories" / "bath_towel_set.json"),
             "--sessions", str(SESSIONS), "--worker-name", worker_name],
            cwd=ROOT,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=(subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS)
            if os.name == "nt" else 0,
        )
        result.update(collector_started=True, supervisor_pid=child.pid)
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__": main()
