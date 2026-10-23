import json
from pathlib import Path
import sys
import pytest
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'scripts'))
import setup_proxyshare_adspower_workers as setup
import selection
import replace_proxyshare_worker_environment as replacement
from selection_automation import profile_capacity


def fixture(tmp_path,monkeypatch,**fields):
    sessions=tmp_path/'config/sessions.json';sessions.parent.mkdir()
    sessions.write_text(json.dumps({'machine_id':'mine','task_key':'old','used':True,
        'workers':[{'name':'worker-01-new','profile_id':'owned'},
                   {'name':'worker-02-new','profile_id':'missing'}],**fields}))
    monkeypatch.setattr(setup,'ROOT',tmp_path);monkeypatch.setattr(setup,'SESSIONS',sessions)
    monkeypatch.setattr(selection,'identity',lambda:'mine');monkeypatch.setattr(selection,'live_owner',lambda:None)
    monkeypatch.setattr(setup,'api_base',lambda:'local')
    monkeypatch.setattr(profile_capacity,'list_profiles',lambda *a:[{'user_id':'owned'},{'user_id':'foreign'}])
    actions=[]
    monkeypatch.setattr(setup,'api_call',lambda *a,**k:actions.append(('stop',a[1])) or {})
    def delete(base,identifier):
        assert list((tmp_path/'runtime/profile_retirement').glob('*.json'))
        actions.append(('delete',identifier))
    monkeypatch.setattr(replacement,'delete_profile_after_release',delete)
    return sessions,actions


def test_new_task_retires_only_owned_existing_and_records_missing(tmp_path,monkeypatch):
    sessions,actions=fixture(tmp_path,monkeypatch)
    setup.retire_for_task('new_task')
    assert actions==[('stop','/api/v1/browser/stop?user_id=owned'),('delete','owned')]
    assert not sessions.exists()
    snapshot=json.loads(next((tmp_path/'runtime/profile_retirement').glob('*.json')).read_text('utf-8'))
    assert snapshot['already_missing']==['missing']


def test_same_unused_bootstrap_may_resume_but_used_must_retire(tmp_path,monkeypatch):
    sessions,actions=fixture(tmp_path,monkeypatch,task_key='same',used=False)
    setup.retire_for_task('same');assert not actions and sessions.exists()
    value=json.loads(sessions.read_text());value['used']=True;sessions.write_text(json.dumps(value))
    setup.retire_for_task('same');assert actions and not sessions.exists()


def test_closed_started_profile_retires_even_when_bootstrap_is_not_marked_used(tmp_path,monkeypatch):
    sessions,actions=fixture(tmp_path,monkeypatch,task_key='same',used=False)
    from selection_automation import profile_single_use
    monkeypatch.setattr(profile_single_use,'running_profile',lambda _:False)
    directory=tmp_path/'runtime/profile-single-use';directory.mkdir(parents=True)
    (directory/'owned.json').write_text('{}')
    setup.retire_for_task('same')
    assert actions and not sessions.exists()


def test_foreign_manifest_and_other_active_task_never_deleted(tmp_path,monkeypatch):
    sessions,actions=fixture(tmp_path,monkeypatch,machine_id='other')
    with pytest.raises(RuntimeError):setup.retire_for_task('new_task')
    assert not actions and sessions.exists()
    monkeypatch.setattr(selection,'live_owner',lambda:{'category_key':'other_active'})
    with pytest.raises(RuntimeError):setup.retire_for_task('new_task')
    assert not actions


def test_replacement_updates_visible_browser_number_without_leaving_old_number(tmp_path,monkeypatch):
    sessions=tmp_path/'config/sessions.json';sessions.parent.mkdir()
    sessions.write_text(json.dumps({'workers':[{'name':'worker-01-new','profile_id':'old','serial_number':'91'}]}))
    monkeypatch.setattr(replacement,'ROOT',tmp_path)
    monkeypatch.setattr(replacement,'SESSIONS',sessions)
    replacement.update_sessions_profile('worker-01-new','new','104')
    worker=json.loads(sessions.read_text())['workers'][0]
    assert worker['profile_id']=='new' and worker['serial_number']=='104'
    replacement.update_sessions_profile('worker-01-new','new')
    worker=json.loads(sessions.read_text())['workers'][0]
    assert worker['serial_number']=='104'  # binding completion must retain it
    replacement.update_sessions_profile('worker-01-new','newer')
    worker=json.loads(sessions.read_text())['workers'][0]
    assert worker['profile_id']=='newer' and 'serial_number' not in worker


def test_retiring_manifest_also_retires_leftover_completed_journal(tmp_path,monkeypatch):
    sessions,actions=fixture(tmp_path,monkeypatch,task_key='same',used=True)
    journal=tmp_path/'runtime/bootstrap.json';journal.parent.mkdir()
    journal.write_text(json.dumps({'machine_id':'mine','task_key':'same','used':False,
        'workers':[{'name':'worker-01-new','profile_id':'owned','state':'BOUND'}]}))
    setup.retire_for_task('same')
    assert not sessions.exists() and not journal.exists()
    assert actions==[('stop','/api/v1/browser/stop?user_id=owned'),('delete','owned')]


def test_retirement_driver_failure_keeps_manifests_and_is_retryable_after_delete(tmp_path, monkeypatch):
    from selection_automation import driver_retirement
    sessions, actions = fixture(tmp_path, monkeypatch)
    known = [{'user_id': 'owned'}]
    monkeypatch.setattr(profile_capacity, 'list_profiles', lambda *a: list(known))
    original_delete = replacement.delete_profile_after_release
    def delete(*args):
        original_delete(*args)
        known.clear()
    monkeypatch.setattr(replacement, 'delete_profile_after_release', delete)
    releases = []
    def blocked(root, worker, identifier):
        assert actions[-1] == ('delete', 'owned')
        releases.append(identifier)
        raise driver_retirement.DriverCleanupRequired('驱动身份不符')
    monkeypatch.setattr(driver_retirement, 'release_retired_driver', blocked)
    with pytest.raises(driver_retirement.DriverCleanupRequired):
        setup.retire_for_task('new_task')
    assert sessions.exists()
    pending = json.loads(next((tmp_path / 'runtime/profile_retirement').glob('*.json')).read_text('utf-8'))
    assert pending['deleted'] == ['owned'] and pending['already_missing'] == ['missing']
    assert pending['environment_cleanup'] == 'RETRY_REQUIRED'
    assert '驱动' in pending['environment_cleanup_reason']
    def release(root, worker, identifier):
        releases.append(identifier)
        return {'status': 'RELEASED'}
    monkeypatch.setattr(driver_retirement, 'release_retired_driver', release)
    setup.retire_for_task('new_task')
    assert not sessions.exists()
    assert actions == [('stop', '/api/v1/browser/stop?user_id=owned'), ('delete', 'owned')]
    receipts = [json.loads(p.read_text('utf-8')) for p in (tmp_path / 'runtime/profile_retirement').glob('*.json')]
    assert any(r.get('environment_cleanup') == 'COMPLETE' and len(r['driver_cleanup']) == 2 for r in receipts)
    assert any(r.get('environment_cleanup') == 'RETRY_REQUIRED' for r in receipts)


@pytest.mark.parametrize('protected', ['same_unused', 'foreign_machine', 'other_active_task'])
def test_driver_cleanup_never_enters_active_or_foreign_retirement(tmp_path, monkeypatch, protected):
    from selection_automation import driver_retirement
    fields = {'task_key': 'same', 'used': False} if protected == 'same_unused' else {}
    if protected == 'foreign_machine': fields['machine_id'] = 'foreign'
    sessions, actions = fixture(tmp_path, monkeypatch, **fields)
    if protected == 'other_active_task':
        monkeypatch.setattr(selection, 'live_owner', lambda: {'category_key': 'other'})
    def forbidden(*a): pytest.fail('active or foreign driver must never be released')
    monkeypatch.setattr(driver_retirement, 'release_retired_driver', forbidden)
    if protected == 'same_unused': setup.retire_for_task('same')
    else:
        with pytest.raises(RuntimeError): setup.retire_for_task('same')
    assert sessions.exists() and not actions


def test_deferred_bootstrap_then_replacement_retires_current_driver_once(tmp_path, monkeypatch):
    from selection_automation import driver_retirement
    sessions, actions = fixture(tmp_path, monkeypatch,
        workers=[{'name': 'worker-01-new', 'profile_id': 'old-profile'}])
    journal = tmp_path / 'runtime/bootstrap.json'
    journal.parent.mkdir()
    journal.write_bytes(sessions.read_bytes())
    # This is an existing supported path: READY survives a locked old journal.
    original_unlink = Path.unlink
    def locked(path, *args, **kwargs):
        if path == journal: raise PermissionError('fixture completed journal locked')
        return original_unlink(path, *args, **kwargs)
    with monkeypatch.context() as lock:
        lock.setattr(Path, 'unlink', locked)
        setup.cleanup_completed_journal(journal)
    assert journal.exists()
    monkeypatch.setattr(replacement, 'ROOT', tmp_path)
    monkeypatch.setattr(replacement, 'SESSIONS', sessions)
    replacement.update_sessions_profile('worker-01-new', 'new-profile')
    assert json.loads(journal.read_text())['workers'][0]['profile_id'] == 'old-profile'
    assert json.loads(sessions.read_text())['workers'][0]['profile_id'] == 'new-profile'
    folder = tmp_path / 'runtime/worker-01-new'
    folder.mkdir()
    (folder / 'driver.json').write_text('{"profile_id":"new-profile"}')
    monkeypatch.setattr(profile_capacity, 'list_profiles', lambda *a: [{'user_id': 'new-profile'}])
    releases = []
    def release(root, worker, identifier):
        if identifier != 'new-profile':
            raise driver_retirement.DriverCleanupRequired('旧清单与当前驱动环境不一致')
        releases.append(identifier)
        return {'status': 'RELEASED'}
    monkeypatch.setattr(driver_retirement, 'release_retired_driver', release)
    setup.retire_for_task('new_task')
    assert releases == ['new-profile']
    assert not sessions.exists() and not journal.exists()
    assert actions == [('stop', '/api/v1/browser/stop?user_id=new-profile'), ('delete', 'new-profile')]


def test_current_driver_outside_retired_worker_profiles_is_not_released(tmp_path, monkeypatch):
    from selection_automation import driver_retirement
    sessions, actions = fixture(tmp_path, monkeypatch)
    folder = tmp_path / 'runtime/worker-01-new'
    folder.mkdir(parents=True)
    (folder / 'driver.json').write_text('{"profile_id":"foreign-profile"}')
    def forbidden(*a): pytest.fail('a foreign driver must not reach the release helper')
    monkeypatch.setattr(driver_retirement, 'release_retired_driver', forbidden)
    with pytest.raises(driver_retirement.DriverCleanupRequired):
        setup.retire_for_task('new_task')
    assert sessions.exists()
    receipt = json.loads(next((tmp_path / 'runtime/profile_retirement').glob('*.json')).read_text('utf-8'))
    assert receipt['environment_cleanup'] == 'RETRY_REQUIRED'
