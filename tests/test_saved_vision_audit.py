import hashlib
import json
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
import audit_saved_vision as audit


def test_label_cannot_affect_replayed_decision(tmp_path,monkeypatch):
    category=tmp_path/'category.json';category.write_text('{}')
    source=tmp_path/'result_fixture.json'
    row={'category_path':'old/path/category.json','category_sha256':hashlib.sha256(category.read_bytes()).hexdigest(),
         'facts':{'visible':'unchanged'},'actual':'SAME','state':'确认同款','reference':'DIFFERENT'}
    calls=[]
    def score(category,facts):
        calls.append(facts.copy());return {'state':'确认同款','reason':'双边具体事实'}
    monkeypatch.setattr(audit,'score',score)
    source.write_text(json.dumps(row),encoding='utf-8');first=audit.replay(source)
    row['reference']='SAME';source.write_text(json.dumps(row),encoding='utf-8');second=audit.replay(source)
    assert first['actual']==second['actual']=='SAME'
    assert first['reference_result']=='MISMATCH' and second['reference_result']=='MATCH'
    assert calls==[{'visible':'unchanged'}]*2
    assert first['api_requests']==0 and not first['production_cache_promoted']


def test_changed_frozen_category_is_rejected_before_score(tmp_path,monkeypatch):
    (tmp_path/'category.json').write_text('{}')
    source=tmp_path/'result_fixture.json';source.write_text(json.dumps({'category_path':'category.json','category_sha256':'wrong','facts':{}}))
    monkeypatch.setattr(audit,'score',lambda *a: (_ for _ in ()).throw(AssertionError('must not score')))
    assert audit.replay(source)['status']=='REJECTED'


def test_empty_directory_does_not_claim_successful_replay():
    report=audit.audit([])
    assert report["empty_input"] and report["input_count"]==0
