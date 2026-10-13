import importlib
import json
from pathlib import Path
import sys

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
from test_codex_visual_review import fixture
from test_set_inventory_identity import configured


def setup(tmp_path,monkeypatch,count=2):
    watcher=importlib.import_module('watch_codex_visual_reviews')
    category=configured()
    base=tmp_path/'runs'/category['category_key']
    folder=base/'stage123/codex_review'
    folder.mkdir(parents=True)
    for index in range(1,count+1):
        pair,row=fixture()
        (folder/f'manifest_{index:02}.json').write_text(json.dumps({'pairs':[pair]}),encoding='utf-8')
        (folder/f'raw_{index:02}.jsonl').write_text(json.dumps(row)+'\n',encoding='utf-8')
    monkeypatch.setattr(watcher,'ROOT',tmp_path)
    monkeypatch.setattr(watcher,'load_category',lambda _:category)
    calls=[]
    monkeypatch.setattr(watcher,'ensure_publisher',lambda *a,**kw:calls.append('publisher'))
    monkeypatch.setattr(watcher,'publication_current',lambda *a:False)
    return watcher,base,folder,calls


def test_watcher_imports_independently_and_keeps_publisher_until_final_signature(tmp_path,monkeypatch):
    watcher,base,folder,calls=setup(tmp_path,monkeypatch)
    state=watcher.tick(tmp_path/'category.json',2)
    assert state['completed']==2 and state['all_reviews_complete'] and not state['done']
    output=base/'stage123/codex_reviews/codex-01.jsonl'
    before=output.stat().st_mtime_ns
    watcher.tick(tmp_path/'category.json',2)
    assert output.stat().st_mtime_ns==before and len(calls)==2
    monkeypatch.setattr(watcher,'publication_current',lambda *a:True)
    assert watcher.tick(tmp_path/'category.json',2)['done']


def test_missing_raw_and_invalid_task_do_not_stop_other_tasks(tmp_path,monkeypatch):
    watcher,base,folder,_=setup(tmp_path,monkeypatch,3)
    (folder/'raw_02.jsonl').unlink()
    (folder/'raw_03.jsonl').write_text('broken\n',encoding='utf-8')
    state=watcher.tick(tmp_path/'category.json',3)
    assert state['completed']==1 and not state['done'] and state['errors']
    assert (base/'stage123/codex_reviews/codex-01.jsonl').exists()


def test_unfinished_tail_waits_without_fabricating_review(tmp_path,monkeypatch):
    watcher,_,folder,_=setup(tmp_path,monkeypatch,1)
    (folder/'raw_01.jsonl').write_text('{"asin":',encoding='utf-8')
    state=watcher.tick(tmp_path/'category.json',1)
    assert state['completed']==0 and not state['done']
    assert state['tasks'][0]['unfinished_tail']


def test_invalid_revision_row_is_recorded_but_valid_other_rows_import(tmp_path,monkeypatch):
    watcher,_,folder,_=setup(tmp_path,monkeypatch,1)
    pair,row=fixture()
    bad={**row,'goods_id':'other'}
    with (folder/'raw_01.jsonl').open('a',encoding='utf-8') as stream:
        stream.write(json.dumps(bad)+'\n')
    state=watcher.tick(tmp_path/'category.json',1)
    assert state['completed']==1
    assert state['tasks'][0]['errors'][0]['error']=='pair absent from task manifest'


def test_watch_holds_unique_lock_and_writes_pid_creation_metadata(tmp_path,monkeypatch):
    watcher,_,folder,_=setup(tmp_path,monkeypatch,1)
    monkeypatch.setattr(watcher,'publication_current',lambda *a:True)
    state=watcher.watch(tmp_path/'category.json',expected_tasks=1)
    metadata=json.loads((folder/'consumer.json').read_text(encoding='utf-8'))
    assert state['done'] and metadata['pid']>0 and metadata['created']>0
    assert (folder/'consumer.lock').exists()


def test_external_batch_tracks_current_revision_without_reimporting_old_tasks(tmp_path,monkeypatch):
    watcher,base,_,_=setup(tmp_path,monkeypatch,1)
    batch=base/'nightly_review'/'new-batch'
    batch.mkdir(parents=True)
    pair,_=fixture()
    pair_file=batch/'manifest.jsonl'
    pair_file.write_text(json.dumps(pair)+'\n',encoding='utf-8')
    output=base/'stage123/codex_reviews/new.jsonl'
    output.parent.mkdir(parents=True)
    stale={**pair,'vision_revision_key':'old-key','review_status':'DIFFERENT'}
    output.write_text(json.dumps(stale)+'\n',encoding='utf-8')
    (batch/'review-batch.json').write_text(json.dumps({'tasks':[{
        'task_id':'new','manifest':str(pair_file),'output':str(output)}]}),encoding='utf-8')
    before=output.read_bytes()
    state=watcher.tick(tmp_path/'category.json',1,batch)
    assert state['target']==1 and state['completed']==0 and not state['all_reviews_complete']
    assert output.read_bytes()==before
    assert not (base/'stage123/codex_reviews/codex-01.jsonl').exists()
    current={**pair,'review_status':'UNKNOWN'}
    output.write_text(json.dumps(current)+'\n'+json.dumps(current)+'\n',encoding='utf-8')
    state=watcher.tick(tmp_path/'category.json',1,batch)
    assert state['completed']==1 and state['all_reviews_complete'] and not state['done']
    monkeypatch.setattr(watcher,'publication_current',lambda *a:True)
    assert watcher.tick(tmp_path/'category.json',1,batch)['done']


def test_external_batch_rejects_output_outside_current_review_directory(tmp_path,monkeypatch):
    watcher,base,_,_=setup(tmp_path,monkeypatch,1)
    batch=base/'nightly_review'/'new-batch'
    batch.mkdir(parents=True)
    (batch/'review-batch.json').write_text(json.dumps({'tasks':[{
        'task_id':'foreign','manifest':str(batch/'manifest.jsonl'),'output':str(tmp_path/'foreign.jsonl')}]}),encoding='utf-8')
    state=watcher.tick(tmp_path/'category.json',1,batch)
    assert state['errors'] and state['completed']==0 and not state['all_reviews_complete']


def test_relative_category_recognizes_absolute_publisher_signature(tmp_path,monkeypatch):
    watcher=importlib.import_module('watch_codex_visual_reviews')
    base=tmp_path/'runs'/'example'
    base.mkdir(parents=True)
    category_path=base/'category.json'
    category_path.write_text('{}',encoding='utf-8')
    current=watcher.signature(watcher.source_paths(category_path,base))
    (base/'publish_state.json').write_text(json.dumps({
        'stage3_excel':current,'deliveries':{'layer123':{'file':'final.xlsx'}}}),encoding='utf-8')
    monkeypatch.chdir(tmp_path)
    assert watcher.publication_current(Path('runs/example/category.json'),Path('runs/example'))


def test_relative_category_is_normalized_before_starting_publisher(tmp_path,monkeypatch):
    watcher,base,_,_=setup(tmp_path,monkeypatch,0)
    monkeypatch.chdir(tmp_path)
    received=[]
    monkeypatch.setattr(watcher,'ensure_publisher',lambda category_path,**kw:received.append(category_path))
    watcher.tick(Path('category.json'),0)
    assert received==[tmp_path/'category.json']
