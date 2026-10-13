"""Restart this installation's console; explicit maintenance keeps independent tasks."""
import argparse
import json
import math
import os
import re
from pathlib import Path
import subprocess
import sys
import time
from urllib.request import build_opener, ProxyHandler

import psutil

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
from selection_automation.process_lock import ProcessLock


def status(port):
    with build_opener(ProxyHandler({})).open(f'http://127.0.0.1:{port}/api/status', timeout=15) as response:
        return json.load(response)


def listener(port):
    pids = {c.pid for c in psutil.net_connections(kind='tcp')
            if c.status == psutil.CONN_LISTEN and c.laddr.port == port}
    if not pids:
        return None
    if len(pids) != 1 or None in pids:
        raise RuntimeError('无法唯一确认网页进程，未停止任何程序')
    return psutil.Process(pids.pop())


def validate_console(process, root):
    expected = (root / 'scripts/web_console.py').resolve()
    cwd = Path(process.cwd()).resolve()
    args = process.cmdline()
    if cwd != root.resolve() or not any(
        (cwd / arg).resolve() == expected for arg in args[1:] if arg.endswith('web_console.py')
    ):
        raise RuntimeError('端口由其他程序占用，未停止任何程序')


def validate_maintenance_idle(state):
    """Only the independently owned collection task may remain active."""
    generation=state.get('generation')
    stage3=state.get('stage3_control')
    if (state.get('ok') is not True or state.get('console_busy') is not False or
        state.get('market_busy') is not False or not isinstance(stage3,dict) or
        stage3.get('running') is not False or not isinstance(generation,dict) or
        generation.get('busy') or generation.get('stage') not in (None,'done','complete','completed','error','idle')):
        raise RuntimeError('控制台存在准备、市场或第三层任务，不能保留任务维护重启')


def validate_kept_task(console, root, state):
    """A registered, task-bound supervisor must be independent of this console."""
    validate_maintenance_idle(state)
    try:
        record=json.loads((root/'runtime/active.json').read_text(encoding='utf-8'))
        key=record['category_key'];pid=record['pid'];created=record['created']
        if (not isinstance(key,str) or not re.fullmatch('[a-z][a-z0-9_]{0,63}',key) or
            state.get('category_key')!=key or type(pid) is not int or pid==console.pid or
            type(created) not in (int,float) or not math.isfinite(created) or created<=0):
            raise ValueError('invalid owner identity')
        process=psutil.Process(pid)
        if (not process.is_running() or process.status()==psutil.STATUS_ZOMBIE or
            abs(process.create_time()-created)>=.01 or Path(process.cwd()).resolve()!=root.resolve() or
            console.pid in [parent.pid for parent in process.parents()]):
            raise ValueError('owner is not an independent live task')
        args=process.cmdline();expected=(root/'scripts/selection.py').resolve()
        positions=[i for i,arg in enumerate(args[1:],1) if arg.endswith('selection.py') and
                   (root/arg).resolve()==expected]
        if len(positions)!=1 or args[positions[0]+1]!='run' or args.count('--category')!=1:
            raise ValueError('owner command is not a supervisor')
        target=Path(args[args.index('--category')+1])
        if not target.is_absolute():target=root/target
        if not target.is_file() or target.resolve()!=(root/'runs'/key/'category.json').resolve():
            raise ValueError('owner category does not match this task')
        if json.loads((root/'runtime/active.json').read_text(encoding='utf-8'))!=record:
            raise ValueError('owner registration changed during validation')
        return {'pid':pid,'created':created,'category_key':key}
    except (OSError,ValueError,KeyError,TypeError,IndexError,psutil.Error) as exc:
        raise RuntimeError('无法确认本安装的独立运行任务，未停止任何程序') from exc


def native_process_active(pid):
    """A terminated Windows process can retain its PID while kernel I/O drains."""
    if os.name != 'nt':
        return True
    import ctypes
    from ctypes import wintypes
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    kernel.GetExitCodeProcess.restype = wintypes.BOOL
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = kernel.OpenProcess(0x1000, False, pid)
    if not handle:
        error = ctypes.get_last_error()
        if error == 87: raise psutil.NoSuchProcess(pid)
        if error == 5: raise psutil.AccessDenied(pid)
        raise OSError(error, 'Cannot open process identity')
    try:
        code = wintypes.DWORD()
        if not kernel.GetExitCodeProcess(handle, ctypes.byref(code)):
            raise OSError(ctypes.get_last_error(), 'Cannot read process exit code')
        return code.value == 259
    finally:
        kernel.CloseHandle(handle)


def validate_readonly_idle(console, root, state):
    """Do not orphan a useful reader belonging to this exact console and task."""
    key = state.get('category_key')
    if not isinstance(key, str) or not re.fullmatch('[a-z][a-z0-9_]{0,63}', key):
        return
    expected = (root/'runs'/key).resolve()
    console_created = console.create_time()
    try:
        children = console.children(recursive=True)
        for child in children:
            try:
                created = child.create_time()
                args = child.cmdline()
                if (args.count('-m') != 1 or args[args.index('-m')+1] != 'selection_automation.stage3_progress'
                    or args.count('--base') != 1):
                    continue
                base = Path(args[args.index('--base')+1])
                if not base.is_absolute(): base = root/base
                if base.resolve() != expected or Path(child.cwd()).resolve() != root.resolve():
                    continue
                current = psutil.Process(child.pid)
                if (abs(current.create_time()-created) >= .01 or not current.is_running()
                    or current.status() == psutil.STATUS_ZOMBIE or not native_process_active(child.pid)):
                    continue
                if not any(parent.pid == console.pid and abs(parent.create_time()-console_created) < .01
                           for parent in current.parents()):
                    continue
                raise RuntimeError(f'READONLY_READER_BUSY: 正在核对当前任务结果（PID {child.pid}），请等待完成后维护重启')
            except (psutil.NoSuchProcess, psutil.ZombieProcess, IndexError):
                continue
        if abs(console.create_time()-console_created) >= .01:
            raise RuntimeError('网页身份已变化，未停止任何程序')
    except (OSError, psutil.AccessDenied) as exc:
        raise RuntimeError('无法确认网页只读核对进程状态，未停止任何程序') from exc


def restart(port, root=ROOT, *, keep_task=False, task_root=None, independent=False):
    with ProcessLock(root / f'runtime/web-console/restart-{port}.lock'):
        previous = listener(port)
        kept=None
        if previous:
            validate_console(previous, root)
            state = status(port)
            if keep_task:validate_maintenance_idle(state)
            if state.get('ok') is not True or state.get('active') is not False or state.get('market_busy'):
                if not keep_task or state.get('active') is not True:
                    raise RuntimeError('任务正在运行或状态无法确认，请先在网页停止任务再重启网页')
                owner_root=Path(task_root) if task_root else root
                if (owner_root/'runtime').resolve()!=(root/'runtime').resolve():
                    raise RuntimeError('旧任务与网页不共享同一运行记录，未停止任何程序')
                kept=validate_kept_task(previous,owner_root,state)
            if keep_task: validate_readonly_idle(previous, root, state)
            # psutil checks process identity (PID + creation time) before terminate.
            previous.terminate()
            previous.wait(timeout=15)
        if not independent and listener(port):
            raise RuntimeError('端口仍被占用，未启动重复网页服务')
        logs = root / 'runtime/web-console'
        logs.mkdir(parents=True, exist_ok=True)
        launch = None
        child = None
        if independent and os.name == 'nt':
            from persistent_web_service import start
            launch = start(port, root)
        else:
            with (logs / f'service-{port}.log').open('ab') as output:
                child = subprocess.Popen(
                    [sys.executable, str(root / 'scripts/web_console.py'), '--port', str(port)],
                    cwd=root, stdin=subprocess.DEVNULL, stdout=output, stderr=subprocess.STDOUT,
                    creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0,
                )
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            if child is not None and child.poll() is not None:
                raise RuntimeError(f'网页启动失败，请查看 {logs / f"service-{port}.log"}')
            try:
                current = listener(port)
                if current and (launch is not None or current.pid == child.pid or child.pid in [p.pid for p in current.parents()]):
                    validate_console(current, root)
                    if status(port).get('ok') is True:
                        result={'ok': True, 'pid': current.pid, 'url': f'http://127.0.0.1:{port}/'}
                        if launch:result.update(launch)
                        if kept:result['task_preserved']=kept
                        return result
            except (OSError, ValueError, psutil.Error):
                pass
            time.sleep(.3)
        raise RuntimeError(f'网页启动等待超时，请查看 {logs / f"service-{port}.log"}')


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--port', type=int, default=8765)
    parser.add_argument('--keep-task',action='store_true',help='仅维护重启网页，保留经身份核验的独立采集监督器')
    parser.add_argument('--task-root',type=Path,help='迁移网页后，保留共享同一runtime的原目录监督器')
    parser.add_argument('--ensure-running',action='store_true',help='已有服务则连接，不打断当前任务')
    parser.add_argument('--open-browser',action='store_true')
    parser.add_argument('--attached',action='store_true',help='诊断用途：从当前进程启动，不使用独立 Windows 后台任务')
    args = parser.parse_args()
    if not 1 <= args.port <= 65535:
        parser.error('port must be 1–65535')
    try:
        existing=listener(args.port) if args.ensure_running else None
        if existing:
            validate_console(existing,ROOT)
            result={'ok':True,'pid':existing.pid,'url':f'http://127.0.0.1:{args.port}/'}
        else:
            result=restart(args.port,keep_task=args.keep_task,task_root=args.task_root,independent=not args.attached)
        if args.open_browser:
            import webbrowser
            webbrowser.open(result['url'])
        print(json.dumps(result, ensure_ascii=False))
    except Exception as exc:
        print(str(exc), file=sys.stderr)
        sys.exit(1)
