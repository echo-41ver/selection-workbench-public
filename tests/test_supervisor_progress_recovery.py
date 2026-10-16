import ctypes
from ctypes import wintypes
import json
import os
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import company_setup
import selection


@pytest.mark.skipif(os.name != 'nt', reason='Windows destination sharing lock')
def test_real_windows_reader_lock_does_not_abort_progress_loop(tmp_path, capsys):
    target = tmp_path / 'status.json'
    selection.save(target, {'count': 10})
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                                  wintypes.LPVOID, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    kernel.CreateFileW.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = kernel.CreateFileW(str(target), 0x80000000, 3, None, 3, 0x80, None)
    assert handle != wintypes.HANDLE(-1).value
    try:
        assert selection.save_progress(target, {'count': 11}) is False
        assert json.loads(target.read_text()) == {'count': 10}
    finally:
        kernel.CloseHandle(handle)
    assert '保留采集进程，下轮重试' in capsys.readouterr().out
    assert selection.save_progress(target, {'count': 12}) is True
    assert json.loads(target.read_text()) == {'count': 12}
    assert not list(tmp_path.glob('*.tmp'))


def test_non_lock_write_errors_still_raise(tmp_path, monkeypatch):
    def full(*args):
        raise OSError(28, 'disk full')
    monkeypatch.setattr(selection, 'save', full)
    with pytest.raises(OSError, match='disk full'):
        selection.save_progress(tmp_path / 'status.json', {})


def test_resume_keeps_completed_lanes_retired_and_task_identity_strict(tmp_path):
    workers = [{'name': f'worker-{n:02d}-new', 'profile_id': f'existing-{n}'} for n in range(1, 5)]
    manifest = {'machine_id': 'local', 'task_key': 'headphones', 'used': True, 'workers': workers}
    base = tmp_path / 'runs/headphones'
    selection.save(base / 'temu/keyword_plan.json', {'finalized': True})
    for index, worker in enumerate(workers):
        selection.save(base / f"temu/supervisors/{worker['name']}.json",
                       {'status': 'WORKER_COMPLETE' if index in (0, 3) else 'RUNNING'})
    assert company_setup.resume_workers(tmp_path, 'headphones', manifest, 'local', 4) == workers[1:3]
    with pytest.raises(RuntimeError):
        company_setup.resume_workers(tmp_path, 'different_task', manifest, 'local', 4)
    with pytest.raises(RuntimeError):
        company_setup.resume_workers(tmp_path, 'headphones', manifest, 'other_machine', 4)
    selection.save(base / 'temu/supervisors/assignments.json',
                   {'jobs': [{'owner': workers[0]['name'], 'started': True, 'complete': False}]})
    assert company_setup.resume_workers(tmp_path, 'headphones', manifest, 'local', 4) == workers[:3]
