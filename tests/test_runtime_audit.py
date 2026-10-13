import json
from selection_automation.runtime_audit import process_version,record_start,recovery_event


def test_loaded_version_is_immutable_after_source_update(tmp_path):
    scripts=tmp_path/'scripts';scripts.mkdir();source=scripts/'worker.py';source.write_text('x=1')
    old=dict(process_version(str(tmp_path)));source.write_text('x=2')
    assert process_version(str(tmp_path))==old
    payload=record_start(tmp_path,tmp_path/'run','fixture',{'category':'bowl'})
    assert payload['source_sha256']==old['source_sha256']
    assert len(payload['config_sha256'])==64
    recovery_event(tmp_path/'run','w','AUTOMATIC_RECOVERY_STARTED',recovery_id='one')
    recovery_event(tmp_path/'run','w','RECOVERY_FIRST_NEW_PRODUCT',recovery_id='one',unique_goods=20)
    lines=(tmp_path/'run/temu/workers/w/recovery_events.jsonl').read_text().splitlines()
    assert [json.loads(line)['recovery_id'] for line in lines]==['one','one']


def test_extracted_package_uses_its_build_identity_not_parent_git(tmp_path):
    package=tmp_path/'package';package.mkdir()
    (package/'BUILD_INFO.json').write_text(json.dumps({'commit':'released-commit'}))
    assert process_version(str(package))['commit']=='released-commit'
