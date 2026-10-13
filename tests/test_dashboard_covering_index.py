from selection_workflow.incremental_stage12 import IncrementalStage12


def test_dashboard_counts_use_covering_index_and_preserve_totals(tmp_path):
    engine=IncrementalStage12(tmp_path/'run')
    try:
        states=[('PENDING',None),('CANDIDATE','KEEP'),('BELOW_VECTOR_THRESHOLD','KEEP'),('FAILED_RETRYABLE','KEEP')]
        for i,(status,gate) in enumerate(states):
            engine.con.execute('INSERT INTO pairs(path_key,asin,goods_id,amazon_version,temu_version,pair_revision,status,stage1_status,updated_at) VALUES(?,?,?,?,?,?,?,?,?)',('path',str(i),'T','A','T','rev',status,gate,'now'))
        engine.con.commit()
        query='SELECT status,stage1_status,COUNT(*) FROM pairs GROUP BY status,stage1_status'
        assert sum(row[2] for row in engine.con.execute(query))==len(states)
        plan=' '.join(row[3] for row in engine.con.execute('EXPLAIN QUERY PLAN '+query))
        assert 'COVERING INDEX idx_pairs_status_gate' in plan
        assert 'TEMP B-TREE' not in plan
    finally:engine.close()
