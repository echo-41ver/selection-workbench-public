"""Rule content lifecycle; no images, downloads, providers or production files."""
import json
import os
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
import pytest
from test_identity_policy import category
from selection_automation.identity_policy import compile_policy
from selection_automation.rules import read_frozen,text_gate
from selection_workflow.incremental_stage12 import IncrementalStage12


def versions(name):
    a=compile_policy(category(name));b=deepcopy(a)
    b['stage1']['attributes'][0]['hard_conflict']=False
    return a,compile_policy(b)


@pytest.mark.parametrize('name',['灯泡','镜子','花盆'])
def test_same_path_content_edit_refreshes_rules_even_with_same_size_and_mtime(tmp_path,name):
    a,b=versions(name);path=tmp_path/'category.json'
    original=json.dumps(a,ensure_ascii=False)+' ';edited=json.dumps(b,ensure_ascii=False)
    assert len(original.encode())==len(edited.encode())
    path.write_text(original,encoding='utf8');stamp=path.stat()
    assert text_gate(read_frozen(str(path)),'fixed body','jointed body')['excluded']
    path.write_text(edited,encoding='utf8');os.utime(path,ns=(stamp.st_atime_ns,stamp.st_mtime_ns))
    assert not text_gate(read_frozen(str(path)),'fixed body','jointed body')['excluded']


@pytest.mark.parametrize('name',['灯泡','镜子','花盆'])
def test_active_pipeline_refuses_rule_drift_then_reopen_uses_new_rules(tmp_path,name):
    a,b=versions(name);path=tmp_path/'category.json';path.write_text(json.dumps(a),encoding='utf8')
    root=tmp_path/'stage12';v=SimpleNamespace(revision='NO_MODEL_RULE_LIFECYCLE')
    pipeline=IncrementalStage12(root,vectorizer=v,text_gate_name='declarative:'+str(path))
    try:
        pipeline.ensure_processing_contract()
        previous=pipeline.con.execute("SELECT value FROM processing_metadata WHERE key='stage12_contract'").fetchone()[0]
        read_frozen(str(path))
        path.write_text(json.dumps(b),encoding='utf8')
        with pytest.raises(ValueError,match='rule content changed'):pipeline.ensure_processing_contract()
        assert pipeline.con.execute("SELECT value FROM processing_metadata WHERE key='stage12_contract'").fetchone()[0]==previous
    finally:pipeline.close()
    pipeline=IncrementalStage12(root,vectorizer=v,text_gate_name='declarative:'+str(path))
    try:
        assert pipeline.ensure_processing_contract()['changed']
        assert not text_gate(read_frozen(str(path)),'fixed body','jointed body')['excluded']
    finally:pipeline.close()


@pytest.mark.skipif(os.name != 'nt', reason='Windows path compatibility')
def test_long_candidate_export_path_is_explained_without_replacing_previous_file(tmp_path,monkeypatch):
    import selection_workflow.incremental_stage12 as engine
    from selection_workflow.incremental_stage12 import SourceSpec,PathSpec
    v=SimpleNamespace(revision='NO_MODEL_PATH_GUARD')
    pipeline=IncrementalStage12(tmp_path/'db',vectorizer=v)
    pipeline.configure([SourceSpec('a','AMAZON','amazon_generic',tmp_path/'a'),
                        SourceSpec('t','TEMU','temu_frontend',tmp_path/'t')],
                       [PathSpec('kept','a','t','PATH_FIXTURE')])
    old=tmp_path/'layer3_candidates_kept.jsonl';old.write_text('PREVIOUS_CANDIDATES',encoding='utf8')
    monkeypatch.setattr(engine,'unique_temporary_path',lambda destination:tmp_path/('x'*270))
    try:
        with pytest.raises(ValueError,match='较短的安装/输出目录'):
            pipeline.materialize_candidates(tmp_path)
        assert old.read_text(encoding='utf8')=='PREVIOUS_CANDIDATES'
        assert (tmp_path/'db'/'stage12.sqlite3').exists()
    finally:pipeline.close()


@pytest.mark.parametrize('workers,count',[(1,2),(2,256)])
def test_edit_during_batch_cannot_mix_new_rules_into_old_contract_or_cache(tmp_path,monkeypatch,workers,count):
    import numpy as np
    from PIL import Image
    import selection_workflow.incremental_stage12 as engine
    from selection_workflow.incremental_stage12 import SourceSpec,PathSpec
    a,b=versions('unseen_fixture')
    rule=tmp_path/'category.json';rule.write_text(json.dumps(a),encoding='utf8')
    image=tmp_path/'local.png';Image.new('RGB',(16,16),'red').save(image,compress_level=0)
    ap=tmp_path/'amazon.jsonl';tp=tmp_path/'temu.jsonl'
    ap.write_text(json.dumps({'asin':'A','title':'fixed body','local_image_path':str(image)})+'\n',encoding='utf8')
    rows=[{'goods_id':'cached','title':'fixed body','local_image_path':str(image)}]
    tp.write_text(''.join(json.dumps(x)+'\n' for x in rows),encoding='utf8')
    calls=[]
    v=SimpleNamespace(revision='NO_MODEL_BATCH_SNAPSHOT',encode=lambda p:calls.append(p) or np.array([1.,0.]))
    root=tmp_path/'db'
    def opened():
        p=IncrementalStage12(root,vectorizer=v,text_gate_name='declarative:'+str(rule),text_gate_workers=workers)
        p.configure([SourceSpec('a','AMAZON','amazon_generic',ap,'replace'),SourceSpec('t','TEMU','temu_frontend',tp,'replace')],[PathSpec('fixture','a','t','BATCH_SNAPSHOT')])
        return p
    pipeline=opened()
    try:
        pipeline.ingest_source('a');pipeline.ingest_source('t');pipeline.process_batch(1)
        old_contract=pipeline.con.execute("SELECT value FROM processing_metadata WHERE key='stage12_contract'").fetchone()[0]
        old_pair_revision=pipeline.con.execute("SELECT pair_revision FROM pairs WHERE goods_id='cached'").fetchone()[0]
        cached_vectors=pipeline.con.execute('SELECT count(*) FROM vectors').fetchone()[0]
        # Recreate the preceding path-reading engine's actual contract shape.
        # A snapshot-protocol upgrade must requeue its cached local decisions,
        # while retaining immutable image/vector cache and paid pair identity.
        import inspect
        from selection_automation import rules
        prior_gate_revision=engine.digest(engine.stable_json({
            'stage1':a['stage1'],
            'ordinary_variants':sorted(set(a['stage3'].get('variant_fields',[]))-set(a['stage3']['modules'])),
            'engine':engine.digest(Path(inspect.getsourcefile(rules.text_gate)).read_bytes())}))
        legacy=json.loads(old_contract);legacy['text_gate_revision']=prior_gate_revision
        pipeline.con.execute("UPDATE processing_metadata SET value=? WHERE key='stage12_contract'",(engine.stable_json(legacy),));pipeline.con.commit()
        pipeline.close();pipeline=opened()
        migration=pipeline.ensure_processing_contract()
        assert migration['changed'] and migration['rescheduled']==1
        assert pipeline.con.execute('SELECT count(*) FROM candidates').fetchone()[0]==0
        assert pipeline.con.execute('SELECT count(*) FROM vectors').fetchone()[0]==cached_vectors
        assert pipeline.con.execute("SELECT pair_revision FROM pairs WHERE goods_id='cached'").fetchone()[0]==old_pair_revision
        pipeline.process_batch(1)
        old_contract=pipeline.con.execute("SELECT value FROM processing_metadata WHERE key='stage12_contract'").fetchone()[0]
        rows.extend({'goods_id':f'T{i:04}','title':'jointed body','local_image_path':str(image)} for i in range(count))
        tp.write_text(''.join(json.dumps(x)+'\n' for x in rows),encoding='utf8');pipeline.ingest_source('t')
        original_dispatch=pipeline._evaluate_text_gates
        original_worker=engine._evaluate_text_gate_args
        if workers==1:
            evaluations=[]
            def evaluate(values):
                evaluations.append(values)
                if len(evaluations)==2:rule.write_text(json.dumps(b),encoding='utf8')
                return original_worker(values)
            monkeypatch.setattr(engine,'_evaluate_text_gate_args',evaluate)
        else:
            def dispatch(inputs):
                # Simulate a human edit after the batch contract check and just
                # before actual spawned workers start evaluating its input.
                rule.write_text(json.dumps(b),encoding='utf8')
                return original_dispatch(inputs)
            monkeypatch.setattr(pipeline,'_evaluate_text_gates',dispatch)
        pipeline.process_batch(count)
        states=dict(pipeline.con.execute("SELECT goods_id,status FROM pairs"))
        assert states['cached']=='CANDIDATE'
        assert all(states[f'T{i:04}']=='STAGE1_EXCLUDED' for i in range(count))
        assert pipeline.con.execute("SELECT value FROM processing_metadata WHERE key='stage12_contract'").fetchone()[0]==old_contract
        if workers>1:assert pipeline._text_gate_pool is not None and len(pipeline._text_gate_pool._processes)==2
        export=pipeline.materialize_candidates(tmp_path/'old_export')
        assert export['fixture_count']==1
        with pytest.raises(ValueError,match='rule content changed'):pipeline.process_batch(count)
        assert pipeline.con.execute('SELECT count(*) FROM vectors').fetchone()[0]==cached_vectors
    finally:pipeline.close()
    monkeypatch.setattr(engine,'_evaluate_text_gate_args',original_worker)
    pipeline=opened()
    try:
        changed=pipeline.ensure_processing_contract();assert changed['changed']
        assert pipeline.con.execute("SELECT value FROM processing_metadata WHERE key='stage12_contract'").fetchone()[0]!=old_contract
        pipeline.process_batch(count+1)
        assert {r[0] for r in pipeline.con.execute('SELECT status FROM pairs')}=={'CANDIDATE'}
        assert pipeline.con.execute("SELECT pair_revision FROM pairs WHERE goods_id='cached'").fetchone()[0]==old_pair_revision
        assert pipeline.con.execute('SELECT count(*) FROM vectors').fetchone()[0]==cached_vectors
        assert pipeline.materialize_candidates(tmp_path/'new_export')['fixture_count']==count+1
    finally:pipeline.close()
