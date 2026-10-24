"""Real temporary files/processes; no business or paid requests are made."""
import ctypes
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import tracemalloc

import pytest

from selection_automation.commercial import jsonl
from selection_workflow.incremental_stage12 import replace_snapshot_file


ROOT = Path(__file__).resolve().parents[1]
READER = r'''
import json,sys,time
from pathlib import Path
sys.path.insert(0,sys.argv[1])
from selection_automation.commercial import jsonl
source,ready,release=map(Path,sys.argv[2:])
records=jsonl(source)
try:
    first=next(records)
    ready.write_text(json.dumps(first),encoding='utf-8')
    deadline=time.monotonic()+20
    while not release.exists():
        if time.monotonic()>=deadline:raise TimeoutError('test release not received')
        time.sleep(.01)
    print(json.dumps([first,*records]),flush=True)
finally:
    records.close()
'''


@pytest.mark.skipif(os.name != 'nt', reason='Windows file sharing regression')
def test_commercial_long_reader_allows_atomic_snapshot_replacement(tmp_path):
    source = tmp_path/'candidates.jsonl'
    source.write_text('{"id":"old-first"}\n{"id":"old-second"}\n', encoding='utf-8')
    replacement = tmp_path/'next.jsonl'
    replacement.write_text('{"id":"new"}\n', encoding='utf-8')
    ready, release = tmp_path/'ready.json', tmp_path/'release'
    child = subprocess.Popen([sys.executable, '-B', '-c', READER, str(ROOT/'src'), str(source), str(ready), str(release)],
        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, encoding='utf-8', creationflags=subprocess.CREATE_NO_WINDOW)
    try:
        deadline = time.monotonic()+10
        while not ready.exists() and child.poll() is None and time.monotonic() < deadline:
            time.sleep(.01)
        assert ready.exists(), 'Reader did not reach its first yielded record'
        assert json.loads(ready.read_text('utf-8')) == {'id':'old-first'}
        # Outlast the existing production writer's 2s retry window. The reader
        # is a separate process, held inside the real application's generator.
        time.sleep(2.1)
        replace_snapshot_file(replacement, source)
        assert child.poll() is None, 'Replacement must succeed before releasing the reader'
        assert list(jsonl(source)) == [{'id':'new'}]
    finally:
        release.write_text('release reader', encoding='utf-8')
        stdout, stderr = child.communicate(timeout=10)
        assert child.returncode == 0, stderr
    assert json.loads(stdout) == [{'id':'old-first'}, {'id':'old-second'}]


def test_commercial_jsonl_keeps_lazy_parsing_and_strict_corruption(tmp_path):
    source = tmp_path/'candidates.jsonl'
    source.write_text('{"id":"first"}\n\nnot valid JSON', encoding='utf-8')
    records = jsonl(source)
    assert next(records) == {'id':'first'}
    with pytest.raises(json.JSONDecodeError):
        next(records)
    assert list(jsonl(tmp_path/'missing.jsonl')) == []


def exclusive_reader(path):
    """An actual exclusive Windows open detects any leaked shared reader."""
    from ctypes import wintypes
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
        wintypes.LPVOID, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    kernel.CreateFileW.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.CloseHandle.restype = wintypes.BOOL
    handle = kernel.CreateFileW(str(path), 0x80000000, 0, None, 3, 0x80, None)
    if handle == ctypes.c_void_p(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    return kernel, handle


@pytest.mark.skipif(os.name != 'nt', reason='Windows exclusive open detects handle leaks')
@pytest.mark.parametrize('ending', ['exhaust', 'early_close', 'parse_error'])
def test_commercial_reader_releases_handle_on_every_exit(tmp_path, ending, monkeypatch):
    from selection_automation import snapshot_io
    snapshots=[]
    create=snapshot_io.tempfile.TemporaryFile
    def capture(*args, **kwargs):
        value=create(*args, **kwargs)
        snapshots.append(value)
        return value
    monkeypatch.setattr(snapshot_io.tempfile, 'TemporaryFile', capture)
    source = tmp_path/'candidates.jsonl'
    source.write_text('{"id":"first"}\n'+('broken\n' if ending == 'parse_error' else '{"id":"second"}\n'), encoding='utf-8')
    records = jsonl(source)
    assert next(records) == {'id':'first'}
    if ending == 'exhaust':
        assert list(records) == [{'id':'second'}]
    elif ending == 'early_close':
        records.close()
    else:
        with pytest.raises(json.JSONDecodeError):
            next(records)
    kernel, handle = exclusive_reader(source)
    assert kernel.CloseHandle(handle)
    assert snapshots and all(stream.closed for stream in snapshots)


def test_snapshot_read_memory_stays_bounded(tmp_path):
    from selection_automation.snapshot_io import open_snapshot_text
    source=tmp_path/'large-candidates.jsonl'
    line='{"id":"first","text":"'+('x'*1024)+'"}\n'
    with source.open('w',encoding='utf-8') as stream:
        for _ in range(24576):
            stream.write(line)
    tracemalloc.start()
    try:
        with open_snapshot_text(source) as stream:
            assert stream.readline()==line
            _,peak=tracemalloc.get_traced_memory()
        # The 24+ MiB source is copied in 1 MiB blocks on Windows, never read
        # into one full bytes/string/list object. Parsing remains lazy too.
        assert peak < 8*1024*1024
    finally:
        tracemalloc.stop()


@pytest.mark.skipif(os.name != 'nt', reason='Windows exclusive lock regression')
def test_exclusive_external_lock_still_raises_and_preserves_last_good_snapshot(tmp_path):
    source, replacement = tmp_path/'candidates.jsonl', tmp_path/'next.jsonl'
    source.write_text('{"id":"old"}\n', encoding='utf-8')
    replacement.write_text('{"id":"new"}\n', encoding='utf-8')
    kernel, handle = exclusive_reader(source)
    try:
        with pytest.raises(PermissionError):
            replace_snapshot_file(replacement, source, timeout_seconds=.05)
    finally:
        assert kernel.CloseHandle(handle)
    assert list(jsonl(source)) == [{'id':'old'}]
    assert replacement.read_text('utf-8') == '{"id":"new"}\n'
