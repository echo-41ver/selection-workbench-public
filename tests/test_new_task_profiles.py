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
        'workers':[{'profile_id':'owned'},{'profile_id':'missing'}],**fields}))
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
        'workers':[{'profile_id':'owned','state':'BOUND'}]}))
    setup.retire_for_task('same')
    assert not sessions.exists() and not journal.exists()
    assert actions==[('stop','/api/v1/browser/stop?user_id=owned'),('delete','owned')]
