import ctypes
import hashlib
import json
import os
from pathlib import Path
import subprocess
from types import SimpleNamespace

import pytest
from selection_automation import driver_retirement as drivers


@pytest.fixture
def owned(tmp_path, monkeypatch):
    folder = tmp_path / 'runtime/worker-01-new'
    folder.mkdir(parents=True)
    executable = tmp_path / 'vendor/geckodriver.exe'
    executable.parent.mkdir()
    executable.write_bytes(b'offline driver identity, never executed')
    worker = {'name': 'worker-01-new', 'profile_id': 'owned', 'webdriver_url': 'http://127.0.0.1:4451'}
    record = dict(pid=123, started_ticks=638000000000000123, profile_id='owned',
                  port=4451, marionette_port=58549, browser_pid=7, browser_started_ticks=638000000000000001)
    binding = dict(phase='BOUND', geckodriver_pid=123, geckodriver_started_ticks=record['started_ticks'],
                   profile_id='owned', webdriver_port=4451, marionette_port=58549,
                   browser_pid=7, browser_started_ticks=record['browser_started_ticks'])
    calls = []
    clock = [0.0]
    monkeypatch.setattr(drivers, 'time', SimpleNamespace(monotonic=lambda: clock[0],
        sleep=lambda seconds: clock.__setitem__(0, clock[0] + seconds)))
    class Process:
        gone = False
        ticks = record['started_ticks']
        exe = str(executable)
        args = [str(executable), '--connect-existing', '--marionette-port', '58549', '--port', '4451', '--log', 'warn']
        listeners = {4451: [123], 58549: []}
        fail = False
        def __init__(self, pid):
            assert pid == 123
            calls.append('open_exact_pid')
        def exited(self): return self.gone
        def identity(self): return self.ticks, self.exe, self.args
        def listener_pids(self, port): return self.listeners[port]
        def terminate_and_wait(self):
            if self.fail: raise drivers.DriverCleanupRequired('驱动尚未退出')
            calls.append('terminate_verified_handle')
        def close(self): calls.append('close_handle')
    monkeypatch.setattr(drivers, '_WindowsProcess', Process)
    def write():
        (folder / 'driver.json').write_text(json.dumps(record), encoding='utf-8')
        (folder / 'binding.json').write_text(json.dumps(binding), encoding='utf-8')
    write()
    return SimpleNamespace(root=tmp_path, folder=folder, executable=executable, worker=worker,
                           record=record, binding=binding, process=Process, calls=calls, write=write, clock=clock)


@pytest.mark.parametrize('modern', [False, True])
@pytest.mark.parametrize('listening', [False, True])
def test_exact_legacy_and_new_identity_release_only_recorded_driver(owned, modern, listening):
    o = owned
    if modern:
        o.record.update(installation_root=str(o.root), exe_path=str(o.executable),
                        exe_sha256=hashlib.sha256(o.executable.read_bytes()).hexdigest())
        o.write()
    o.process.listeners = {4451: [123] if listening else [], 58549: []}
    before = {p.name: p.read_bytes() for p in o.folder.iterdir()}
    result = drivers.release_retired_driver(o.root, o.worker, 'owned')
    assert result['status'] == 'RELEASED'
    assert o.calls == ['open_exact_pid', 'terminate_verified_handle', 'close_handle']
    assert before == {p.name: p.read_bytes() for p in o.folder.iterdir()}


@pytest.mark.parametrize('conflict', [
    'profile', 'pid_reuse_one_tick', 'missing_binding', 'binding_profile', 'binding_pid',
    'binding_start', 'binding_marionette', 'binding_webdriver', 'binding_browser_pid',
    'binding_browser_start', 'missing_browser_proof', 'foreign_executable', 'relative_executable',
    'command_executable', 'command_marionette', 'command_webdriver', 'duplicate_command_port',
    'unexpected_command', 'foreign_listener', 'unknown_listener', 'browser_still_active',
    'foreign_installation', 'record_executable', 'record_hash', 'partial_metadata',
    'manifest_port', 'manifest_nonlocal', 'invalid_port', 'worker_traversal', 'binding_changed',
])
def test_ambiguous_identity_never_terminates_and_requires_maintenance(owned, conflict):
    o = owned
    if conflict == 'profile': o.record['profile_id'] = 'foreign'
    elif conflict == 'pid_reuse_one_tick': o.process.ticks += 1
    elif conflict.startswith('binding_') and conflict != 'binding_changed':
        field = {'binding_profile': 'profile_id', 'binding_pid': 'geckodriver_pid',
                 'binding_start': 'geckodriver_started_ticks', 'binding_marionette': 'marionette_port',
                 'binding_webdriver': 'webdriver_port', 'binding_browser_pid': 'browser_pid',
                 'binding_browser_start': 'browser_started_ticks'}[conflict]
        o.binding[field] = 'foreign' if field == 'profile_id' else o.binding[field] + 1
    elif conflict == 'missing_browser_proof': o.record.pop('browser_started_ticks')
    elif conflict == 'foreign_executable': o.process.exe = str(o.root / 'other.exe')
    elif conflict == 'relative_executable': o.process.exe = 'geckodriver.exe'
    elif conflict.startswith('command_'):
        args = list(o.process.args)
        args[{'command_executable': 0, 'command_marionette': 3, 'command_webdriver': 5}[conflict]] = 'other'
        o.process.args = args
    elif conflict == 'duplicate_command_port': o.process.args = o.process.args + ['--port', '4451']
    elif conflict == 'unexpected_command': o.process.args = o.process.args + ['--other']
    elif conflict == 'foreign_listener': o.process.listeners = {4451: [123, 999], 58549: []}
    elif conflict == 'unknown_listener': o.process.listeners = {4451: [None], 58549: []}
    elif conflict == 'browser_still_active': o.process.listeners = {4451: [123], 58549: [7]}
    elif conflict in {'foreign_installation', 'record_executable', 'record_hash', 'partial_metadata'}:
        o.record.update(installation_root=str(o.root), exe_path=str(o.executable),
                        exe_sha256=hashlib.sha256(o.executable.read_bytes()).hexdigest())
        if conflict == 'foreign_installation': o.record['installation_root'] = str(o.root / 'other-install')
        if conflict == 'record_executable': o.record['exe_path'] = str(o.root / 'other/geckodriver.exe')
        if conflict == 'record_hash': o.record['exe_sha256'] = '0' * 64
        if conflict == 'partial_metadata': o.record.pop('exe_sha256')
    elif conflict == 'manifest_port': o.worker['webdriver_url'] = 'http://127.0.0.1:4452'
    elif conflict == 'manifest_nonlocal': o.worker['webdriver_url'] = 'http://example.invalid:4451'
    elif conflict == 'invalid_port': o.record['marionette_port'] = 70000
    elif conflict == 'worker_traversal': o.worker['name'] = '../other-install'
    elif conflict == 'binding_changed':
        original = o.process.listener_pids
        def changed(self, port):
            (o.folder / 'binding.json').write_text('{}')
            return original(self, port)
        o.process.listener_pids = changed
    o.write()
    if conflict == 'missing_binding': (o.folder / 'binding.json').unlink()
    with pytest.raises(drivers.DriverCleanupRequired, match='请维护后重试清理'):
        drivers.release_retired_driver(o.root, o.worker, 'owned')
    assert 'terminate_verified_handle' not in o.calls
    if o.calls: assert o.calls[-1] == 'close_handle'


def test_missing_record_is_only_harmless_without_a_binding(owned):
    o = owned
    (o.folder / 'driver.json').unlink()
    with pytest.raises(drivers.DriverCleanupRequired):
        drivers.release_retired_driver(o.root, o.worker, 'owned')
    (o.folder / 'binding.json').unlink()
    assert drivers.release_retired_driver(o.root, o.worker, 'owned') == {'status': 'NOT_RECORDED'}
    assert not o.calls


@pytest.mark.parametrize('not_running', ['gone', 'exited'])
def test_original_process_already_gone_needs_no_termination(owned, monkeypatch, not_running):
    o = owned
    if not_running == 'gone':
        def gone(pid): raise ProcessLookupError(pid)
        monkeypatch.setattr(drivers, '_WindowsProcess', gone)
    else: o.process.gone = True
    (o.folder / 'binding.json').unlink()
    assert drivers.release_retired_driver(o.root, o.worker, 'owned')['status'] == 'ALREADY_EXITED'
    assert 'terminate_verified_handle' not in o.calls


def test_driver_wait_failure_is_visible_and_preserves_identity(owned):
    o = owned
    o.process.fail = True
    with pytest.raises(drivers.DriverCleanupRequired, match='驱动尚未退出'):
        drivers.release_retired_driver(o.root, o.worker, 'owned')
    assert (o.folder / 'driver.json').exists() and (o.folder / 'binding.json').exists()
    assert o.calls[-1] == 'close_handle'


def test_acknowledged_profile_deletion_waits_passively_for_browser_listener(owned, monkeypatch):
    o = owned
    observations = iter([[7], [7], []])
    waits = []
    def listeners(self, port):
        return next(observations) if port == 58549 else [123]
    o.process.listener_pids = listeners
    monkeypatch.setattr(drivers, 'time', SimpleNamespace(monotonic=lambda: 0.0,
                                                      sleep=lambda seconds: waits.append(seconds)), raising=False)
    assert drivers.release_retired_driver(o.root, o.worker, 'owned')['status'] == 'RELEASED'
    assert len(waits) == 2
    assert o.calls.count('terminate_verified_handle') == 1


@pytest.mark.parametrize('changed', ['driver_identity', 'driver_record', 'binding_record', 'foreign_driver_listener'])
def test_passive_wait_revalidates_identity_records_and_port_before_termination(owned, changed):
    o = owned
    count = [0]
    def listeners(self, port):
        if port == 4451:
            return [999] if changed == 'foreign_driver_listener' and count[0] > 1 else [123]
        count[0] += 1
        if count[0] == 1: return [7]
        if changed == 'driver_identity': o.process.ticks += 1
        elif changed == 'driver_record': (o.folder / 'driver.json').write_text('{}')
        elif changed == 'binding_record': (o.folder / 'binding.json').write_text('{}')
        return []
    o.process.listener_pids = listeners
    with pytest.raises(drivers.DriverCleanupRequired):
        drivers.release_retired_driver(o.root, o.worker, 'owned')
    assert 'terminate_verified_handle' not in o.calls
    assert o.calls[-1] == 'close_handle'


def test_browser_socket_wait_is_bounded_and_never_controls_listener_process(owned):
    o = owned
    o.process.listeners = {4451: [123], 58549: [999]}
    with pytest.raises(drivers.DriverCleanupRequired, match='浏览器端口仍在使用'):
        drivers.release_retired_driver(o.root, o.worker, 'owned')
    assert 5 <= o.clock[0] < 5.2
    assert o.calls == ['open_exact_pid', 'close_handle']


def test_native_backend_uses_exact_integer_ticks_and_same_handle_for_termination(monkeypatch, tmp_path):
    from ctypes import wintypes
    import psutil
    low, high = 0x12345679, 0x01DA0001
    calls = []
    class API:
        def GetProcessTimes(self, handle, created, *others):
            assert handle == 987654
            created._obj.dwLowDateTime, created._obj.dwHighDateTime = low, high
            return True
        def QueryFullProcessImageNameW(self, handle, flags, name, size):
            assert handle == 987654
            name.value = str(tmp_path / 'geckodriver.exe')
            return True
        def WaitForSingleObject(self, handle, timeout):
            calls.append(('wait', handle, timeout))
            return 0 if timeout else 258
        def TerminateProcess(self, handle, code):
            calls.append(('terminate', handle, code))
            return True
        def CloseHandle(self, handle): calls.append(('close', handle))
    backend = object.__new__(drivers._WindowsProcess)
    backend.ctypes, backend.wintypes, backend.api = ctypes, wintypes, API()
    backend.handle, backend.pid = 987654, 123
    monkeypatch.setattr(psutil, 'Process', lambda pid: SimpleNamespace(cmdline=lambda: ['offline']))
    ticks, exe, args = backend.identity()
    assert ticks == (high << 32) + low + 504911232000000000
    assert args == ['offline']
    backend.terminate_and_wait()
    backend.close()
    assert ('terminate', 987654, 0) in calls and ('wait', 987654, 5000) in calls
    assert calls[-1] == ('close', 987654) and backend.handle is None


@pytest.mark.skipif(os.name != 'nt', reason='Windows exact FILETIME/.NET identity contract')
def test_native_identity_matches_readonly_dotnet_ticks_for_this_test_process():
    import sys
    process = drivers._WindowsProcess(os.getpid())
    try:
        ticks, executable, command = process.identity()
        result = subprocess.run(['powershell.exe', '-NoProfile', '-Command',
            f'(Get-Process -Id {os.getpid()}).StartTime.ToUniversalTime().Ticks'], capture_output=True, timeout=15)
        assert result.returncode == 0, result.stderr
        assert ticks == int(result.stdout.strip())
        assert Path(executable).resolve() == Path(sys.executable).resolve()
        assert command
    finally:
        process.close()
