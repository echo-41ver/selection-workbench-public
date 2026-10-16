import inspect
import json
from pathlib import Path
import sqlite3
import sys
from types import SimpleNamespace

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
import local_service_recovery as recovery


def fixture(tmp_path, **options):
    base=tmp_path/'runs/task';(base/'stage12').mkdir(parents=True,exist_ok=True)
    (base/'stage12/stage12.sqlite3').touch()
    (base/'category.json').write_text('{}')
    calls=[]
    watcher=recovery.LocalServiceRecovery(tmp_path,lambda:{'category_key':'task'},
        launch=lambda command,log:(calls.append(command) or SimpleNamespace(poll=lambda:None)),
        alive=options.pop('alive',lambda *args:False),
        contract=options.pop('contract',lambda *args:(True,'same')),
        child_running=lambda child:child.poll() is None)
    return base,watcher,calls


def test_dead_local_watchers_restart_without_starting_collection_or_models(tmp_path):
    base,watcher,calls=fixture(tmp_path)
    watcher.step();watcher.step()
    assert len(calls)==2
    assert Path(calls[0][1]).name=='watch_temu_union.py'
    assert Path(calls[1][1]).name=='run_incremental_stage12.py'
    assert calls[1][2]=='watch'
    assert all('stage3' not in str(arg) and 'collect' not in str(arg) for cmd in calls for arg in cmd)
    assert json.loads((base/'local_service_recovery.json').read_text('utf-8'))['model_requests']==0


def test_live_or_unknown_owned_service_is_never_restarted(tmp_path):
    for live in (True,None):
        base,watcher,calls=fixture(tmp_path,alive=lambda *args:live)
        watcher.step();assert not calls


def test_explicit_stop_and_local_stage12_stop_are_respected(tmp_path):
    base,watcher,calls=fixture(tmp_path)
    (base/'STOP').touch();watcher.step();assert not calls
    (base/'STOP').unlink();(base/'stage12/STOP').touch();watcher.step()
    assert len(calls)==1 and 'watch_temu_union' in calls[0][1]


def test_rule_change_blocks_reopen_instead_of_rescheduling_existing_candidates(tmp_path):
    base,watcher,calls=fixture(tmp_path,contract=lambda *args:(False,'规则变化，需要确认'))
    watcher.step()
    assert len(calls)==1
    state=json.loads((base/'local_service_recovery.json').read_text('utf-8'))
    assert state['services']['stage12']['human_action_required']
    assert state['services']['stage12']['status']=='ACTION_REQUIRED'


def test_contract_guard_is_read_only_and_detects_only_relevant_changes(tmp_path,monkeypatch):
    from selection_automation import rules
    from selection_workflow.incremental_stage12 import digest,stable_json,VECTOR_REVISION
    base=tmp_path/'runs/task';out=base/'stage12';out.mkdir(parents=True,exist_ok=True)
    category={'stage1':{'attributes':[]},'stage3':{'variant_fields':[],'modules':{}}}
    frozen=base/'category.json';frozen.write_text('{}')
    monkeypatch.setattr(rules,'read_frozen',lambda path:category)
    config={'output_dir':str(out),'text_gate':'declarative:'+str(frozen),
            'stage12_rule_version':'test','candidate_threshold':60}
    (base/'stage12_config.json').write_text(json.dumps(config))
    revision=digest(stable_json({'stage1':category['stage1'],'snapshot_protocol':'declarative-content-v1',
        'ordinary_variants':[],'engine':digest(Path(inspect.getsourcefile(rules.text_gate)).read_bytes())}))
    stored={'schema':'stage12-processing-contract-v1','text_gate':config['text_gate'],'text_gate_revision':revision,
        'rule_version':'test','vector_revision':VECTOR_REVISION,'candidate_threshold':60.0,
        'cosine_policy':'float64-normalized-unrounded-threshold-v2'}
    database=out/'stage12.sqlite3';con=sqlite3.connect(database)
    con.execute('create table processing_metadata(key text,value text)')
    con.execute('insert into processing_metadata values(?,?)',('stage12_contract',json.dumps(stored)))
    con.commit();con.close();original=database.read_bytes()
    assert recovery.stage12_contract_unchanged(tmp_path,base)[0]
    assert database.read_bytes()==original
    config['candidate_threshold']=70;(base/'stage12_config.json').write_text(json.dumps(config))
    assert not recovery.stage12_contract_unchanged(tmp_path,base)[0]
    assert database.read_bytes()==original
