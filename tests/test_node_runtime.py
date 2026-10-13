import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from selection_automation.node_runtime import ensure_node_runtime, NodeRuntimeError


def harness(tmp_path, states, install_exit=0):
    node = tmp_path/'toolchain/node.exe'
    npm = node.parent/'node_modules/npm/bin/npm-cli.js'
    npm.parent.mkdir(parents=True)
    npm.touch()
    root = tmp_path/'project'
    root.mkdir()
    (root/'package-lock.json').write_text('{}')
    values = iter(states)
    calls = []
    def run(command, **kwargs):
        calls.append(command)
        assert kwargs['cwd'] == root
        if command[1] == '-e':
            return SimpleNamespace(returncode=0, stdout=json.dumps({'status': next(values)}))
        return SimpleNamespace(returncode=install_exit, stdout='')
    return root, lambda: ensure_node_runtime(root, run=run, which=lambda _: str(node)), calls


def test_ready_runtime_never_reinstalls(tmp_path):
    _, ensure, calls = harness(tmp_path, ['READY'])
    assert ensure() == {'ready': True, 'repaired': False}
    assert len(calls) == 1


def test_scheduled_install_uses_verified_local_proxy_without_api_key(tmp_path,monkeypatch):
    from selection_automation import browser_transport
    root=tmp_path/'project';root.mkdir()
    states=iter(['BROWSER_MISSING','BROWSER_MISSING','READY']);installs=[];messages=[]
    monkeypatch.delenv('HTTPS_PROXY',raising=False)
    monkeypatch.delenv('https_proxy',raising=False)
    monkeypatch.setenv('DEEPSEEK_API_KEY','TEST_ONLY_NOT_FOR_INSTALLER')
    monkeypatch.setattr(browser_transport,'amazon_environment',lambda root:{'AMAZON_BROWSER_PROXY':'http://127.0.0.1:7890'})
    def run(command,**kwargs):
        if command[1]=='-e':return SimpleNamespace(returncode=0,stdout=json.dumps({'status':next(states)}))
        installs.append(kwargs)
        return SimpleNamespace(returncode=0)
    result=ensure_node_runtime(root,run=run,which=lambda _: 'node',progress=lambda **fields:messages.append(fields))
    assert result['repaired'] and len(installs)==1
    assert installs[0]['env']['HTTPS_PROXY']=='http://127.0.0.1:7890'
    assert 'DEEPSEEK_API_KEY' not in installs[0]['env']
    assert installs[0]['timeout']==120
    assert 'HTTP 下载代理' in messages[0]['message']
    assert json.loads((root/'runtime/node-dependencies-status.json').read_text('utf-8'))['status']=='READY'


def test_install_timeout_records_actionable_state(tmp_path,monkeypatch):
    import subprocess
    from selection_automation import browser_transport
    monkeypatch.setattr(browser_transport,'amazon_environment',lambda root:{})
    def run(command,**kwargs):
        if command[1]=='-e':return SimpleNamespace(returncode=0,stdout='{"status":"BROWSER_MISSING"}')
        raise subprocess.TimeoutExpired(command,120)
    with pytest.raises(NodeRuntimeError):ensure_node_runtime(tmp_path,run=run,which=lambda _: 'node')
    record=json.loads((tmp_path/'runtime/node-dependencies-status.json').read_text('utf-8'))
    assert record['status']=='ERROR' and record['error']=='TimeoutExpired'


def test_clean_checkout_installs_locked_dependencies_and_browser(tmp_path):
    root, ensure, calls = harness(tmp_path, ['DEPENDENCY_MISSING', 'DEPENDENCY_MISSING', 'BROWSER_MISSING', 'READY'])
    assert ensure()['repaired']
    installs = [command for command in calls if command[1] != '-e']
    assert installs[0][2:] == ['ci', '--no-audit', '--no-fund']
    assert installs[1][1:] == [str(root/'node_modules/playwright/cli.js'), 'install', 'chromium']


def test_missing_browser_does_not_replace_installed_modules(tmp_path):
    _, ensure, calls = harness(tmp_path, ['BROWSER_MISSING', 'BROWSER_MISSING', 'READY'])
    assert ensure()['repaired']
    assert sum(command[1] != '-e' for command in calls) == 1
    assert not any('ci' in command for command in calls)


def test_other_launcher_completed_repair(tmp_path):
    _, ensure, calls = harness(tmp_path, ['DEPENDENCY_MISSING', 'READY'])
    assert ensure() == {'ready': True, 'repaired': False}
    assert all(command[1] == '-e' for command in calls)


def test_install_failure_is_actionable_and_does_not_continue(tmp_path):
    _, ensure, calls = harness(tmp_path, ['DEPENDENCY_MISSING', 'DEPENDENCY_MISSING'], install_exit=7)
    with pytest.raises(NodeRuntimeError, match='退出码 7.*deploy.cmd'):
        ensure()
    assert sum(command[1] != '-e' for command in calls) == 1


def test_missing_lockfile_refuses_unpinned_install(tmp_path):
    root, ensure, calls = harness(tmp_path, ['DEPENDENCY_MISSING', 'DEPENDENCY_MISSING'])
    (root/'package-lock.json').unlink()
    with pytest.raises(NodeRuntimeError, match='锁文件'):
        ensure()
    assert all(command[1] == '-e' for command in calls)


def test_generation_stops_before_site_or_paid_model_if_repair_fails(monkeypatch):
    import generate_category as generator
    import selection_automation.node_runtime as runtime
    monkeypatch.setenv('DEEPSEEK_API_KEY', 'TEST_ONLY_KEY')
    def fail(root,**kwargs):
        raise NodeRuntimeError('依赖修复失败；请运行 deploy.cmd')
    monkeypatch.setattr(runtime, 'ensure_node_runtime', fail)
    monkeypatch.setattr(generator, 'read_listing', lambda *_: pytest.fail('site read must not run'))
    monkeypatch.setattr(generator, 'model_json', lambda *_: pytest.fail('paid model must not run'))
    with pytest.raises(generator.CategoryGenerationError, match='deploy.cmd') as error:
        generator.generate_category('抽油泵', 50, 400)
    assert error.value.code == 'NODE_DEPENDENCIES_NOT_READY'
