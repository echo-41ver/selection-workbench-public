"""Real Windows regressions for inherited daemon stdout handles; local only."""
from __future__ import annotations

import importlib.util
import io
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import psutil
import pytest

from selection_automation.background_launch import run_background_launcher

ROOT = Path(__file__).resolve().parents[1]


def launcher(tmp_path: Path, *, keep_parent_alive: bool) -> tuple[list[str], Path]:
    owned_pid = tmp_path / "owned-child.json"
    script = tmp_path / "spawn_daemon.py"
    script.write_text("""
import json, os, pathlib, subprocess, sys, time
child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'],
    stdin=subprocess.DEVNULL, stdout=sys.stdout, stderr=sys.stderr, close_fds=False)
pathlib.Path(sys.argv[1]).write_text(json.dumps({'pid':child.pid}), encoding='utf-8')
print(json.dumps({'status':'REPLACED','worker':1,'child_pid':child.pid}), flush=True)
print('launcher diagnostics', file=sys.stderr, flush=True)
if sys.argv[2] == '1': time.sleep(30)
""", encoding="utf-8")
    return [sys.executable, str(script), str(owned_pid), "1" if keep_parent_alive else "0"], owned_pid


def stop_owned_fixture_child(pid_file: Path) -> None:
    if not pid_file.exists():
        return
    pid = json.loads(pid_file.read_text("utf-8"))["pid"]
    try:
        child = psutil.Process(pid)
        assert child.cmdline()[-1] == "import time; time.sleep(30)"
        child.terminate()
        child.wait(timeout=5)
    except psutil.NoSuchProcess:
        pass


@pytest.mark.skipif(os.name != "nt", reason="Windows inherited stdio regression")
def test_launcher_returns_while_background_descendant_holds_stdout(tmp_path):
    command, pid_file = launcher(tmp_path, keep_parent_alive=False)
    started = time.monotonic()
    try:
        result = run_background_launcher(command, cwd=tmp_path, timeout=2)
        elapsed = time.monotonic() - started
        assert result.returncode == 0 and elapsed < 3
        value = json.loads(result.stdout)
        assert value["status"] == "REPLACED" and value["worker"] == 1
        assert psutil.Process(value["child_pid"]).is_running()
        assert "launcher diagnostics" in result.stderr
        assert result.stdout_path.is_file() and result.stderr_path.is_file()
    finally:
        stop_owned_fixture_child(pid_file)


@pytest.mark.skipif(os.name != "nt", reason="Windows timeout cleanup regression")
def test_timeout_is_bounded_with_live_background_descendant_and_keeps_logs(tmp_path):
    command, pid_file = launcher(tmp_path, keep_parent_alive=True)
    started = time.monotonic()
    try:
        with pytest.raises(subprocess.TimeoutExpired) as failed:
            run_background_launcher(command, cwd=tmp_path, timeout=0.5)
        assert time.monotonic() - started < 3
        value = json.loads(failed.value.stdout)
        assert psutil.Process(value["child_pid"]).is_running()
        assert "launcher diagnostics" in failed.value.stderr
        assert failed.value.stdout_path.is_file()
    finally:
        stop_owned_fixture_child(pid_file)


def test_adspower_local_api_ignores_system_proxy(monkeypatch):
    spec = importlib.util.spec_from_file_location("adspower_direct_test", ROOT / "scripts/replace_proxyshare_worker_environment.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    captured = []
    class Opener:
        def open(self, request, *, timeout):
            assert request.full_url == "http://127.0.0.1:50325/status"
            assert timeout == 7
            return io.BytesIO(b'{"code":0,"msg":"success"}')
    def build(handler):
        captured.append(handler.proxies)
        return Opener()
    monkeypatch.setattr(module.urllib.request, "build_opener", build)
    monkeypatch.setattr(module.urllib.request, "urlopen", lambda *_a, **_k: pytest.fail("system proxy path used"))
    assert module.api_call("http://127.0.0.1:50325", "/status", timeout=7)["code"] == 0
    assert captured == [{}]
