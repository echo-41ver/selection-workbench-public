import json
import time
from types import SimpleNamespace

import pytest

from selection_automation import local_agents


@pytest.mark.parametrize('changed', [None, 'model', 'version', 'login', 'failed_probe'])
def test_verified_cli_capability_survives_time_but_not_identity_or_auth_changes(tmp_path, monkeypatch, changed):
    exe = tmp_path / 'codex.exe'; exe.touch()
    monkeypatch.setattr(local_agents, 'find_executable', lambda root, agent: str(exe))
    def run(prefix, args, root):
        return SimpleNamespace(returncode=1 if changed == 'login' and args == ['login', 'status'] else 0,
                               stdout='codex-cli 0.162.0', stderr='')
    monkeypatch.setattr(local_agents, '_run', run)
    receipt = local_agents.connection_receipt(tmp_path, 'codex', 'selected-model')
    receipt.parent.mkdir(parents=True)
    receipt.write_text(json.dumps({'executable': str(exe), 'version': 'old' if changed == 'version' else '0.162.0',
        'tested_at': time.time() - 86400, 'connected': changed != 'failed_probe', 'probe_revision': 2,
        'selected_model': 'other' if changed == 'model' else 'selected-model'}))
    row = local_agents._probe_cli(tmp_path, 'codex', 'selected-model')
    assert row['ready'] is (changed is None)


@pytest.mark.parametrize('blocked', [False, True])
def test_desktop_capability_still_requires_current_native_transport(tmp_path, monkeypatch, blocked):
    from selection_automation import desktop_agents
    exe = tmp_path / 'app.exe'; exe.touch()
    monkeypatch.setattr(desktop_agents, 'probe', lambda root, agent: {
        'id': agent, 'installed': True, 'executable': str(exe), 'desktop_path': str(exe), 'version': None,
        'transport_available': not blocked, 'authenticated': None, 'requires_enable': blocked,
        'ready': False, 'connected': False, 'status': '待连接验证'})
    receipt = local_agents.connection_receipt(tmp_path, 'doubao', 'selected-model')
    receipt.parent.mkdir(parents=True)
    stamp = exe.stat()
    receipt.write_text(json.dumps({'executable': str(exe), 'version': None, 'tested_at': time.time() - 86400,
        'installation_stamp': f'{stamp.st_mtime_ns}:{stamp.st_size}', 'connected': True,
        'probe_revision': 2, 'selected_model': 'selected-model'}))
    assert local_agents._probe(tmp_path, 'doubao', 'selected-model')['ready'] is (not blocked)


def _real_probe(tmp_path, monkeypatch, script, *, timeout=.4, grace=1.5, startup_timeout=5):
    import sys
    import time
    from pathlib import Path
    agent = {'id': 'codex', 'name': 'Codex CLI', 'installed': True,
             'authenticated': True, 'executable': sys.executable, 'version': 'test'}
    monkeypatch.setattr(local_agents, 'discover', lambda *a, **kw: [dict(agent)])
    monkeypatch.setattr(local_agents, 'analysis_command',
                        lambda *a, **kw: [sys.executable, '-u', '-c', script])
    original_popen = local_agents.subprocess.Popen
    def ready_interpreter(command, *args, **kwargs):
        job = Path(kwargs['cwd'])
        ready = job/'synthetic-interpreter-ready.txt'
        assert not ready.exists()
        # Cold Windows interpreter/import startup is not the simulated CLI wait.
        # The same real child signals readiness, then waits for the sole stdin.
        command = list(command)
        command[-1] = ('import json, pathlib, subprocess, sys, time\n'
            f'pathlib.Path({str(ready)!r}).write_text("ready")\n' + command[-1])
        started = time.monotonic()
        process = original_popen(command, *args, **kwargs)
        try:
            deadline = started + startup_timeout
            while not ready.is_file() and process.poll() is None and time.monotonic() < deadline:
                time.sleep(.01)
            metadata = {'pid':process.pid, 'ready':ready.is_file(),
                        'seconds':time.monotonic()-started, 'timeout_seconds':startup_timeout}
            if metadata['ready']:
                (job/'fixture-startup.json').write_text(json.dumps(metadata), encoding='utf-8')
                return process
            # A failed fixture must not orphan its one synthetic process.
            if process.poll() is None:
                process.kill()
            try:
                stdout, stderr = process.communicate(timeout=1)
            except local_agents.subprocess.TimeoutExpired:
                process.wait(timeout=1)
                pytest.fail('Synthetic interpreter pipe cleanup did not finish')
            metadata['native_exit_code'] = process.returncode
            (job/'fixture-startup.json').write_text(json.dumps(metadata))
            (job/'fixture-startup.stderr.log').write_text(local_agents._probe_output(stderr), encoding='utf-8')
            pytest.fail('Synthetic interpreter did not become ready within its startup bound')
        except BaseException:
            if process.poll() is None:
                process.kill()
            process.wait(timeout=1)
            raise
    monkeypatch.setattr(local_agents.subprocess, 'Popen', ready_interpreter)
    # Exercise the old subprocess.run timeout as well as the new bounded runner.
    original_run = local_agents._run
    monkeypatch.setattr(local_agents, '_run', lambda p, a, r, timeout=12, text=None:
                        original_run(p, a, r, timeout=min(timeout, .4), text=text))
    monkeypatch.setattr(local_agents, 'CONNECTION_PROBE_TIMEOUT_SECONDS', timeout, raising=False)
    monkeypatch.setattr(local_agents, 'CONNECTION_PROBE_FINALIZE_SECONDS', grace, raising=False)
    monkeypatch.setattr(local_agents, 'CONNECTION_PROBE_CLEANUP_SECONDS', .5, raising=False)
    row = local_agents.check_connection(tmp_path, 'codex')
    return row, Path(row['probe_job'])


def test_real_probe_allows_one_bounded_same_process_exit_after_correct_answer(tmp_path, monkeypatch):
    row, job = _real_probe(tmp_path, monkeypatch, '''
import json, pathlib, sys, time
request = sys.stdin.read()
pathlib.Path('execution-count.txt').open('a').write('1')
digits = json.loads(pathlib.Path('probe-expectation.json').read_text())['digits']
pathlib.Path('vision-probe.txt').write_text(digits)
time.sleep(.6)
print('probe finished', flush=True)
''')
    assert row['connected'] and row['ready']
    execution = json.loads((job / 'probe-execution.json').read_text())
    assert execution['native_exit_code'] == 0 and execution['completed']
    assert execution['finalize_grace_used'] and not execution['timed_out']
    assert (job / 'execution-count.txt').read_text() == '1'
    assert 'probe finished' in (job / 'stdout.log').read_text()


def test_real_probe_timeout_preserves_failure_native_code_and_redacts_output(tmp_path, monkeypatch):
    import hashlib
    import uuid
    test_only_token = 'sk-' + 'TEST_ONLY_' + uuid.uuid4().hex
    test_only_secret = 'TEST_ONLY_' + uuid.uuid4().hex
    expected_stdout = f'Authorization: Bearer {test_only_token}\n'
    expected_stderr = f'api_key={test_only_secret}\n'
    row, job = _real_probe(tmp_path, monkeypatch, '''
import pathlib, sys, time
sys.stdin.read()
pathlib.Path('execution-count.txt').open('a').write('1')
print(%r, flush=True)
print(%r, file=sys.stderr, flush=True)
time.sleep(60)
''' % (expected_stdout.rstrip('\n'), expected_stderr.rstrip('\n')), timeout=.25, grace=.25)
    assert not row['connected'] and not row['ready']
    execution = json.loads((job / 'probe-execution.json').read_text())
    assert execution['timed_out'] and not execution['finalize_grace_used']
    assert execution['native_exit_code'] is not None
    assert execution['native_exit_code'] != 0 and not execution['completed']
    assert execution['error_kind'] == 'TimeoutExpired'
    assert '超时' in row['status']
    assert (job / 'execution-count.txt').read_text() == '1'
    for stream, emitted in (('stdout', expected_stdout), ('stderr', expected_stderr)):
        raw = emitted.encode('utf-8')
        assert execution[stream + '_bytes'] == len(raw)
        assert execution[stream + '_sha256'] == hashlib.sha256(raw).hexdigest()
        assert '[REDACTED]' in (job / (stream + '.log')).read_text()
    for name in ('probe-execution.json', 'stdout.log', 'stderr.log'):
        content = (job / name).read_text()
        assert test_only_token not in content
        assert test_only_secret not in content


@pytest.mark.parametrize('exit_code', [0, 7])
def test_correct_answer_still_requires_real_native_zero(tmp_path, monkeypatch, exit_code):
    # This case checks the real exit code, not subsecond Windows scheduling.
    # The separate timeout/finalization cases retain their short deadlines.
    row, job = _real_probe(tmp_path, monkeypatch, '''
import json, pathlib, sys
sys.stdin.read()
digits = json.loads(pathlib.Path('probe-expectation.json').read_text())['digits']
pathlib.Path('vision-probe.txt').write_text(digits)
sys.exit(%d)
''' % exit_code, timeout=2)
    assert row['connected'] is (exit_code == 0)
    execution = json.loads((job / 'probe-execution.json').read_text())
    assert execution['native_exit_code'] == exit_code
    assert not execution['timed_out'] and not execution['finalize_grace_used']


def test_correct_answer_cannot_pass_if_same_process_exceeds_finalization_bound(tmp_path, monkeypatch):
    row, job = _real_probe(tmp_path, monkeypatch, '''
import json, pathlib, sys, time
sys.stdin.read()
pathlib.Path('execution-count.txt').open('a').write('1')
digits = json.loads(pathlib.Path('probe-expectation.json').read_text())['digits']
pathlib.Path('vision-probe.txt').write_text(digits)
time.sleep(60)
''', timeout=.25, grace=.25)
    assert not row['connected']
    execution = json.loads((job / 'probe-execution.json').read_text())
    assert execution['vision_answer_matches'] and execution['finalize_grace_used']
    assert execution['timed_out'] and not execution['completed']
    assert execution['native_exit_code'] not in (None, 0)
    assert (job / 'execution-count.txt').read_text() == '1'


def test_native_zero_without_correct_vision_is_not_connected(tmp_path, monkeypatch):
    row, job = _real_probe(tmp_path, monkeypatch, '''
import pathlib, sys
sys.stdin.read()
pathlib.Path('vision-probe.txt').write_text('not an image answer')
''')
    assert not row['connected']
    execution = json.loads((job / 'probe-execution.json').read_text())
    assert execution['native_exit_code'] == 0
    assert not execution['vision_answer_matches'] and not execution['completed']


def test_probe_start_failure_still_saves_private_safe_evidence(tmp_path, monkeypatch):
    def denied(*args, **kwargs):
        raise PermissionError('password=never-retain-exception-credentials')
    monkeypatch.setattr(local_agents.subprocess, 'Popen', denied)
    row, job = _real_probe(tmp_path, monkeypatch, 'unused')
    assert not row['connected']
    execution = json.loads((job / 'probe-execution.json').read_text())
    assert execution['native_exit_code'] is None and execution['pid'] is None
    assert execution['error_kind'] == 'PermissionError'
    assert not execution['completed']
    assert 'never-retain' not in (job / 'probe-execution.json').read_text()
    assert (job / 'stdout.log').is_file() and (job / 'stderr.log').is_file()


def test_inherited_child_pipe_does_not_turn_exit_zero_into_false_connection_or_hang(tmp_path, monkeypatch):
    import time
    started = time.monotonic()
    row, job = _real_probe(tmp_path, monkeypatch, '''
import json, pathlib, subprocess, sys
sys.stdin.read()
digits = json.loads(pathlib.Path('probe-expectation.json').read_text())['digits']
pathlib.Path('vision-probe.txt').write_text(digits)
subprocess.Popen([sys.executable, '-c', "import pathlib,time;time.sleep(1.2);pathlib.Path('pipe-child-finished.txt').write_text('done')"], stdout=sys.stdout, stderr=sys.stderr)
''', timeout=.3, grace=.3)
    elapsed = time.monotonic() - started
    startup = json.loads((job / 'fixture-startup.json').read_text())
    assert not row['connected'] and elapsed - startup['seconds'] < 2
    execution = json.loads((job / 'probe-execution.json').read_text())
    assert execution['native_exit_code'] == 0 and execution['timed_out']
    assert execution['vision_answer_matches'] and not execution['completed']
    assert not execution['output_capture_complete']
    # The sole synthetic descendant exits on its own; do not leave test children.
    deadline = time.monotonic() + 3
    while not (job / 'pipe-child-finished.txt').is_file() and time.monotonic() < deadline:
        time.sleep(.01)
    assert (job / 'pipe-child-finished.txt').is_file()


def test_fixture_startup_timeout_reaps_its_one_owned_child(tmp_path, monkeypatch):
    import sys
    native_popen = local_agents.subprocess.Popen
    children = []
    def never_ready(command, *args, **kwargs):
        process = native_popen([sys.executable, '-c', 'import time;time.sleep(60)'], *args, **kwargs)
        children.append(process)
        return process
    monkeypatch.setattr(local_agents.subprocess, 'Popen', never_ready)
    with pytest.raises(pytest.fail.Exception, match='did not become ready'):
        _real_probe(tmp_path, monkeypatch, 'unused', startup_timeout=.2)
    assert len(children) == 1 and children[0].poll() is not None
    metadata_files = list((tmp_path/'runtime/agent-config/probes').rglob('fixture-startup.json'))
    assert len(metadata_files) == 1
    metadata = json.loads(metadata_files[0].read_text())
    assert not metadata['ready'] and metadata['native_exit_code'] not in (None, 0)
