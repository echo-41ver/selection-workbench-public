"""Attach analysis to an already-running legacy collection task without restarting browsers."""
import argparse
import os
from pathlib import Path
import sys
import time

import psutil
from selection import ROOT, read, save, spawn, live_owner, terminate_owned
from selection_automation.category_config import load_category
from selection_automation.process_lock import ProcessLock


def paid_requested(base):
    marker = read(base / 'stage123/paid_enabled.json', {})
    return marker.get('category_key') == base.name and marker.get('source') == 'web-console'


def handoff(base):
    """A resumed full supervisor owns analysis; never run two watchers or vision workers."""
    state = read(base / 'analysis_bridge.json', {})
    try:
        process = psutil.Process(state['pid'])
        if abs(process.create_time() - state['created']) > .01:
            return
    except (KeyError, psutil.Error):
        return
    (base / 'analysis_bridge.handoff').write_text('full supervisor taking ownership', encoding='utf-8')
    try:
        process.wait(timeout=15)
    except psutil.TimeoutExpired:
        raise RuntimeError('本地分析交接未完成，暂不启动重复分析进程') from None


def attach(category_path):
    category_path = Path(category_path).resolve()
    cat = read(category_path)
    base = ROOT / 'runs' / cat['category_key']
    if category_path != (base / 'category.json').resolve():
        raise ValueError('只允许衔接已有任务的冻结配置')
    owner = live_owner()
    if not owner or owner.get('category_key') != base.name:
        raise ValueError('只用于当前仍在运行的旧采集任务；新任务直接使用网页运行按钮')
    with ProcessLock(base / 'analysis_bridge.start.lock'):
        old = read(base / 'analysis_bridge.json', {})
        try:
            process = psutil.Process(old['pid'])
            if abs(process.create_time() - old['created']) < .01:
                return old
        except (KeyError, psutil.Error):
            pass
        # Validate all analysis rules before changing the old task's execution mode.
        cat['execution_mode'] = 'full'
        preview = base / 'analysis_bridge.category.json'
        save(preview, cat)
        load_category(preview)
        if not (base / 'analysis_bridge.original.json').exists():
            save(base / 'analysis_bridge.original.json', read(category_path))
        save(category_path, cat)
        from pipeline import write_stage12_config
        config = write_stage12_config(cat)
        output = Path(read(config)['output_dir'])
        output.mkdir(parents=True, exist_ok=True)
        (output / 'STOP').unlink(missing_ok=True)
        (base / 'analysis_bridge.handoff').unlink(missing_ok=True)
        child = spawn([sys.executable, str(Path(__file__).resolve()), 'watch', '--category', str(category_path)],
                      base / 'logs/analysis_bridge.log')
        state = {'pid':child.pid, 'created':psutil.Process(child.pid).create_time(), 'collector_owner':owner,
                 'category_key':base.name, 'attached_at':time.time(), 'status':'starting'}
        save(base / 'analysis_bridge.json', state)
        return state


def watch(category_path):
    cat = load_category(category_path)
    base = ROOT / 'runs' / cat['category_key']
    with ProcessLock(base / 'analysis_bridge.lock'):
        # Parent writes the exact collector identity before this process starts work.
        deadline = time.monotonic() + 10
        state = read(base / 'analysis_bridge.json', {})
        while not state.get('collector_owner') and time.monotonic() < deadline:
            time.sleep(.1); state = read(base / 'analysis_bridge.json', {})
        if not state.get('collector_owner'):
            raise RuntimeError('分析进程未注册')
        # Windows venv python.exe can be a launcher with a separate child PID.
        state.update(pid=os.getpid(), created=psutil.Process().create_time())
        children = {}; attempts = {}; next_at = {}; last_publish = 0
        config = base / 'stage12_config.json'
        output = Path(read(config)['output_dir'])
        def service(name, args, repeat=False):
            child = children.get(name)
            if child and child.poll() is None:
                return
            if child:
                children.pop(name)
                state[name] = {'exit_code':child.returncode, 'status':'failed' if child.returncode else 'finished'}
                attempts[name] = attempts.get(name, 0) + (1 if child.returncode else 0)
                next_at[name] = time.monotonic() + 30
                if not repeat and child.returncode == 0:
                    attempts[name] = 3
            if attempts.get(name, 0) >= 3 or time.monotonic() < next_at.get(name, 0):
                return
            children[name] = spawn([sys.executable, str(ROOT/'scripts'/args[0]), *map(str,args[1:])], base/'logs'/f'bridge_{name}.log')
            state[name] = {'pid':children[name].pid, 'status':'running'}
        try:
            while not (base/'STOP').exists() and not (output/'STOP').exists() and not (base/'analysis_bridge.handoff').exists():
                owner = live_owner()
                if owner and owner != state['collector_owner']:
                    break
                service('stage12', ['run_incremental_stage12.py','watch','--config',config])
                candidates = output / f'layer3_candidates_{base.name}.jsonl'
                if paid_requested(base) and candidates.is_file():
                    service('stage3',['run_category_layer3.py','--category',category_path,'--candidates',candidates,'--results',base/'stage123/results.jsonl'],repeat=True)
                else:
                    state['stage3'] = {'status':'waiting for webpage button; no paid request'}
                # The old collector already publishes every minute. Keep publishing
                # snapshots after it finishes, while local analysis catches up.
                if not owner and time.monotonic()-last_publish >= 60:
                    service('publish',['publish_all.py','--category',category_path],repeat=True)
                    last_publish = time.monotonic()
                save(base/'analysis_bridge.json',{**state,'status':'running','heartbeat':time.time()})
                time.sleep(1)
        finally:
            for child in children.values():
                terminate_owned(child)
            save(base/'analysis_bridge.json',{**state,'status':'stopped','stopped_at':time.time()})


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=['attach','watch'])
    parser.add_argument('--category', type=Path, required=True)
    args = parser.parse_args()
    if args.command == 'attach':
        import json
        print(json.dumps(attach(args.category), ensure_ascii=False))
    else:
        watch(args.category)
