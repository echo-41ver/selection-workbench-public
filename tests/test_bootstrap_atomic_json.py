import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from replace_proxyshare_worker_environment import atomic_json
from selection_automation import merge_temu_snapshots
from setup_proxyshare_adspower_workers import cleanup_completed_journal
from types import SimpleNamespace
import company_setup
import pytest


def test_bootstrap_checkpoint_retries_windows_reader_lock(tmp_path, monkeypatch):
    target = tmp_path / 'bootstrap.json'
    target.write_text('{"workers": []}', encoding='utf-8')
    original = merge_temu_snapshots.os.replace
    attempts = []
    def occupied(source, destination):
        attempts.append(1)
        if len(attempts) < 3:
            assert json.loads(target.read_text()) == {'workers': []}
            raise PermissionError('Windows reader holds destination')
        return original(source, destination)
    monkeypatch.setattr(merge_temu_snapshots.os, 'replace', occupied)
    atomic_json(target, {'workers': [{'name': 'worker-04-new', 'profile_id': 'created'}]})
    assert len(attempts) == 3
    assert json.loads(target.read_text())['workers'][0]['profile_id'] == 'created'
    assert not list(tmp_path.glob('*.tmp'))


def test_completed_bootstrap_cleanup_lock_does_not_fail_start(tmp_path, monkeypatch):
    target = tmp_path / 'bootstrap.json'
    target.write_text('{"workers": []}')
    original = Path.unlink
    def occupied(self, *args, **kwargs):
        if self == target: raise PermissionError('reader is polling checkpoint')
        return original(self, *args, **kwargs)
    monkeypatch.setattr(Path, 'unlink', occupied)
    cleanup_completed_journal(target)
    assert target.exists()


@pytest.mark.parametrize('error',[
    'Profile ID did not resolve to one unique FlowerBrowser root process.',
    'Selected profile Marionette was not ready before the bounded deadline; browser retained.',
    'Maximum number of active sessions',
])
def test_resume_binding_failure_preserves_same_profile(tmp_path, monkeypatch, error):
    driver=tmp_path/'geckodriver.exe';driver.touch()
    calls=[];binds=[]
    def api(base,path,**kwargs):
        calls.append(path)
        return {'data':{'webdriver':str(driver)}}
    def launch(*args,**kwargs):
        binds.append(args)
        return SimpleNamespace(returncode=1 if len(binds)==1 else 0,
            stderr=error,stdout='')
    monkeypatch.setattr(company_setup,'run_background_launcher',launch)
    with pytest.raises(RuntimeError,match='绑定失败'):
        company_setup.rebind_existing_worker('local',{'name':'worker-01-new','profile_id':'existing'},4455,api)
    assert len(binds)==1
    assert len([x for x in calls if '/stop?' in x])==0
    assert all('user_id=existing' in x and 'create' not in x and 'delete' not in x for x in calls)


def test_prepared_workers_drop_old_captcha_but_keep_collection_checkpoint(tmp_path):
    import selection
    p=tmp_path/'temu/supervisors/worker-03-new.json'
    selection.save(p,{'status':'HUMAN_ACTION_REQUIRED','query_status':'WAITING_CAPTCHA',
        'manual_action_required':True,'current_query':'remote','unique_goods':80,
        'completed_queries':['earlier']})
    selection.reset_prepared_worker_states(tmp_path,['worker-03-new'])
    result=selection.read(p)
    assert result['status']=='STARTING' and result['manual_action_required'] is False
    assert result['query_status'] is None and result['unique_goods']==80
    assert result['completed_queries']==['earlier']
