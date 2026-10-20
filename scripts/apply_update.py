"""Apply a verified code update without replacing installation data or dependencies."""
from __future__ import annotations

import argparse
import ast
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import subprocess
import sys
import time
from urllib.request import build_opener, ProxyHandler
import uuid

SCHEMA = 'selection-code-update-v1'
PROTECTED = {'.git', '.githooks', '.venv', 'node_modules', 'runtime', 'runs', 'dist', 'work', 'logs',
             '__pycache__', '.pytest_cache', '.deployment-backup', 'deployment-assets'}
METADATA = {'BUILD_INFO.json', 'UPDATE_BASE.json', 'SOURCE_SYNC.json'}
DEPENDENCY_FILES = {'requirements.txt', 'requirements.lock.txt', 'package.json', 'package-lock.json', 'pyproject.toml'}
SHA256 = re.compile(r'[0-9a-f]{64}')
COMMIT = re.compile(r'[0-9a-f]{40}')


def checked_name(name):
    if not isinstance(name, str) or not name or any(c in name for c in '\\:*?"<>|') or any(ord(c) < 32 for c in name):
        raise ValueError('更新文件路径无效')
    parts = name.split('/')
    if any(p in {'', '.', '..'} or p.rstrip(' .') != p or p.startswith('.env') for p in parts):
        raise ValueError('更新文件路径无效')
    if any(p.lower() in PROTECTED for p in parts) or name.casefold() in {p.casefold() for p in METADATA}:
        raise ValueError('更新包不能写入配置、依赖或运行数据目录: ' + name)
    path = PurePosixPath(name)
    if any(p.split('.')[0].upper() in {'CON', 'PRN', 'AUX', 'NUL', *(f'COM{i}' for i in range(1, 10)), *(f'LPT{i}' for i in range(1, 10))} for p in parts):
        raise ValueError('更新文件路径无效')
    if path.is_absolute() or path.name.lower() in {'company.local.json', 'start-web.local.json', 'auth.json'}:
        raise ValueError('更新包不能修改本机配置: ' + name)
    if name.lower() == 'config/sessions.json' or (name.lower().startswith('config/') and 'local' in path.name.lower()) or name.lower().endswith('.credential.xml') or path.suffix.lower() in {'.dpapi', '.sqlite', '.sqlite3', '.db', '.xlsx', '.log'}:
        raise ValueError('更新包不能修改运行数据: ' + name)
    return name


def inside(root, name):
    candidate = root / name
    if not candidate.resolve().is_relative_to(root.resolve()):
        raise ValueError('更新路径超出安装目录: ' + name)
    current = root
    for part in Path(name).parts:
        current = current / part
        if current.is_symlink() or (hasattr(current, 'is_junction') and current.is_junction()):
            raise ValueError('更新路径包含目录链接: ' + name)
    return candidate


def sha(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def write_atomic(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + '.update-' + uuid.uuid4().hex)
    try:
        temporary.write_bytes(data)
        for attempt in range(6):
            try:
                os.replace(temporary, path)
                return
            except PermissionError:
                if attempt == 5:
                    raise
                time.sleep(.1 * (attempt + 1))
    finally:
        temporary.unlink(missing_ok=True)


def json_bytes(value):
    return json.dumps(value, ensure_ascii=False, indent=2).encode('utf-8')


def plan(package, target):
    package, target = package.resolve(), target.resolve()
    manifest = json.loads((package / 'update-manifest.json').read_text('utf-8'))
    inventory = json.loads((package / 'update-base.json').read_text('utf-8'))
    info = json.loads((target / 'BUILD_INFO.json').read_text('utf-8'))
    if manifest.get('schema') != SCHEMA or manifest.get('kind') != 'code-incremental':
        raise ValueError('不是受支持的增量更新包')
    if not all(isinstance(manifest.get(k), str) and COMMIT.fullmatch(manifest[k]) for k in ('base_commit', 'target_commit')):
        raise ValueError('更新版本信息无效')
    if info.get('commit') != manifest['base_commit']:
        raise ValueError('已装版本与更新包基准不一致，未修改任何程序文件')
    if manifest.get('channel') not in {'public', 'internal'} or manifest['channel'] != info.get('channel'):
        raise ValueError('更新包发行类型与已装程序不一致')
    if inventory.get('schema') != SCHEMA or inventory.get('commit') != manifest['target_commit'] or inventory.get('channel') != manifest['channel']:
        raise ValueError('后续升级基准清单无效')
    names = set()
    inventory_names = set()
    for name, value in inventory['files'].items():
        checked_name(name)
        if name.casefold() in inventory_names:
            raise ValueError('后续升级基准包含重复路径')
        inventory_names.add(name.casefold())
        if not isinstance(value, str) or not SHA256.fullmatch(value):
            raise ValueError('后续升级文件校验值无效')
    for row in manifest['files'] + manifest['deleted']:
        name = checked_name(row['path'])
        if name.casefold() in names:
            raise ValueError('更新文件清单包含重复路径')
        names.add(name.casefold())
        if name.casefold() in DEPENDENCY_FILES:
            raise ValueError('代码更新包不能修改锁定依赖')
        installed = inside(target, name)
        before = row['before_sha256']
        if before is not None and (not isinstance(before, str) or not SHA256.fullmatch(before)):
            raise ValueError('原文件校验值无效')
        if installed.exists() and not installed.is_file():
            raise ValueError('更新目标不是普通文件: ' + name)
        if installed.exists() and sha(installed) != before:
            raise ValueError('文件已在本机修改，已保留并停止更新: ' + name)
        if before is not None and not installed.is_file():
            raise ValueError('原安装文件缺失，已停止更新: ' + name)
    for row in manifest['files']:
        source = inside(package, 'payload/' + row['path'])
        if not source.is_file() or source.stat().st_size != row['bytes'] or sha(source) != row['sha256']:
            raise ValueError('更新文件损坏或不完整: ' + row['path'])
        if inventory['files'].get(row['path']) != row['sha256']:
            raise ValueError('更新文件与后续基准不一致')
        if row['path'].endswith('.py'):
            ast.parse(source.read_bytes(), filename=row['path'])
    for row in manifest['deleted']:
        if row['path'] in inventory['files']:
            raise ValueError('删除清单与后续基准不一致')
    # The next inventory must exactly describe both changed and retained source.
    for name, expected in inventory['files'].items():
        if name.casefold() not in names:
            installed = inside(target, name)
            if not installed.is_file() or sha(installed) != expected:
                raise ValueError('原安装文件与升级基准不一致: ' + name)
    return manifest, inventory, info


@contextmanager
def update_lock(target):
    path = inside(target, 'runtime/update-install.lock')
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a+b') as stream:
        stream.seek(0)
        stream.write(b'0')
        stream.flush()
        stream.seek(0)
        try:
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise ValueError('本安装已有升级正在执行') from exc
        try:
            yield
        finally:
            stream.seek(0)
            if os.name == 'nt':
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


class ConsoleServices:
    """Fence updates against live jobs; stop only this installation's web processes."""
    def __init__(self, root):
        self.root = root.resolve()
        self.web = []
        self.wrappers = []
        self.launches = {}
        self.suspended = []
        self.started = []

    def _port(self, args):
        return int(args[args.index('--port') + 1]) if '--port' in args else 8765

    def check(self):
        import psutil
        for process in psutil.process_iter(['pid', 'cmdline', 'cwd', 'create_time']):
            try:
                if process.pid == os.getpid() or Path(process.info['cwd'] or '').resolve() != self.root:
                    continue
                args = process.info['cmdline'] or []
                scripts = [inside(self.root, a) for a in args[1:]
                           if a.endswith(('.py', '.mjs', '.ps1')) and (self.root / a).resolve().is_relative_to(self.root)]
                scripts = [p for p in scripts if p.is_relative_to(self.root / 'scripts')]
                if not scripts:
                    continue
                name = scripts[0].name
                if name == 'web_console.py':
                    self.web.append(process)
                    # A Windows venv child reports the base Python executable.
                    # Re-enter through this installation's venv to retain its dependencies.
                    python = self.root / ('.venv/Scripts/python.exe' if os.name == 'nt' else '.venv/bin/python')
                    self.launches.setdefault(self._port(args), [str(python), *args[1:]])
                elif name == 'persistent_web_service.py':
                    self.wrappers.append(process)
                else:
                    raise ValueError('本安装仍有后台任务运行，请先在工作台结束任务再升级；采集数据和断点会保留')
            except (psutil.NoSuchProcess, psutil.ZombieProcess):
                continue
            except psutil.AccessDenied as exc:
                raise ValueError('无法确认本安装的后台任务状态，未开始升级') from exc
        opener = build_opener(ProxyHandler({}))
        for port in self.launches:
            with opener.open(f'http://127.0.0.1:{port}/api/status', timeout=10) as response:
                state = json.load(response)
            if state.get('console_busy') or state.get('market_busy') or state.get('generation', {}).get('busy') or state.get('stage3_control', {}).get('running'):
                raise ValueError('工作台正在准备或分析任务，请完成后再升级')

    def stop(self):
        import psutil
        for process in self.wrappers:
            process.suspend()
            self.suspended.append(process)
        for process in sorted(self.web, key=lambda p: len(p.parents()), reverse=True):
            try:
                process.terminate()
                process.wait(timeout=15)
            except psutil.NoSuchProcess:
                pass

    def restore(self):
        import psutil
        for process in self.suspended:
            try:
                process.resume()
            except psutil.NoSuchProcess:
                pass
        resumed = bool(self.suspended)
        self.suspended.clear()
        if not resumed:
            for args in self.launches.values():
                log = self.root / 'runtime/update-web-restart.log'
                with log.open('ab') as stream:
                    self.started.append(subprocess.Popen(args, cwd=self.root, stdout=stream, stderr=subprocess.STDOUT,
                                        creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0))
        opener = build_opener(ProxyHandler({}))
        for port in self.launches:
            deadline = time.monotonic() + 30
            while True:
                if self.started and all(process.poll() is not None for process in self.started):
                    raise RuntimeError('网页服务启动失败')
                try:
                    with opener.open(f'http://127.0.0.1:{port}/api/status', timeout=2) as response:
                        if json.load(response).get('ok') is True:
                            break
                except (OSError, ValueError):
                    pass
                if time.monotonic() >= deadline:
                    raise RuntimeError('网页服务未能重新连接')
                time.sleep(.25)

    def stop_started(self):
        """Stop only newly launched web processes before restoring the old source."""
        import psutil
        for child in self.started:
            if child.poll() is not None:
                continue
            try:
                parent = psutil.Process(child.pid)
                for process in [*reversed(parent.children(recursive=True)), parent]:
                    args = process.cmdline()
                    if Path(process.cwd()).resolve() == self.root and any(
                        arg.endswith('web_console.py') and (self.root / arg).resolve() == self.root / 'scripts/web_console.py'
                        for arg in args[1:]
                    ):
                        process.terminate()
                        process.wait(timeout=5)
            except psutil.NoSuchProcess:
                pass
        self.started.clear()


def apply(package, target, *, services=None):
    package, target = Path(package).resolve(), Path(target).resolve()
    with update_lock(target):
        manifest, inventory, info = plan(package, target)
        services = services or ConsoleServices(target)
        services.check()
        identifier = datetime.now().strftime('%Y%m%d-%H%M%S') + '-' + uuid.uuid4().hex[:8]
        backup = inside(target, '.deployment-backup/updates/' + identifier)
        backup.mkdir(parents=True)
        names = [row['path'] for row in manifest['files'] + manifest['deleted']] + ['BUILD_INFO.json', 'UPDATE_BASE.json']
        existing = set()
        for name in names:
            source = inside(target, name)
            if source.is_file():
                destination = inside(backup, name)
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, destination)
                existing.add(name)
        journal = {'base_commit': manifest['base_commit'], 'target_commit': manifest['target_commit'],
                   'state': 'prepared', 'files': names, 'existed': sorted(existing)}
        write_atomic(backup / 'transaction.json', json_bytes(journal))
        touched = []
        try:
            services.stop()
            plan(package, target)
            for row in manifest['files']:
                name = row['path']
                data = inside(package, 'payload/' + name).read_bytes()
                if hashlib.sha256(data).hexdigest() != row['sha256']:
                    raise ValueError('更新文件在校验后发生变化: ' + name)
                touched.append(name)
                write_atomic(inside(target, name), data)
            for row in manifest['deleted']:
                touched.append(row['path'])
                inside(target, row['path']).unlink()
            touched.extend(['BUILD_INFO.json', 'UPDATE_BASE.json'])
            info.update(commit=manifest['target_commit'], updated_from_commit=manifest['base_commit'],
                        updated_at_utc=datetime.now(timezone.utc).isoformat(), distribution_kind='incremental-updated')
            write_atomic(target / 'UPDATE_BASE.json', json_bytes(inventory))
            write_atomic(target / 'BUILD_INFO.json', json_bytes(info))
            services.restore()
            journal['state'] = 'completed'
            write_atomic(backup / 'transaction.json', json_bytes(journal))
        except BaseException:
            errors = []
            stop_started = getattr(services, 'stop_started', None)
            if stop_started:
                try:
                    stop_started()
                except Exception:
                    errors.append('web-service-stop')
            for name in reversed(touched):
                try:
                    path = inside(target, name)
                    if name in existing:
                        write_atomic(path, inside(backup, name).read_bytes())
                    else:
                        path.unlink(missing_ok=True)
                except OSError:
                    errors.append(name)
            journal['state'] = 'rollback_incomplete' if errors else 'rolled_back'
            journal['restore_errors'] = errors
            write_atomic(backup / 'transaction.json', json_bytes(journal))
            try:
                services.restore()
            except Exception:
                pass
            if errors:
                raise RuntimeError('自动回滚未完成，原文件备份保留在: ' + str(backup))
            raise
        return {'ok': True, 'base_commit': manifest['base_commit'], 'commit': manifest['target_commit'],
                'changed_files': len(manifest['files']), 'deleted_files': len(manifest['deleted']),
                'backup': str(backup), 'configuration_and_data_preserved': True, 'dependencies_reinstalled': False}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--package-root', type=Path, required=True)
    parser.add_argument('--install-root', type=Path, required=True)
    parser.add_argument('--check', action='store_true')
    args = parser.parse_args()
    try:
        if args.check:
            manifest, _, _ = plan(args.package_root, args.install_root)
            ConsoleServices(args.install_root).check()
            print(json.dumps({'ok': True, 'check_only': True, 'target_commit': manifest['target_commit']}, ensure_ascii=False))
        else:
            result = apply(args.package_root, args.install_root)
            print('升级完成，配置、数据、断点和运行环境已保留。')
            print(json.dumps(result, ensure_ascii=False))
    except Exception as exc:
        print('升级未完成: ' + str(exc), file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
