"""Release one recorded driver only after its owning profile has been retired.

Callers must first enforce task/machine ownership and delete (or confirm the
absence of) the profile. This module never discovers processes by name or cwd.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import time
from urllib.parse import urlsplit


class DriverCleanupRequired(RuntimeError):
    def __init__(self, reason):
        self.reason = reason
        super().__init__(f'浏览器环境已退役，但驱动清理未完成（{reason}）；请维护后重试清理，采集断点已保留')


def _integer(value):
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def _path(value):
    return os.path.normcase(str(Path(value).resolve()))


def _read(path):
    raw = path.read_bytes()
    value = json.loads(raw.decode('utf-8-sig'))
    if not isinstance(value, dict):
        raise DriverCleanupRequired('身份记录格式不完整')
    return value, hashlib.sha256(raw).hexdigest()


class _WindowsProcess:
    """Keep the verified kernel handle through termination, fencing PID reuse."""
    def __init__(self, pid):
        import ctypes
        from ctypes import wintypes
        if os.name != 'nt':
            raise DriverCleanupRequired('当前系统不支持精确驱动身份核验')
        self.ctypes, self.wintypes = ctypes, wintypes
        self.api = ctypes.WinDLL('kernel32', use_last_error=True)
        signatures = {
            'OpenProcess': ([wintypes.DWORD, wintypes.BOOL, wintypes.DWORD], wintypes.HANDLE),
            'CloseHandle': ([wintypes.HANDLE], wintypes.BOOL),
            'GetProcessTimes': ([wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4, wintypes.BOOL),
            'QueryFullProcessImageNameW': ([wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)], wintypes.BOOL),
            'WaitForSingleObject': ([wintypes.HANDLE, wintypes.DWORD], wintypes.DWORD),
            'TerminateProcess': ([wintypes.HANDLE, wintypes.UINT], wintypes.BOOL),
        }
        for name, (args, result) in signatures.items():
            function = getattr(self.api, name)
            function.argtypes, function.restype = args, result
        self.handle = self.api.OpenProcess(0x1000 | 0x0001 | 0x00100000, False, pid)
        if not self.handle:
            if ctypes.get_last_error() == 87:
                raise ProcessLookupError(pid)
            raise DriverCleanupRequired('无法读取驱动进程身份')
        self.pid = pid

    def close(self):
        if self.handle:
            self.api.CloseHandle(self.handle)
            self.handle = None

    def exited(self):
        result = self.api.WaitForSingleObject(self.handle, 0)
        if result not in (0, 258):
            raise DriverCleanupRequired('无法确认驱动退出状态')
        return result == 0

    def identity(self):
        c, w = self.ctypes, self.wintypes
        times = [w.FILETIME() for _ in range(4)]
        if not self.api.GetProcessTimes(self.handle, *(c.byref(t) for t in times)):
            raise DriverCleanupRequired('无法读取驱动启动时间')
        # Windows FILETIME starts in 1601; .NET UTC ticks start in year 1.
        ticks = (times[0].dwHighDateTime << 32) + times[0].dwLowDateTime + 504911232000000000
        size = w.DWORD(32768)
        name = c.create_unicode_buffer(size.value)
        if not self.api.QueryFullProcessImageNameW(self.handle, 0, name, c.byref(size)):
            raise DriverCleanupRequired('无法读取驱动完整程序路径')
        import psutil
        return ticks, name.value, psutil.Process(self.pid).cmdline()

    def listener_pids(self, port):
        import psutil
        return [item.pid for item in psutil.net_connections(kind='tcp')
                if item.status == psutil.CONN_LISTEN and item.laddr.port == port]

    def terminate_and_wait(self):
        if self.exited():
            return
        if not self.api.TerminateProcess(self.handle, 0):
            if self.exited():
                return
            raise DriverCleanupRequired('驱动退出请求未完成')
        if self.api.WaitForSingleObject(self.handle, 5000) != 0:
            raise DriverCleanupRequired('驱动尚未退出')


def _validate_process_identity(process, record, root):
    ticks, executable, command = process.identity()
    if ticks != record['started_ticks']:
        raise DriverCleanupRequired('驱动进程编号已重用或启动时间不符')
    if not Path(executable).is_absolute() or Path(executable).name.lower() != 'geckodriver.exe':
        raise DriverCleanupRequired('驱动完整程序身份不符')
    if not command or _path(command[0]) != _path(executable) or command[1:] != [
            '--connect-existing', '--marionette-port', str(record['marionette_port']),
            '--port', str(record['port']), '--log', 'warn']:
        raise DriverCleanupRequired('驱动命令与绑定双端口不一致')
    metadata = ('installation_root', 'exe_path', 'exe_sha256')
    if any(key in record for key in metadata):
        if (not all(isinstance(record.get(key), str) and record[key] for key in metadata) or
                _path(record['installation_root']) != _path(root) or
                _path(record['exe_path']) != _path(executable) or
                not re.fullmatch('[0-9a-f]{64}', record['exe_sha256']) or
                hashlib.sha256(Path(executable).read_bytes()).hexdigest() != record['exe_sha256']):
            raise DriverCleanupRequired('驱动程序或安装绑定已变化')


def release_retired_driver(root, worker, profile_id):
    """Release a retired lane's exact driver; insufficient identity is retryable.

    A legacy record must still agree with the independently written binding on
    profile, PID, exact ticks, browser ownership and both ports. No fallback
    terminates a process using only its executable name or installation cwd.
    """
    process = None
    try:
        root = Path(root).resolve()
        name = worker.get('name')
        if not isinstance(name, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,127}', name):
            raise DriverCleanupRequired('缺少有效的采集路身份')
        folder = (root / 'runtime' / name).resolve()
        if not folder.is_relative_to((root / 'runtime').resolve()):
            raise DriverCleanupRequired('驱动记录不属于当前安装')
        record_path, binding_path = folder / 'driver.json', folder / 'binding.json'
        if not record_path.exists():
            if binding_path.exists():
                raise DriverCleanupRequired('缺少既有绑定的驱动身份记录')
            return {'status': 'NOT_RECORDED'}
        record, record_sha = _read(record_path)
        if str(record.get('profile_id') or '') != str(profile_id):
            raise DriverCleanupRequired('驱动环境与退休环境不一致')
        if not _integer(record.get('pid')):
            raise DriverCleanupRequired('缺少驱动进程编号')
        try:
            process = _WindowsProcess(record['pid'])
        except ProcessLookupError:
            return {'status': 'ALREADY_EXITED', 'driver_record_sha256': record_sha}
        if process.exited():
            return {'status': 'ALREADY_EXITED', 'driver_record_sha256': record_sha}
        binding, binding_sha = _read(binding_path)
        url = urlsplit(str(worker.get('webdriver_url') or ''))
        if (url.scheme != 'http' or url.hostname not in {'127.0.0.1', '::1'} or
                url.username or url.password or url.path not in {'', '/'} or url.query or url.fragment or
                not url.port or url.port != record.get('port')):
            raise DriverCleanupRequired('采集路端口与驱动记录不一致')
        for field in ('port', 'marionette_port'):
            if not _integer(record.get(field)) or record[field] > 65535:
                raise DriverCleanupRequired('驱动端口记录不完整')
        if record['port'] == record['marionette_port']:
            raise DriverCleanupRequired('驱动双端口记录冲突')
        pairs = {'profile_id': 'profile_id', 'pid': 'geckodriver_pid',
                 'started_ticks': 'geckodriver_started_ticks', 'port': 'webdriver_port',
                 'marionette_port': 'marionette_port', 'browser_pid': 'browser_pid',
                 'browser_started_ticks': 'browser_started_ticks'}
        for field, other in pairs.items():
            if field != 'profile_id' and not _integer(record.get(field)):
                raise DriverCleanupRequired('驱动身份记录不完整')
            if binding.get(other) != record[field]:
                raise DriverCleanupRequired('驱动与既有绑定身份不一致')
        _validate_process_identity(process, record, root)
        if any(pid != record['pid'] for pid in process.listener_pids(record['port'])):
            raise DriverCleanupRequired('驱动端口存在其他进程')
        # AdsPower's successful delete does not prove asynchronous socket close.
        # Only wait passively; never stop or otherwise control a listener PID.
        deadline = time.monotonic() + 5
        while process.listener_pids(record['marionette_port']):
            if time.monotonic() >= deadline:
                raise DriverCleanupRequired('浏览器端口仍在使用，稍后重试清理')
            time.sleep(.1)
        if _read(record_path)[1] != record_sha or _read(binding_path)[1] != binding_sha:
            raise DriverCleanupRequired('驱动绑定在清理期间变化')
        if process.exited():
            return {'status': 'ALREADY_EXITED', 'driver_record_sha256': record_sha}
        _validate_process_identity(process, record, root)
        if any(pid != record['pid'] for pid in process.listener_pids(record['port'])):
            raise DriverCleanupRequired('驱动端口存在其他进程')
        process.terminate_and_wait()
        return {'status': 'RELEASED', 'driver_record_sha256': record_sha,
                'binding_record_sha256': binding_sha}
    except DriverCleanupRequired:
        raise
    except Exception as error:
        # Do not persist arbitrary process command lines or provider exceptions.
        raise DriverCleanupRequired('驱动身份暂无法核验') from error
    finally:
        if process is not None:
            process.close()
