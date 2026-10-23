"""Normal Stop retires browsers; explicit resume must use fresh profiles only."""
import json
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import company_setup
import selection


def stopped_run(root):
    category = {'category_key': 'stopped_fixture', 'category_name_zh': '停止后继续测试',
                'temu': {'worker_count': 1}}
    base = root / 'runs' / category['category_key']
    worker = {'name': 'worker-01-new', 'profile_id': 'retired-profile',
              'webdriver_url': 'http://127.0.0.1:4451',
              'session_id_file': 'runtime/worker-01-new/retired-session.txt'}
    manifest = {'schema_version': 'temu-browser-sessions-v1', 'machine_id': 'fixture-machine',
                'task_key': category['category_key'], 'used': True, 'worker_count': 1,
                'workers': [worker]}
    receipt = root / 'runtime/profile_retirement/20261023-stop.json'
    selection.save(root / 'runtime/machine.json', {'id': 'fixture-machine'})
    selection.save(base / 'category.json', category)
    selection.save(receipt, {'machine_id': 'fixture-machine', 'new_task_key': category['category_key'],
                            'records': [{'path': str(root / 'config/sessions.json'), 'manifest': manifest}],
                            'deleted': ['retired-profile'], 'already_missing': []})
    selection.save(root / 'config/proxy_policy.json', {})
    selection.save(base / 'temu/keyword_plan.json', {'finalized': True, 'keywords': [{'query': 'kept query'}]})
    selection.save(base / 'temu/supervisors/assignments.json', {'jobs': [{'query': 'kept query', 'complete': False}]})
    selection.save(base / 'temu/workers/worker-01-new/kept/products.jsonl', {'goods_id': 'kept-source'})
    selection.save(base / 'temu/workers/worker-01-new/kept/progress.json', {'status': 'USER_STOPPED', 'scrolls': 2})
    selection.save(base / 'temu/supervisors/worker-01-new.json', {'status': 'WAITING_SEARCH_VERIFICATION'})
    return category, base, receipt


def fresh_manifest(category):
    return {'schema_version': 'temu-browser-sessions-v1', 'machine_id': 'fixture-machine',
            'task_key': category['category_key'], 'workers': [{'name': 'worker-01-new',
            'profile_id': 'fresh-profile', 'session_id_file': 'runtime/fresh-session.txt'}]}


def source_bytes(base):
    return {path: path.read_bytes() for path in (base / 'temu').rglob('*') if path.is_file()
            and path.name != 'worker-01-new.json'}


def test_stopped_resume_supervisor_reprovisions_without_touching_partial_collection(tmp_path, monkeypatch):
    category, base, receipt = stopped_run(tmp_path)
    before = source_bytes(base)
    monkeypatch.setattr(selection, 'ROOT', tmp_path)
    monkeypatch.setattr(selection, 'identity', lambda: 'fixture-machine')
    commands = []

    def spawn(command, log):
        commands.append(command)
        assert command[2:5] == ['--start', '--preserve-task', category['category_key']]
        assert not (tmp_path / 'config/sessions.json').exists()
        selection.save(tmp_path / 'config/sessions.json', fresh_manifest(category))
        return SimpleNamespace(pid=12345, poll=lambda: 0)

    monkeypatch.setattr(selection, 'spawn', spawn)
    children, state = {}, {}
    assert selection.provision_company_process(base, category, False, children=children,
                                               state=state, preserve_prepared=True)
    assert len(commands) == 1 and not children
    assert source_bytes(base) == before
    assert selection.read(tmp_path / 'config/sessions.json')['workers'][0]['profile_id'] == 'fresh-profile'
    assert selection.read(base / 'temu/supervisors/worker-01-new.json')['status'] == 'STARTING'
    assert '新环境' in state['bootstrap']['启动阶段']
    assert receipt.exists()


@pytest.mark.parametrize('broken', ['missing_receipt', 'foreign_receipt_machine', 'foreign_task',
    'foreign_manifest_machine', 'foreign_path', 'unfinished_retirement', 'invalid_schema',
    'wrong_worker_count', 'foreign_active_manifest', 'retired_bootstrap'])
def test_stopped_resume_rejects_missing_or_foreign_ownership_before_spawn(tmp_path, monkeypatch, broken):
    category, base, receipt = stopped_run(tmp_path)
    value = selection.read(receipt)
    manifest = value['records'][0]['manifest']
    if broken == 'missing_receipt': receipt.unlink()
    elif broken == 'foreign_receipt_machine': value['machine_id'] = 'other-machine'
    elif broken == 'foreign_task': manifest['task_key'] = 'other_task'
    elif broken == 'foreign_manifest_machine': manifest['machine_id'] = 'other-machine'
    elif broken == 'foreign_path': value['records'][0]['path'] = str(tmp_path / 'other/sessions.json')
    elif broken == 'unfinished_retirement': value['deleted'] = []
    elif broken == 'invalid_schema': manifest.pop('schema_version')
    elif broken == 'wrong_worker_count': manifest['worker_count'] = 2
    elif broken == 'foreign_active_manifest':
        selection.save(tmp_path / 'config/sessions.json', {'task_key': 'other_task', 'machine_id': 'fixture-machine'})
    elif broken == 'retired_bootstrap':
        selection.save(tmp_path / 'runtime/bootstrap.json', {'task_key': category['category_key'],
            'machine_id': 'fixture-machine', 'workers': manifest['workers']})
    if broken != 'missing_receipt': selection.save(receipt, value)
    monkeypatch.setattr(selection, 'ROOT', tmp_path)
    monkeypatch.setattr(selection, 'identity', lambda: 'fixture-machine')
    monkeypatch.setattr(selection, 'spawn', lambda *a, **k: pytest.fail('unproved recovery must not start provisioning'))
    with pytest.raises(RuntimeError, match='维护续接必须属于本任务与本安装'):
        selection.provision_company_process(base, category, False, children={}, state={}, preserve_prepared=True)


def test_preserve_company_resume_creates_new_profiles_from_owned_retirement(tmp_path, monkeypatch):
    import replace_proxyshare_worker_environment as replace
    import setup_proxyshare_adspower_workers as setup
    import selection_automation.adspower_kernel as kernel
    import selection_automation.flower_background as background
    import selection_automation.local_proxy_prepare as routes

    category, base, _ = stopped_run(tmp_path)
    # A completed lane's retired marker must never authorize reopening that profile.
    selection.save(base / 'temu/supervisors/worker-01-new.json', {'status': 'WORKER_COMPLETE',
        'environment_cleanup': 'COMPLETE', 'deleted_profile_id': 'retired-profile'})
    before = source_bytes(base)
    secret = tmp_path / 'runtime/secrets/company.dpapi'
    secret.parent.mkdir(parents=True); secret.touch()
    monkeypatch.setattr(selection, 'ROOT', tmp_path)
    monkeypatch.setattr(selection, 'identity', lambda: 'fixture-machine')
    monkeypatch.setattr(company_setup, 'ROOT', tmp_path)
    monkeypatch.setattr(company_setup, 'load', lambda _: {'adspower_api_key': 'TEST_ONLY', 'proxy_architecture': 'LOCAL_VPN'})
    monkeypatch.setattr(company_setup, 'ensure_adspower_api', lambda _: None)
    monkeypatch.setattr(company_setup, 'free_block', lambda start, count: list(range(start, start + count)))
    monkeypatch.setattr(company_setup, 'rebind_existing_worker', lambda *a: pytest.fail('must never rebind retired profile'))
    monkeypatch.setattr(company_setup, 'restore_closed_task_workers', lambda *a: pytest.fail('must never reopen old session'))
    monkeypatch.setattr(setup, 'ROOT', tmp_path)
    monkeypatch.setattr(setup, 'SESSIONS', tmp_path / 'config/sessions.json')
    monkeypatch.setattr(kernel, 'ensure_kernel', lambda *a: None)
    monkeypatch.setattr(background, 'install_defaults', lambda *a: {})
    monkeypatch.setattr(routes, 'prepare', lambda *a: ({}, {}))
    monkeypatch.setattr(replace, 'api_call', lambda *a, **k: pytest.fail('test must not make business requests'))
    calls = []

    def create(command, **kwargs):
        assert Path(command[1]).name == 'setup_proxyshare_adspower_workers.py'
        assert command[-2:] == ['--new-task', category['category_key']]
        assert not (tmp_path / 'config/sessions.json').exists()
        calls.append(command)
        selection.save(tmp_path / 'config/sessions.json', fresh_manifest(category))

    monkeypatch.setattr(company_setup.subprocess, 'run', create)
    verified = []
    monkeypatch.setattr(company_setup, 'verify_prepared_bindings', lambda root, read:
        verified.append(read(root / 'config/sessions.json')['workers'][0]['profile_id']))
    company_setup.ensure_company(True, worker_count=1, preserve_task=category['category_key'])
    assert len(calls) == 1 and verified == ['fresh-profile']
    assert source_bytes(base) == before
    assert selection.read(base / 'temu/supervisors/worker-01-new.json')['deleted_profile_id'] == 'retired-profile'


def test_retired_resume_allows_only_fresh_same_task_bootstrap_and_missing_profiles(tmp_path):
    from selection_automation.retired_collection import retired_task_receipt
    category, _, receipt = stopped_run(tmp_path)
    value = selection.read(receipt)
    value.update(deleted=[], already_missing=['retired-profile'])
    selection.save(receipt, value)
    selection.save(tmp_path / 'runtime/bootstrap.json', {'machine_id': 'fixture-machine',
        'task_key': category['category_key'], 'used': False, 'requested_worker_count': 1,
        'workers': [{'name': 'worker-01-new', 'profile_id': 'fresh-pending-profile'}]})
    assert retired_task_receipt(tmp_path, category['category_key'], 'fixture-machine', 1) == receipt


@pytest.mark.parametrize('broken', ['missing_machine', 'foreign_local_machine', 'foreign_frozen_task',
    'foreign_bootstrap_machine', 'foreign_bootstrap_task', 'used_bootstrap', 'foreign_cleanup_marker',
    'foreign_retirement_task', 'duplicate_pending_worker', 'malformed_deleted'])
def test_retirement_proof_cannot_adopt_foreign_or_malformed_state(tmp_path, broken):
    from selection_automation.retired_collection import retired_task_receipt
    category, base, receipt = stopped_run(tmp_path)
    if broken == 'missing_machine': (tmp_path / 'runtime/machine.json').unlink()
    elif broken == 'foreign_local_machine': selection.save(tmp_path / 'runtime/machine.json', {'id': 'other-machine'})
    elif broken == 'foreign_frozen_task': selection.save(base / 'category.json', {**category, 'category_key': 'other_task'})
    elif broken == 'foreign_cleanup_marker':
        selection.save(base / 'temu/supervisors/worker-01-new.json', {'deleted_profile_id': 'other-profile'})
    elif broken == 'foreign_retirement_task':
        value = selection.read(receipt); value['new_task_key'] = 'other_task'; selection.save(receipt, value)
    elif broken == 'malformed_deleted':
        value = selection.read(receipt); value['deleted'] = 'retired-profile'; selection.save(receipt, value)
    else:
        bootstrap = {'machine_id': 'fixture-machine', 'task_key': category['category_key'],
                     'used': False, 'requested_worker_count': 1, 'workers': []}
        if broken == 'foreign_bootstrap_machine': bootstrap['machine_id'] = 'other-machine'
        elif broken == 'foreign_bootstrap_task': bootstrap['task_key'] = 'other_task'
        elif broken == 'used_bootstrap': bootstrap['used'] = True
        elif broken == 'duplicate_pending_worker': bootstrap['workers'] = [
            {'name': 'worker-01-new', 'profile_id': 'new-one'}, {'name': 'worker-01-new', 'profile_id': 'new-two'}]
        selection.save(tmp_path / 'runtime/bootstrap.json', bootstrap)
    assert retired_task_receipt(tmp_path, category['category_key'], 'fixture-machine', 1) is None


def test_company_rechecks_receipt_before_any_provisioning(tmp_path, monkeypatch):
    category, _, receipt = stopped_run(tmp_path)
    # A receipt accepted by an earlier reader cannot remain authority after it changes.
    value = selection.read(receipt); value['deleted'] = []; selection.save(receipt, value)
    secret = tmp_path / 'runtime/secrets/company.dpapi'
    secret.parent.mkdir(parents=True); secret.touch()
    monkeypatch.setattr(company_setup, 'ROOT', tmp_path)
    monkeypatch.setattr(company_setup, 'load', lambda _: {'adspower_api_key': 'TEST_ONLY'})
    monkeypatch.setattr(selection, 'identity', lambda: 'fixture-machine')
    monkeypatch.setattr(company_setup, 'ensure_adspower_api', lambda *a: pytest.fail('must reject before service access'))
    with pytest.raises(RuntimeError, match='维护续接必须属于本任务与本安装'):
        company_setup.ensure_company(True, worker_count=1, preserve_task=category['category_key'])
