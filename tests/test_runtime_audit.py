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


def test_runtime_head_reads_loose_packed_and_worktree_refs_without_subprocess(tmp_path,monkeypatch):
    from selection_automation import runtime_audit as module
    monkeypatch.setattr(module.subprocess,'run',lambda *a,**k:(_ for _ in ()).throw(AssertionError('Runtime must not spawn Git')))
    git=tmp_path/'.git';(git/'refs/heads').mkdir(parents=True)
    sha='a'*40;(git/'HEAD').write_text('ref: refs/heads/main\n');ref=git/'refs/heads/main';ref.write_text(sha)
    assert module.disk_version(tmp_path)['commit']==sha
    ref.unlink();(git/'packed-refs').write_text(sha+' refs/heads/main\n');assert module.disk_version(tmp_path)['commit']==sha
    worktree=tmp_path/'worktree';worktree.mkdir();meta=git/'worktrees/test';meta.mkdir(parents=True)
    (meta/'HEAD').write_text('ref: refs/heads/main');(meta/'commondir').write_text('../..')
    (worktree/'.git').write_text('gitdir: '+str(meta));assert module.disk_version(worktree)['commit']==sha
    (meta/'HEAD').write_text('b'*40);assert module.disk_version(worktree)['commit']=='b'*40
