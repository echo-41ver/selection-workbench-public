import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import zipfile

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import apply_update as updater
import build_incremental_update as builder


class IdleServices:
    def __init__(self):
        self.stopped = 0
        self.restored = 0

    def check(self):
        pass

    def stop(self):
        self.stopped += 1

    def restore(self):
        self.restored += 1


def git(root, *args):
    return subprocess.check_output(['git', *args], cwd=root, text=True, encoding='utf-8').strip()


def make_update(tmp_path, *, channel='internal', modify=None):
    repo = tmp_path / 'repo'
    repo.mkdir()
    git(repo, 'init', '-q')
    git(repo, 'config', 'user.email', 'update-test@example.invalid')
    git(repo, 'config', 'user.name', 'Update fixture')
    git(repo, 'config', 'core.autocrlf', 'false')
    files = {'web/index.html': b'<h1>old</h1>', 'src/logic.py': b'ANSWER = 1\n',
             'docs/DEPLOYMENT_GUIDE.md': b'old deployment guide', 'README.md': b'public readme',
             'requirements.lock.txt': b'psutil==7.2.2\n', 'obsolete.txt': b'old removable source'}
    for name in ('update.cmd', 'scripts/apply_update.py', 'scripts/apply_update.ps1'):
        files[name] = (ROOT / name).read_bytes()
    for name, data in files.items():
        path = repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    git(repo, 'add', '.')
    git(repo, 'commit', '-qm', 'base')
    base_commit = git(repo, 'rev-parse', 'HEAD')
    if channel == 'internal':
        files['README.md'] = files['docs/DEPLOYMENT_GUIDE.md']
    base_zip = tmp_path / 'base.zip'
    with zipfile.ZipFile(base_zip, 'w') as archive:
        for name, data in files.items():
            archive.writestr(name, data)
        archive.writestr('BUILD_INFO.json', json.dumps({'commit': base_commit, 'channel': channel,
            'contains_company_credentials': True, 'company_config_schema': 'keep-this'}))
        archive.writestr('company.local.json', '{"api_key":"TEST_ONLY_KEPT_CONFIGURATION"}')
        archive.writestr('deployment-assets/model.bin', b'BIG_UNCHANGED_MODEL' * 1000)
    target = tmp_path / 'installed'
    with zipfile.ZipFile(base_zip) as archive:
        archive.extractall(target)
    for name in ('runtime/secrets/keep.dpapi', 'runs/retained/checkpoint.json', '.venv/keep.txt', 'node_modules/keep.txt'):
        path = target / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b'KEEP')
    (repo / 'web/index.html').write_bytes(b'<h1>new</h1>')
    (repo / 'src/logic.py').write_bytes(b'ANSWER = 2\n')
    (repo / 'new.txt').write_bytes(b'new source')
    (repo / 'obsolete.txt').unlink()
    if modify:
        modify(repo)
    git(repo, 'add', '-A')
    git(repo, 'commit', '-qm', 'update')
    base = builder.base_inventory(base_zip)
    report = builder.build(base, repo, tmp_path / 'dist')
    package = tmp_path / 'package'
    with zipfile.ZipFile(report['path']) as archive:
        archive.extractall(package)
    return repo, target, package, report, base


@pytest.mark.parametrize('channel', ['public', 'internal'])
def test_real_git_delta_preserves_configuration_data_and_runtime(tmp_path, channel):
    _, target, package, report, _ = make_update(tmp_path, channel=channel)
    services = IdleServices()
    result = updater.apply(package, target, services=services)
    assert result['ok'] and not result['dependencies_reinstalled']
    assert services.stopped == services.restored == 1
    assert (target / 'web/index.html').read_bytes() == b'<h1>new</h1>'
    assert (target / 'new.txt').read_bytes() == b'new source'
    assert not (target / 'obsolete.txt').exists()
    for name in ('runtime/secrets/keep.dpapi', 'runs/retained/checkpoint.json', '.venv/keep.txt', 'node_modules/keep.txt'):
        assert (target / name).read_bytes() == b'KEEP'
    assert json.loads((target / 'company.local.json').read_text())['api_key'] == 'TEST_ONLY_KEPT_CONFIGURATION'
    info = json.loads((target / 'BUILD_INFO.json').read_text())
    assert info['commit'] == report['target_commit']
    assert info['contains_company_credentials'] and info['company_config_schema'] == 'keep-this'
    with zipfile.ZipFile(report['path']) as archive:
        names = archive.namelist()
        assert 'payload/web/index.html' in names
        assert not any('company.local.json' in name or 'deployment-assets' in name or '.venv' in name for name in names)
        assert 'payload/requirements.lock.txt' not in names


def test_next_update_uses_small_manifest_without_a_new_full_package(tmp_path):
    repo, target, package, first, _ = make_update(tmp_path)
    updater.apply(package, target, services=IdleServices())
    base = json.loads((package / 'update-base.json').read_text())
    (repo / 'web/index.html').write_bytes(b'<h1>third</h1>')
    git(repo, 'add', '.')
    git(repo, 'commit', '-qm', 'next')
    second = builder.build(base, repo, tmp_path / 'dist')
    assert second['changed_files'] == 1 and second['base_commit'] == first['target_commit']
    folder = tmp_path / 'second'
    with zipfile.ZipFile(second['path']) as archive:
        archive.extractall(folder)
    updater.apply(folder, target, services=IdleServices())
    assert (target / 'web/index.html').read_bytes() == b'<h1>third</h1>'


@pytest.mark.parametrize('damage', ['payload', 'base_version', 'local_modification', 'retained_file', 'channel'])
def test_invalid_update_stops_before_modifying_files_or_stopping_web(tmp_path, damage):
    _, target, package, _, _ = make_update(tmp_path)
    if damage == 'payload':
        (package / 'payload/web/index.html').write_bytes(b'CORRUPT')
    elif damage == 'local_modification':
        (target / 'web/index.html').write_bytes(b'LOCAL_CHANGE')
    elif damage == 'retained_file':
        (target / 'README.md').write_bytes(b'LOCAL_README')
    else:
        path = target / 'BUILD_INFO.json'
        info = json.loads(path.read_text())
        info['commit' if damage == 'base_version' else 'channel'] = 'wrong'
        path.write_text(json.dumps(info))
    before = (target / 'BUILD_INFO.json').read_bytes()
    services = IdleServices()
    with pytest.raises(ValueError):
        updater.apply(package, target, services=services)
    assert services.stopped == 0
    assert (target / 'BUILD_INFO.json').read_bytes() == before
    assert (target / 'obsolete.txt').exists()


def test_write_failure_rolls_back_changed_deleted_and_new_files(tmp_path, monkeypatch):
    _, target, package, _, _ = make_update(tmp_path)
    old_info = (target / 'BUILD_INFO.json').read_bytes()
    original = updater.write_atomic
    def fail_once(path, data):
        if path == target / 'BUILD_INFO.json' and b'incremental-updated' in data:
            raise PermissionError('fixture write failure')
        return original(path, data)
    monkeypatch.setattr(updater, 'write_atomic', fail_once)
    services = IdleServices()
    with pytest.raises(PermissionError):
        updater.apply(package, target, services=services)
    assert (target / 'web/index.html').read_bytes() == b'<h1>old</h1>'
    assert (target / 'src/logic.py').read_bytes() == b'ANSWER = 1\n'
    assert (target / 'obsolete.txt').read_bytes() == b'old removable source'
    assert not (target / 'new.txt').exists()
    assert not (target / 'UPDATE_BASE.json').exists()
    assert (target / 'BUILD_INFO.json').read_bytes() == old_info
    journal = next((target / '.deployment-backup/updates').glob('*/transaction.json'))
    assert json.loads(journal.read_text())['state'] == 'rolled_back'
    assert services.restored == 1


def test_changed_dependency_does_not_produce_incomplete_patch(tmp_path):
    with pytest.raises(ValueError, match='依赖版本已变化'):
        make_update(tmp_path, modify=lambda repo: (repo / 'requirements.lock.txt').write_text('psutil==99\n'))


def test_web_stop_failure_does_not_skip_file_rollback(tmp_path):
    _, target, package, _, _ = make_update(tmp_path)
    class BrokenRestart(IdleServices):
        def restore(self):
            raise RuntimeError('fixture restart failure')
        def stop_started(self):
            raise RuntimeError('fixture process cleanup failure')
    with pytest.raises(RuntimeError, match='自动回滚未完成'):
        updater.apply(package, target, services=BrokenRestart())
    assert (target / 'web/index.html').read_bytes() == b'<h1>old</h1>'
    assert not (target / 'new.txt').exists()
    assert (target / 'obsolete.txt').exists()


@pytest.mark.parametrize('name', ['../x.py', 'web/../../x.py', 'C:/x.py', 'web\\x.py', 'runtime/secrets/x',
    'runs/old/file', '.git/config', '.venv/a', 'deployment-assets/a', 'Company.Local.json', 'BUILD_info.json',
    'config/credentials.local.json', 'config/sessions.json', 'auth.json', 'a.credential.xml', 'src/CON.py', 'a:stream', 'a//b'])
def test_protected_paths_cannot_be_updated(name):
    with pytest.raises(ValueError):
        updater.checked_name(name)


def test_runtime_link_cannot_redirect_updates(tmp_path):
    root = tmp_path / 'root'
    outside = tmp_path / 'outside'
    root.mkdir(); outside.mkdir()
    try:
        (root / 'web').symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip('Directory symlink permission unavailable')
    with pytest.raises(ValueError):
        updater.inside(root, 'web/index.html')


def test_active_task_rejects_update_without_stopping_processes(tmp_path, monkeypatch):
    import psutil
    fake = type('Process', (), {'pid': 999999, 'info': {'cwd': str(tmp_path),
        'cmdline': ['python', str(tmp_path / 'scripts/selection.py'), 'run']}})()
    monkeypatch.setattr(psutil, 'process_iter', lambda attrs: iter([fake]))
    services = updater.ConsoleServices(tmp_path)
    with pytest.raises(ValueError, match='后台任务'):
        services.check()


def test_foreign_installation_task_is_left_alone(tmp_path, monkeypatch):
    import psutil
    fake = type('Process', (), {'pid': 999999, 'info': {'cwd': str(tmp_path / 'another'),
        'cmdline': ['python', str(tmp_path / 'another/scripts/selection.py'), 'run']}})()
    monkeypatch.setattr(psutil, 'process_iter', lambda attrs: iter([fake]))
    updater.ConsoleServices(tmp_path).check()


def test_windows_base_interpreter_is_restarted_through_installed_venv(tmp_path, monkeypatch):
    import psutil
    fake = type('Process', (), {'pid': 999999, 'info': {'cwd': str(tmp_path),
        'cmdline': [sys._base_executable, str(tmp_path / 'scripts/web_console.py'), '--port', '8767']}})()
    monkeypatch.setattr(psutil, 'process_iter', lambda attrs: iter([fake]))
    class Response:
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def read(self): return b'{"ok":true}'
    monkeypatch.setattr(updater, 'build_opener', lambda *args: type('Opener', (), {'open': lambda *a, **k: Response()})())
    services = updater.ConsoleServices(tmp_path)
    services.check()
    expected = '.venv/Scripts/python.exe' if sys.platform == 'win32' else '.venv/bin/python'
    assert services.launches[8767][0] == str(tmp_path / expected)
