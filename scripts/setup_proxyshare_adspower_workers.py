#!/usr/bin/env python3
"""Bootstrap the requested number of owned workers, reclaiming inactive owned profiles on capacity limits."""

from __future__ import annotations

import argparse
import json
import pathlib
import re
import subprocess
import time
import urllib.parse
from datetime import datetime

from replace_proxyshare_worker_environment import ROOT, SESSIONS, api_base, api_call, atomic_json, powershell, start_profile_with_retry
from selection_automation.adspower_diagnostics import AdsPowerAPIError
from selection_automation.background_launch import run_background_launcher
from selection_automation.collection_environment import profile_browser_settings


def is_account_capacity_error(error: Exception) -> bool:
    # Only AdsPower's explicit account-limit response is a capacity signal.
    # Network failures, rate limits, permissions, and bind failures stay errors.
    return bool(re.search(r"\bnumber of (?:imported )?accounts exceeds (?:the )?limit of\s+\d+\b", str(error), re.I))


def capacity_blocked(reason: str) -> RuntimeError:
    return RuntimeError("ADSPOWER_CAPACITY_BLOCKED: AdsPower 环境额度已满，无法凑齐所选路数的环境；"
                        "自动检查后没有可安全清理的本项目停用环境；保留其他任务环境。" + reason)


def cleanup_completed_journal(path: pathlib.Path) -> None:
    # Called only after publishing the complete sessions manifest. A monitoring
    # reader on Windows can hold the old checkpoint; cleanup is not provisioning.
    try:
        path.unlink(missing_ok=True)
    except PermissionError:
        print(json.dumps({'warning': 'completed_bootstrap_checkpoint_cleanup_deferred'}))


def retire_for_task(task_key):
    """Retire only profile IDs recorded by this installation, snapshot first."""
    from selection import identity, read, live_owner
    from selection_automation.profile_capacity import list_profiles
    from replace_proxyshare_worker_environment import delete_profile_after_release
    if not re.fullmatch('[a-z][a-z0-9_]{0,63}',task_key): raise ValueError('invalid task key')
    owner=live_owner()
    if owner and owner.get('category_key') not in (None,task_key):
        raise RuntimeError('另一任务正在使用环境，不能退役')
    files=[SESSIONS, ROOT/'runtime/bootstrap.json']
    stale=[]
    for file in files:
        value=read(file,{})
        if not value: continue
        if value.get('machine_id')!=identity(): raise RuntimeError('拒绝删除其他安装的环境')
        from selection_automation.profile_single_use import running_profile
        closed_used = any((ROOT/'runtime/profile-single-use'/f"{w['profile_id']}.json").exists()
                          and not running_profile(str(w['profile_id']))
                          for w in value.get('workers', []) if w.get('profile_id'))
        if value.get('task_key')!=task_key or value.get('used') is True or closed_used:
            stale.append((file,value))
    # A leftover completed journal must retire with the published manifest;
    # otherwise it can resurrect profile IDs that were already deleted.
    if any(file == SESSIONS for file,_ in stale):
        journal_file=ROOT/'runtime/bootstrap.json'
        if all(file != journal_file for file,_ in stale):
            journal=read(journal_file,{})
            if journal:
                if journal.get('machine_id')!=identity(): raise RuntimeError('拒绝删除其他安装的环境')
                stale.append((journal_file,journal))
    if not stale:return
    archive=ROOT/'runtime/profile_retirement'/datetime.now().strftime('%Y%m%d-%H%M%S-%f')
    snapshot={'machine_id':identity(),'new_task_key':task_key,'records':[{'path':str(f),'manifest':v} for f,v in stale],'deleted':[],'already_missing':[]}
    atomic_json(archive.with_suffix('.json'),snapshot)
    base=api_base()
    known={str(row.get('user_id') or row.get('profile_id') or '') for row in list_profiles(api_call,base)}
    identifiers={str(w['profile_id']) for _,value in stale for w in value.get('workers',[]) if w.get('profile_id')}
    for identifier in sorted(identifiers):
        if identifier not in known:
            snapshot['already_missing'].append(identifier)
        else:
            try: api_call(base,'/api/v1/browser/stop?'+urllib.parse.urlencode({'user_id':identifier}),timeout=12)
            except RuntimeError as error:
                if 'not open' not in str(error).lower():raise
            delete_profile_after_release(base,identifier)
            snapshot['deleted'].append(identifier)
        atomic_json(archive.with_suffix('.json'),snapshot)
    for file,_ in stale:
        deadline = time.monotonic() + 5
        while True:
            try:
                file.unlink(missing_ok=True)
                break
            except PermissionError:
                if time.monotonic() >= deadline:
                    raise RuntimeError('旧环境已清理，但环境清单暂被占用；点击重试准备环境即可继续')
                time.sleep(.05)


def _main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--force-new-manifest", action="store_true", help="Create new profiles even if sessions.json already exists")
    parser.add_argument("--retry-capacity", action="store_true", help="Retry creation only after the company account capacity has changed")
    parser.add_argument("--new-task")
    args = parser.parse_args()
    if args.new_task: retire_for_task(args.new_task)

    if SESSIONS.exists() and not args.force_new_manifest:
        try:
            existing = json.loads(SESSIONS.read_text("utf-8")).get("workers") or []
        except (OSError, json.JSONDecodeError):
            existing = []
        if existing:
            raise SystemExit("sessions.json 已有 worker；首次建环境需空清单，或明确使用 --force-new-manifest")

    from selection import identity, read
    journal_path = ROOT / 'runtime' / 'bootstrap.json'
    journal = read(journal_path, {})
    if journal and journal.get('machine_id') != identity():
        raise RuntimeError('Cannot resume another installation bootstrap')
    prior_journal_profiles = {str(w['profile_id']) for w in journal.get('workers', []) if w.get('profile_id')}
    if args.force_new_manifest:
        journal = {}
    if not journal:
        journal = {'machine_id': identity(), 'task_key': args.new_task, 'used': False, 'workers': [], 'created_at': datetime.now().astimezone().isoformat()}
        atomic_json(journal_path, journal)
    journal.pop('capacity_limit', None)  # Every explicit start may automatically retry capacity.

    shell = powershell()
    base = api_base()
    # A harmless authenticated read fails before creating a new profile or chain.
    try:
        api_call(base,'/api/v1/user/list?page=1&page_size=1',timeout=15)
    except AdsPowerAPIError as error:
        journal['preflight_error']=error.diagnostic
        atomic_json(journal_path,journal)
        raise
    from selection_automation.browser_transport import local_vpn,adspower_proxy
    transport=read(ROOT/'runtime/secrets/proxyshare.settings.json',{})
    local=local_vpn(transport)
    chain = None if local else run_background_launcher(
        [shell, "-NoProfile", "-File", str(ROOT / "scripts" / "proxyshare_chain_manager.ps1"), "-Action", "start" if journal.get("proxy_sessions_ready") else "prepare-replacement"],
        cwd=ROOT, timeout=90+10*len((read(ROOT/'runtime/proxy_policy.json',{}) or {}).get('worker_chain_ports',[])),
    )
    if chain is not None and chain.returncode:
        raise RuntimeError(f"ProxyShare 链启动/验收失败: {(chain.stderr or chain.stdout)[-1200:]}")

    journal['proxy_sessions_ready']=True
    atomic_json(journal_path,journal)
    base = api_base()
    workers: list[dict] = journal['workers']
    stamp = datetime.now().astimezone()
    policy = read(ROOT / 'runtime' / 'proxy_policy.json') or read(ROOT / 'config' / 'proxy_policy.json')
    gecko_ports = policy.get('webdriver_ports', list(range(4451,4455)))
    from selection_automation.workers import worker_count
    target_count = worker_count(policy.get('worker_count', 4))
    if len(policy.get('worker_chain_ports') or []) != target_count or len(gecko_ports) != target_count:
        raise RuntimeError('所选路数与代理及 WebDriver 端口数量不一致，初始化未完成')
    requested_count = int(journal.setdefault('requested_worker_count', target_count))
    if requested_count != target_count:
        raise RuntimeError('初始化断点的路数与配置不同，请创建新任务后重试')
    for number, webdriver_port in zip(range(1, target_count + 1), gecko_ports):
        existing = next((w for w in workers if w['name'] == f'worker-{number:02d}-new'), None)
        if existing and existing.get('bootstrap_status') == 'BOUND':
            continue
        chain_port = policy['worker_chain_ports'][number-1]
        body = {
            "group_id": "0",
            "name": f"TEMU-{identity()[:10]}-W{number}-{stamp:%Y%m%d-%H%M%S}",
            **profile_browser_settings(),
            "remark": "Portable TEMU worker; local ProxyShare chain; never configure ProxyShare directly",
            "user_proxy_config": {
                "proxy_soft": "other", "proxy_type": "http", "proxy_host": "127.0.0.1",
                "proxy_port": str(chain_port), "proxy_user": "", "proxy_password": "", "proxy_url": "",
            },
        }
        if local:
            body['user_proxy_config']=adspower_proxy(transport)
            body['remark']='Portable TEMU worker; local VPN route'
        profile_id = existing.get('profile_id') if existing else None
        if not profile_id:
            from selection_automation.profile_capacity import reclaim_one
            for creation_attempt in range(2):
                try:
                    response = api_call(base, "/api/v2/browser-profile/create", method="POST", body=body, timeout=45)
                    break
                except RuntimeError as error:
                    if not is_account_capacity_error(error):
                        raise
                    protected = prior_journal_profiles | {str(w['profile_id']) for w in workers if w.get('profile_id')}
                    protected.update(str(w['profile_id']) for w in (read(SESSIONS, {}).get('workers') or []) if w.get('profile_id'))
                    reclaimed = None
                    if creation_attempt == 0:
                        reclaimed = reclaim_one(api_call, base, identity(), protected,
                                                ROOT/'runtime'/'profile_reclamation', atomic_json,
                                                multi_device_mode=policy.get('adspower_multi_device_mode'))
                    if reclaimed:
                        continue
                    journal['capacity_limit'] = {'kind':'ADSPOWER_ACCOUNT_LIMIT',
                        'message':'No safely reclaimable inactive owned profile', 'blocked_worker':number,
                        'detected_at':datetime.now().astimezone().isoformat()}
                    atomic_json(journal_path,journal)
                    raise capacity_blocked('所选路数初始化未完成；已保存可续作环境。') from error
            data = response.get("data") or {}
            profile_id = data if isinstance(data, str) else data.get("profile_id") or data.get("user_id") or data.get("id")
            if not profile_id:
                raise RuntimeError(f"worker {number} 创建环境后没有 profile_id；保留已创建环境")
            existing = {
                'name': f'worker-{number:02d}-new', 'profile_id': str(profile_id),
                'webdriver_url': f'http://127.0.0.1:{webdriver_port}',
                'session_id_file': f'runtime/worker-{number:02d}-new/flower_session_id.txt',
                'bidi_url_file': f'runtime/worker-{number:02d}-new/flower_bidi_url.txt',
                'bootstrap_status': 'PROFILE_CREATED',
            }
            workers.append(existing)
            # Persist before start/bind so a failed first installation can resume.
            atomic_json(journal_path, journal)
        try:
            started = start_profile_with_retry(base,profile_id,api=api_call)
        except AdsPowerAPIError as error:
            existing['startup_error']=error.diagnostic
            existing['bootstrap_status']='PROFILE_CREATED'
            atomic_json(journal_path,journal)
            raise
        existing.pop('startup_error',None)
        marionette = int(started.get("marionette_port") or 0)
        geckodriver = str(started.get("webdriver") or "")
        if not pathlib.Path(geckodriver).is_file():
            raise RuntimeError(f"worker {number} 新环境未返回可用 Marionette/geckodriver")
        for bind_attempt in range(1):
            try:
                bind = run_background_launcher(
                    [shell, "-NoProfile", "-File", str(ROOT / "scripts" / "bind_flower_worker.ps1"),
                     "-WorkerName", f"worker-{number:02d}-new", "-ProfileId", str(profile_id), "-MarionettePort", str(marionette),
                     "-WebDriverPort", str(webdriver_port), "-GeckoDriverPath", geckodriver, "-WaitSeconds", "60"],
                    cwd=ROOT, timeout=75,
                )
                if not bind.returncode: break
                existing['bind_error']=(bind.stderr or bind.stdout or 'bind failed')[-1000:]
            except subprocess.TimeoutExpired:
                existing['bind_error']='binding launcher timed out after 75 seconds'
            existing['bootstrap_status']='BIND_FAILED'
            atomic_json(journal_path,journal)
            raise RuntimeError(f"worker {number} WebDriver 绑定失败；保留环境，不关闭重开；原因: {existing.get('bind_error','')}")
        existing['bootstrap_status'] = 'BOUND'
        atomic_json(journal_path, journal)

    bound_workers = [worker for worker in workers if worker.get('bootstrap_status') == 'BOUND']
    if len(bound_workers) != target_count:
        raise RuntimeError('所选路数初始化未完成，保留断点；不发布部分就绪清单')
    try:
        from selection_automation.profile_capacity import list_profiles
        serials={str(row.get('user_id') or row.get('profile_id') or ''):row.get('serial_number')
                 for row in list_profiles(api_call,base)}
        for worker in bound_workers:
            serial=serials.get(worker['profile_id'])
            if serial is not None: worker['serial_number']=str(serial)
    except RuntimeError:
        pass
    capacity_limited = False
    policy['worker_chain_ports'] = list(policy['worker_chain_ports'])[:len(bound_workers)]
    policy['webdriver_ports'] = list(gecko_ports)[:len(bound_workers)]
    policy['worker_count'] = len(bound_workers)
    policy['capacity_limited'] = capacity_limited
    atomic_json(ROOT / 'runtime' / 'proxy_policy.json', policy)
    manifest = {
        "schema_version": "temu-browser-sessions-v1",
        "created_for_run": f"portable-bootstrap-{stamp:%Y%m%d-%H%M%S}",
        "reuse_policy": "NEW_PROFILES_ONLY",
        "proxy_architecture": "LOCAL_VPN" if local else "LOCAL_CHAIN_PROXYSHARE",
        "session_rotation_policy": "BOUNDED_TRANSPORT_RECOVERY" if local else "NEW_ADSPOWER_PROFILE_AND_NEW_PROXY_SESSION",
        "workers": bound_workers,
        "worker_count": len(bound_workers),
        "desired_worker_count": requested_count,
        "capacity_limited": capacity_limited,
        "capacity_limit": journal.get('capacity_limit'),
        "machine_id": identity(),
        "task_key": args.new_task,
        "used": False,
    }
    atomic_json(SESSIONS, manifest)
    cleanup_completed_journal(journal_path)
    print(json.dumps({"status": "READY", "profiles_created": len(bound_workers),
                      "worker_count": len(bound_workers), "desired_worker_count": requested_count,
                      "capacity_limited": capacity_limited, "sessions_manifest": str(SESSIONS)}, ensure_ascii=False))


def main():
    import sys
    sys.path.insert(0, str(ROOT / 'src'))
    from selection_automation.process_lock import ProcessLock
    with ProcessLock(ROOT / 'runtime' / 'bootstrap.lock'):
        _main()


if __name__ == "__main__": main()
