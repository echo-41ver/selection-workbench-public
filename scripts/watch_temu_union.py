from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
import threading
import os
import math
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from selection_automation.category_config import load_category
from selection_automation.merge_temu_snapshots import atomic_write, merge
HEARTBEAT_INTERVAL=10


def snapshot_signature(paths: list[Path]) -> str:
    digest = hashlib.sha256()
    for path in paths:
        stat = path.stat()
        digest.update(str(path).encode("utf-8"))
        digest.update(str(stat.st_size).encode("ascii"))
        digest.update(str(stat.st_mtime_ns).encode("ascii"))
    return digest.hexdigest()


def publish_once(base, previous=None):
    """Retry failed inputs on every cycle; publish status even while idle."""
    prior=previous or {}
    union=base/'union'; inputs=[]
    state={**prior,'heartbeat_at':time.time(),'updated_at':time.strftime('%Y-%m-%dT%H:%M:%S%z')}
    stopped=threading.Event()
    import psutil
    owner={'owner_pid':os.getpid(),'owner_created':psutil.Process().create_time()}
    progress={**prior,'status':'MERGING','merge_started_at':time.time(),**owner}
    def heartbeat():
        while not stopped.is_set():
            try:
                atomic_write(union/'watch_state.json',json.dumps({**progress,'heartbeat_at':time.time()},ensure_ascii=False,indent=2))
            except OSError:
                # Publication still reports the final error through its normal path.
                pass
            stopped.wait(HEARTBEAT_INTERVAL)
    pulse=threading.Thread(target=heartbeat,name='union-heartbeat',daemon=True)
    pulse.start()
    try:
        inputs=sorted([*(base/'workers').glob('*/*/products.jsonl'),
                       *(base/'workers').glob('*/*/unclassified_products.jsonl')])
        provider=base.parent/'market/geekbi.json'
        category_path=base.parent/'category.json'
        signature_inputs=[*inputs,*([provider] if provider.is_file() else [])]
        if not signature_inputs:
            state.update(status='WAITING_SOURCE',error=None,consecutive_failures=0)
        else:
            signature=snapshot_signature([*signature_inputs,*([category_path] if category_path.is_file() else [])])
            if prior.get('status')!='PUBLISHED' or prior.get('signature')!=signature or not (union/'products.jsonl').exists():
                rows,summary=merge(inputs,provider) if provider.is_file() else merge(inputs)
                if category_path.is_file():
                    from selection_automation.category_subject_filter import filter_category_union
                    category=json.loads(category_path.read_text('utf-8'))
                    rows,summary=filter_category_union(ROOT,category,rows,summary,union)
                summary['analysis_unique_goods']=len(rows)
                # Each source is atomically replaced and read as one complete file.
                # Cross-worker updates are normal: publish this valid observation,
                # retain the pre-read signature so the next cycle catches updates.
                atomic_write(union/'products.jsonl',''.join(json.dumps(row,ensure_ascii=False)+'\n' for row in rows))
                atomic_write(union/'summary.json',json.dumps(summary,ensure_ascii=False,indent=2))
                state.update(unique_goods=len(rows),last_success_at=time.time(),signature=signature)
            state.update(status='PUBLISHED',worker_snapshots=len(inputs),error=None,consecutive_failures=0)
    except Exception as exc:
        state.update(status='SOURCE_READ_FAILED_PRESERVED_PREVIOUS_UNION',error=str(exc)[:1000],
                     consecutive_failures=prior.get('consecutive_failures',0)+1,
                     source_files=[str(p) for p in inputs])
    finally:
        stopped.set()
        pulse.join()
    state.update(heartbeat_at=time.time(),merge_started_at=None,**owner)
    state.pop('health_write_pending',None)
    try:
        atomic_write(union/'watch_state.json',json.dumps(state,ensure_ascii=False,indent=2))
    except PermissionError:
        # A reader can retain the Windows destination beyond atomic_write's
        # bounded retry. Keep this local watcher alive and retry next cycle.
        # Published goods remain intact; the supervisor still sees stale health
        # if the destination never becomes writable.
        state['health_write_pending']=True
    return state


def watch_health(base, now=None):
    path=base/'temu/union/watch_state.json'
    try: state=json.loads(path.read_text('utf-8'))
    except (OSError,ValueError): return {}
    if not isinstance(state,dict): return {}
    def valid_time(value):
        return isinstance(value,(int,float)) and not isinstance(value,bool) and math.isfinite(value) and value>=0
    heartbeat_at=state.get('heartbeat_at',path.stat().st_mtime)
    if not valid_time(heartbeat_at): return {}
    age=(time.time() if now is None else now)-heartbeat_at
    merging=state.get('status')=='MERGING'
    if merging and not valid_time(state.get('merge_started_at')): return {}
    merge_age=max(0,(time.time() if now is None else now)-state.get('merge_started_at',time.time())) if merging else 0
    stale=age>90 or merge_age>600
    failed=state.get('status')=='SOURCE_READ_FAILED_PRESERVED_PREVIOUS_UNION'
    return {'状态':'汇总监督器无心跳，等待自动重启' if stale else '汇总失败，正在自动重试' if failed else
                  '等待采集快照' if state.get('status')=='WAITING_SOURCE' else '正在汇总已采集商品' if merging else '汇总已更新',
            '阻断':stale or failed,'stale':stale,'source_failed':failed,'error':state.get('error'),
            '建议动作':state.get('error') if failed else '',
            '连续失败次数':state.get('consecutive_failures',0),'最近成功更新时间':state.get('last_success_at'),
            'heartbeat_age_seconds':round(age,1),'unique_goods':state.get('unique_goods'),
            'owner_pid':state.get('owner_pid'),'merge_in_progress':merging,'merge_age_seconds':round(merge_age,1),
            'last_success_at':state.get('last_success_at')}


def wait_for_task_interval(stop_file, seconds):
    """The independently recovered watcher must observe the task's normal Stop."""
    deadline=time.monotonic()+seconds
    while not stop_file.exists():
        remaining=deadline-time.monotonic()
        if remaining<=0:return True
        time.sleep(min(.25,remaining))
    return False


def watch(category_path, *, interval=20.0, once=False):
    category=load_category(category_path.resolve())
    base=ROOT/'runs'/category['category_key']/'temu'
    stop_file=base.parent/'STOP'
    if stop_file.exists():return
    state={}
    from selection_automation.process_lock import ProcessLock
    with ProcessLock(base/'union/watcher.lock'):
        while not stop_file.exists():
            state=publish_once(base,state)
            if once:
                if state['status']!='PUBLISHED':raise SystemExit(state.get('error') or 'no TEMU worker snapshots found')
                return
            if not wait_for_task_interval(stop_file,min(30.0,max(2.0,interval))):return


def main() -> None:
    parser = argparse.ArgumentParser(description="Watch independent TEMU worker snapshots and atomically publish a goods_id union")
    parser.add_argument("--category", type=Path, default=ROOT / "config" / "categories" / "bath_towel_set.json")
    parser.add_argument("--interval", type=float, default=20.0)
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    watch(args.category,interval=args.interval,once=args.once)


if __name__ == "__main__":
    main()
