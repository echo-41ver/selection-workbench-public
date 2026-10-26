import json
from pathlib import Path
import sys

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
import dashboard_progress as dashboard


def test_async_progress_retains_snapshot_without_blocking_live_state(tmp_path,monkeypatch):
    import threading,time
    started=threading.Event();release=threading.Event();calls=[]
    entry={'checked':float('-inf'),'metrics':{'amazon':{'actual_count':2}},'snapshot_at':123}
    dashboard._CACHE[str(tmp_path.resolve())]=entry
    def collect(*args):
        calls.append(True);started.set();release.wait(3)
        return {'amazon':{'actual_count':3}}
    monkeypatch.setattr(dashboard,'_collect',collect)
    try:
        result=dashboard.enrich_progress(tmp_path,{'状态':'live'},asynchronous=True)
        assert started.wait(1)
        assert result['状态']=='live' and result['阶段']['amazon']['actual_count']==2
        assert result['progress_snapshot']=={'updated_at':123,'refreshing':True,
            'scope':'最近本地进度快照；实时采集状态仍读取监督器'}
        assert dashboard.enrich_progress(tmp_path,{},asynchronous=True)['阶段']['amazon']['actual_count']==2
        assert calls==[True]
    finally:release.set()
    deadline=time.monotonic()+1
    while entry['refreshing'] and time.monotonic()<deadline:time.sleep(.01)
    assert entry['metrics']['amazon']['actual_count']==3 and entry['snapshot_at']>123


def put(path,value):
    path.parent.mkdir(parents=True,exist_ok=True);path.write_text(json.dumps(value),encoding='utf-8')


def test_actual_snapshots_are_counted_without_mutating_state(tmp_path):
    put(tmp_path/'amazon/amazon_frontend_latest.json',{'records':[{},{}],'target_count':10,'status':'PARTIAL'})
    put(tmp_path/'stage12/layer12_summary_fixture.json',{'states':{'PENDING':100,'CANDIDATE':2,'STAGE1_EXCLUDED':3,'FAILED_RETRYABLE':4},'candidates':2})
    (tmp_path/'excel').mkdir();(tmp_path/'excel/final.xlsx').write_bytes(b'fixture');(tmp_path/'excel/final_data.xlsx').write_bytes(b'fixture')
    original={'阶段':{'amazon':{'状态':'运行中'}}}
    result=dashboard.enrich_progress(tmp_path,original)['阶段']
    assert result['amazon']['actual_count']==2 and result['amazon']['target_count']==10
    assert result['amazon']['状态']=='运行中'
    assert result['stage12']['processed_pairs']==5 and result['stage12']['candidate_count']==2
    assert result['excel']['actual_count']==1
    assert original=={'阶段':{'amazon':{'状态':'运行中'}}}


def test_missing_evidence_does_not_create_zero_counts(tmp_path):
    assert dashboard.enrich_progress(tmp_path,{'阶段':{'stage3':{'状态':'等待候选'}}})['阶段']=={'stage3':{'状态':'等待候选'}}


def test_amazon_detail_queue_does_not_show_rank_rows_as_completed_details(tmp_path):
    path=tmp_path/'amazon/amazon_frontend_latest.json'
    put(path,{'records':[{}, {}, {}, {}],'target_count':4})
    put(path.with_name(path.name+'.checkpoint.json'),{'detail_queue':{'items':[
        {'status':'COMPLETE'},{'status':'RETRY_PENDING'},
        {'status':'MANUAL_ACTION_REQUIRED'},{'status':'EXHAUSTED'}]}})
    row=dashboard.enrich_progress(tmp_path,{})['阶段']['amazon']
    assert (row['actual_count'],row['completed'],row['remaining'])==(4,1,3)
    assert row['detail_retry_pending']==row['detail_manual_count']==row['detail_exhausted_count']==1
    assert row['状态']=='正在获取详情或自动队尾补采'


def test_amazon_detail_queue_surfaces_block_after_automatic_retries_end(tmp_path):
    path=tmp_path/'amazon/amazon_frontend_latest.json'
    put(path,{'records':[{},{}],'target_count':2,'detail_enrichment':{'queue':{'items':[
        {'status':'COMPLETE'},{'status':'MANUAL_ACTION_REQUIRED'}]}}})
    row=dashboard.enrich_progress(tmp_path,{})['阶段']['amazon']
    assert row['completed']==1 and row['detail_retry_pending']==0
    assert row['manual_action_required'] is True
    assert row['状态']=='部分详情需要处理'


def test_public_original_images_are_not_counted_as_complete_details(tmp_path):
    path=tmp_path/'amazon/amazon_frontend_latest.json'
    put(path,{'records':[{'public_image_evidence':{'verified':True}}],'target_count':1,
              'detail_enrichment':{'queue':{'items':[{'status':'MANUAL_ACTION_REQUIRED'}]}}})
    row=dashboard.enrich_progress(tmp_path,{})['阶段']['amazon']
    assert row['public_original_image_count']==1
    assert row['completed']==0


def test_completed_amazon_clears_only_its_own_stale_failure(tmp_path):
    path=tmp_path/'amazon/amazon_frontend_latest.json'
    put(path,{'records':[{},{}],'target_count':2,'status':'COMPLETE','details_complete':True,
              'detail_enrichment':{'queue':{'items':[{'status':'COMPLETE'},{'status':'COMPLETE'}]}}})
    original={'状态':'存在阻断','时间':0,'阻断阶段':['amazon','market'],
              '人工介入阶段':['amazon','market'],'需人工介入':True,
              '建议动作':{'amazon':'旧失败','market':'需要配置'},'阶段':{'amazon':{'状态':'失败，需要处理','退出码':2}}}
    result=dashboard.enrich_progress(tmp_path,original)
    assert result['阶段']['amazon']['状态']=='本轮完成'
    assert result['阶段']['amazon']['退出码']==0
    assert result['阻断阶段']==['market'] and result['状态']=='存在阻断'
    assert result['人工介入阶段']==['market'] and result['需人工介入']
    assert result['建议动作']=={'market':'需要配置'}
    original.update(阻断阶段=['amazon'],人工介入阶段=['amazon'])
    recovered=dashboard.enrich_progress(tmp_path,original)
    assert recovered['状态']=='阻断已解除，最新进度见下方' and not recovered['需人工介入']
    assert original['阶段']['amazon']['退出码']==2


def test_result_pairs_not_requests_incremental_and_throttled(tmp_path,monkeypatch):
    clock=[0];monkeypatch.setattr(dashboard.time,'monotonic',lambda:clock[0])
    path=tmp_path/'stage123/results.jsonl';path.parent.mkdir()
    def row(goods,status):return json.dumps({'asin':'A','goods_id':goods,'vision_revision_key':goods,'api_status':status})+'\n'
    path.write_text(row('1','failed')+row('1','success')+row('1','failed')+row('2','failed'),encoding='utf-8')
    put(path.with_name('requests.jsonl'),{'requests':9999})
    first=dashboard.enrich_progress(tmp_path,{})['阶段']['stage3']
    assert first['completed_pairs']==1 and first['failed_pairs']==1
    offset=dashboard._CACHE[str(tmp_path.resolve())]['results']['offset']
    with path.open('a',encoding='utf-8') as stream:stream.write(row('2','success'))
    assert dashboard.enrich_progress(tmp_path,{})['阶段']['stage3']['completed_pairs']==1
    clock[0]=11
    second=dashboard.enrich_progress(tmp_path,{})['阶段']['stage3']
    assert second['completed_pairs']==2 and second['failed_pairs']==0
    assert dashboard._CACHE[str(tmp_path.resolve())]['results']['offset']>offset


def test_partial_tail_not_counted_until_complete(tmp_path,monkeypatch):
    clock=[0];monkeypatch.setattr(dashboard.time,'monotonic',lambda:clock[0])
    path=tmp_path/'stage123/results.jsonl';path.parent.mkdir();path.write_text('{"asin":"A"',encoding='utf-8')
    first=dashboard.enrich_progress(tmp_path,{})['阶段']['stage3']
    assert first['metrics_catching_up'] and 'completed_pairs' not in first
    with path.open('a',encoding='utf-8') as stream:stream.write(',"goods_id":"T","api_status":"success"}\n')
    clock[0]=11
    assert dashboard.enrich_progress(tmp_path,{})['阶段']['stage3']['completed_pairs']==1


def test_completed_lane_progress_accumulates_all_passes_not_unique_or_pending_assignments(tmp_path):
    name='worker-02-new';output=tmp_path/'temu/workers'/name/'kettle--recent'
    put(tmp_path/'temu/supervisors'/f'{name}.json',{'status':'WORKER_COMPLETE','current_sort':'recent','output':str(output)})
    put(tmp_path/'temu/supervisors/assignments.json',{'schema_version':'temu-worker-assignments-v3',
        'workers':[{'name':name,'passes':[]}],'jobs':[{'query':'kettle','max_products':400,
            'sort_passes':{'sales':{'owner':name,'complete':True},'recent':{'owner':name,'complete':True}}}]})
    put(output.with_name('kettle')/'progress.json',{'status':'KEYWORD_LIMIT_REACHED','search_query':'kettle',
        'sales_sort':{'requested':'sales'},'raw_collected_count':400,'unique_goods':120})
    for raw in (400,200):
        put(output/'progress.json',{'status':'KEYWORD_LIMIT_REACHED' if raw==400 else 'KEYWORD_COMPLETE',
            'completion_basis':'NO_MORE_RESULTS_CONFIRMED' if raw<400 else 'CONFIGURED_PRODUCT_LIMIT_REACHED',
            'search_query':'kettle','sales_sort':{'requested':'recent'},'raw_collected_count':raw,'unique_goods':205})
        row=dashboard._temu(tmp_path,{'temu':{'max_products_per_keyword':400}})['workers'][0]
        assert row['completed']==400+raw and row['total']==400+raw and row['percent']==100
        assert row['lane_planned_total']==800 and row['lane_shortfall']==400-raw
        assert row['last_pass_raw_count']==raw
        assert row['current_query']=='kettle' and row['total_keywords']==2


def test_database_separates_text_completion_from_failed_images(tmp_path):
    import sqlite3
    with sqlite3.connect(tmp_path/'stage12.sqlite3') as con:
        con.execute('CREATE TABLE pairs(status TEXT,stage1_status TEXT)')
        con.executemany('INSERT INTO pairs VALUES(?,?)', [('PENDING',None),('STAGE1_EXCLUDED','TEXT_HARD_EXCLUDE'),('FAILED_RETRYABLE','TEXT_PASS'),('WAIT_IMAGE_SOURCE','TEXT_INSUFFICIENT'),('CANDIDATE','TEXT_PASS')])
    result=dashboard._database(tmp_path)
    assert result['stage1']['completed']==4 and result['stage1']['total']==5
    assert result['stage2']['completed']==1 and result['stage2']['total']==3
    assert result['stage2']['remaining']==2 and result['stage2']['failed']==1


def image_action_database(stage12,*,classification='ACTION_INVALID_IMAGE',queue_status='ACTION_REQUIRED'):
    """The source/version and asset identity used by the real processing tables."""
    import hashlib,sqlite3
    stage12.mkdir(parents=True,exist_ok=True)
    con=sqlite3.connect(stage12/'stage12.sqlite3')
    con.executescript('''
        CREATE TABLE paths(path_key TEXT,amazon_source TEXT,temu_source TEXT);
        CREATE TABLE products(source_key TEXT,product_id TEXT,evidence_hash TEXT,record_json TEXT);
        CREATE TABLE pairs(path_key TEXT,asin TEXT,goods_id TEXT,amazon_version TEXT,temu_version TEXT,status TEXT,stage1_status TEXT);
        CREATE TABLE image_retry_queue(image_key TEXT,status TEXT,last_classification TEXT);
    ''')
    con.execute('INSERT INTO paths VALUES(?,?,?)',('path','amazon','temu'))
    ref='https://example.invalid/current-image.jpg';key=hashlib.sha256(ref.encode()).hexdigest()
    con.execute('INSERT INTO products VALUES(?,?,?,?)',('temu','T','v1',json.dumps({'main_image_url':ref})))
    con.execute('INSERT INTO image_retry_queue VALUES(?,?,?)',(key,queue_status,classification))
    con.executemany('INSERT INTO pairs VALUES(?,?,?,?,?,?,?)',[
        ('path',f'A{i}','T','av1','v1','FAILED_IMAGE_ACTION','TEXT_PASS') for i in range(10)])
    con.executemany('INSERT INTO pairs VALUES(?,?,?,?,?,?,?)',[
        ('path','complete','other','av1','tv1',state,'TEXT_PASS') for state in ('CANDIDATE','BELOW_VECTOR_THRESHOLD')])
    con.commit();return con,key


def test_terminal_current_image_source_is_actionable_without_counting_failure_as_success(tmp_path):
    con,_=image_action_database(tmp_path);con.close()
    before=(tmp_path/'stage12.sqlite3').read_bytes()
    row=dashboard._database(tmp_path)['stage2']
    assert (row['completed'],row['total'],row['remaining'],row['failed'],row['waiting'])==(2,12,10,10,0)
    assert row['manual_action_required'] is True
    assert (row['image_action_products'],row['image_action_assets'],row['image_action_pairs'])==(1,1,10)
    assert row['image_action_reasons']=={'ACTION_INVALID_IMAGE':1}
    assert '1 件商品' in row['状态'] and '图片当前无法完整解码' in row['状态'] and '10 对' in row['状态']
    assert '来源损坏' not in row['状态']
    assert '停止' in row['状态'] and '自动重试' in row['状态']
    assert '维护' in row['建议动作'] and '日志' not in row['建议动作']
    assert (tmp_path/'stage12.sqlite3').read_bytes()==before


def test_incomplete_progress_cannot_round_up_to_100_percent():
    assert dashboard._bar(52770,52780,'fixture')['percent']==99.9
    assert dashboard._bar(52780,52780,'fixture')['percent']==100


def test_stale_queue_wrong_source_or_version_cannot_create_current_image_alert(tmp_path):
    con,key=image_action_database(tmp_path)
    con.execute("UPDATE products SET evidence_hash='v2'")
    con.execute("INSERT INTO products SELECT 'unrelated',product_id,'v1',record_json FROM products")
    con.commit()
    row=dashboard._database(tmp_path)['stage2']
    assert not row.get('manual_action_required') and row['image_action_pairs']==0
    # The current product is updated to an unrelated asset; the old queue remains.
    con.execute("UPDATE products SET evidence_hash='v1',record_json=? WHERE source_key='temu'",
                (json.dumps({'main_image_url':'https://example.invalid/new.jpg'}),))
    con.commit();con.close()
    row=dashboard._database(tmp_path)['stage2']
    assert not row.get('manual_action_required') and row['image_action_assets']==0
    assert row['completed']==2 and row['failed']==10


def test_both_failed_images_count_affected_pair_once_and_classifications_stay_accurate(tmp_path):
    import hashlib
    con,_=image_action_database(tmp_path,classification='ACTION_AUTH')
    ref='C:/current-local-image.jpg';key=hashlib.sha256(ref.encode()).hexdigest()
    con.execute('INSERT INTO products VALUES(?,?,?,?)',('amazon','A0','av1',json.dumps({'local_image_path':ref,'main_image_url':'ignored'})))
    con.execute('INSERT INTO image_retry_queue VALUES(?,?,?)',(key,'ACTION_REQUIRED','ACTION_TLS'))
    con.commit();con.close()
    row=dashboard._database(tmp_path)['stage2']
    assert (row['image_action_products'],row['image_action_assets'],row['image_action_pairs'])==(2,2,10)
    assert row['image_action_reasons']=={'ACTION_AUTH':1,'ACTION_TLS':1}
    assert '访问被拒绝' in row['状态'] and '证书校验失败' in row['状态']
    assert '损坏' not in row['状态']


def test_active_network_retry_or_ready_asset_is_not_terminal_action(tmp_path):
    con,_=image_action_database(tmp_path,classification='TRANSIENT_NETWORK',queue_status='RETRY')
    for state in ('RETRY','RUNNING','READY','INACTIVE'):
        con.execute('UPDATE image_retry_queue SET status=?',(state,));con.commit()
        row=dashboard._database(tmp_path)['stage2']
        assert not row.get('manual_action_required') and row['image_action_pairs']==0
    con.close()


def test_image_recovery_removes_only_owned_notice_and_preserves_other_stage_failure(tmp_path,monkeypatch):
    con,_=image_action_database(tmp_path/'stage12')
    monkeypatch.setattr(dashboard,'_collect',lambda base,entry:dashboard._database(base/'stage12'))
    original={'阶段':{'stage2':{'状态':'运行中'},'market':{'状态':'需要登录','manual_action_required':True}}}
    failed=dashboard.enrich_progress(tmp_path,original)
    assert failed['阶段']['stage2']['manual_action_required']
    con.execute("UPDATE image_retry_queue SET status='READY'")
    con.execute("UPDATE pairs SET status='CANDIDATE' WHERE status='FAILED_IMAGE_ACTION'");con.commit();con.close()
    dashboard._CACHE[str(tmp_path.resolve())]['checked']=float('-inf')
    recovered=dashboard.enrich_progress(tmp_path,failed)
    row=recovered['阶段']['stage2']
    assert row['completed']==row['total']==12 and row['remaining']==0
    assert not row.get('manual_action_required') and not row.get('image_action_required')
    assert '状态' not in row and '建议动作' not in row
    assert recovered['阶段']['market']==original['阶段']['market']
    assert failed['阶段']['stage2']['manual_action_required'] is True


def test_unavailable_database_does_not_clear_last_confirmed_image_action(tmp_path,monkeypatch):
    con,_=image_action_database(tmp_path/'stage12');con.close()
    monkeypatch.setattr(dashboard,'_collect',lambda base,entry:dashboard._database(base/'stage12'))
    failed=dashboard.enrich_progress(tmp_path,{})
    assert failed['阶段']['stage2']['manual_action_required']
    # A failed bounded refresh is not evidence that the source was repaired.
    monkeypatch.setattr(dashboard,'_collect',lambda base,entry:{'stage2':{'completed':None,'total':None,'reason':'数据库忙'}})
    dashboard._CACHE[str(tmp_path.resolve())]['checked']=float('-inf')
    unavailable=dashboard.enrich_progress(tmp_path,failed)['阶段']['stage2']
    assert unavailable['manual_action_required'] and unavailable['image_action_required']
    assert unavailable['状态']==failed['阶段']['stage2']['状态']


def test_current_visual_versions_exclude_stale_success(tmp_path,monkeypatch):
    from selection_automation import vision
    put(tmp_path/'category.json',{'category_key':'fixture'})
    image=tmp_path/'image';image.write_bytes(b'fixture')
    candidates=tmp_path/'layer3_candidates_fixture.jsonl'
    candidates.write_text(json.dumps({'asin':'A','goods_id':'T','amazon_image_path':str(image),'temu_image_path':str(image)})+'\n',encoding='utf-8')
    monkeypatch.setattr(vision,'compatible_vision_keys',lambda *args:('new','newlegacy'))
    state={'revisions':{(('A','T'),'old'):'success'}}
    assert dashboard._current_vision(tmp_path,tmp_path,state)['completed']==0
    state['revisions'][(('A','T'),'new')]='success'
    assert dashboard._current_vision(tmp_path,tmp_path,state)['completed']==1


def test_temu_saturation_not_keyword_complete(tmp_path):
    put(tmp_path/'temu/supervisors/assignments.json',{'workers':[{'name':'worker-01-new','queries':['drill','cordless drill']}]})
    put(tmp_path/'temu/supervisors/worker-01-new.json',{'current_query':'drill','assigned':2})
    put(tmp_path/'temu/workers/worker-01-new/drill/progress.json',{'search_query':'drill','status':'SESSION_SATURATED','unique_goods':40,'raw_collected_count':40})
    worker=dashboard._temu(tmp_path,{'temu':{'max_products_per_keyword':100}})['workers'][0]
    assert worker['completed']==40 and worker['remaining']==160
    assert worker['completed_keywords']==0 and worker['total_keywords']==2


def test_broad_query_progress_uses_dispatched_triple_budget(tmp_path):
    put(tmp_path/'temu/supervisors/worker-01-new.json',{'current_query':'fuel transfer pump','assigned':2})
    put(tmp_path/'temu/workers/worker-01-new/fuel-transfer-pump/progress.json',
        {'search_query':'fuel transfer pump','status':'RUNNING','unique_goods':304,'raw_collected_count':304})
    put(tmp_path/'temu/supervisors/assignments.json',{'jobs':[{'query':'fuel transfer pump','owner':'worker-01-new','max_products':1200}]})
    worker=dashboard._temu(tmp_path,{'temu':{'max_products_per_keyword':400}})['workers'][0]
    assert worker['total']==2400 and worker['completed']==304
    assert 12<=worker['percent']<13


def test_temu_finished_worker_keeps_visible_keyword_progress(tmp_path):
    put(tmp_path/'temu/supervisors/assignments.json',{'workers':[{'name':'worker-01-new','queries':['drill','cordless drill']}]})
    put(tmp_path/'temu/supervisors/worker-01-new.json',{'status':'WORKER_COMPLETE','current_query':None})
    put(tmp_path/'temu/workers/worker-01-new/drill/progress.json',{'search_query':'drill','status':'KEYWORD_LIMIT_REACHED','unique_goods':100,'raw_collected_count':100})
    put(tmp_path/'temu/workers/worker-01-new/cordless-drill/progress.json',{'search_query':'cordless drill','status':'KEYWORD_LIMIT_REACHED','unique_goods':100,'raw_collected_count':100})
    worker=dashboard._temu(tmp_path,{'temu':{'max_products_per_keyword':100}})['workers'][0]
    assert worker['actual_count']==200
    assert (worker['completed'],worker['total'],worker['remaining'])==(200,200,0)


def test_201_candidates_incrementally_verify_and_image_changes_invalidate(tmp_path,monkeypatch):
    from selection_automation import vision
    put(tmp_path/'category.json',{'category_key':'fixture'})
    image=tmp_path/'image';image.write_bytes(b'fixture')
    rows=[{'asin':'A','goods_id':str(i),'amazon_image_path':str(image),'temu_image_path':str(image)} for i in range(201)]
    (tmp_path/'layer3_candidates_fixture.jsonl').write_text(''.join(json.dumps(row)+'\n' for row in rows),encoding='utf-8')
    calls=[]
    def keys(category,row):
        calls.append(row['goods_id']);return ('new','legacy')
    monkeypatch.setattr(vision,'compatible_vision_keys',keys)
    state={'revisions':{(('A',str(i)),'new'):'success' for i in range(201)}}
    first=dashboard._current_vision(tmp_path,tmp_path,state)
    assert first['completed'] is None and first['verification_completed']==200
    second=dashboard._current_vision(tmp_path,tmp_path,state)
    assert second['completed']==201 and second['total']==201 and len(calls)==201
    dashboard._current_vision(tmp_path,tmp_path,state)
    assert len(calls)==201
    image.write_bytes(b'changed-image-size')
    changed=dashboard._current_vision(tmp_path,tmp_path,state)
    assert changed['completed'] is None and changed['verification_completed']==200
    assert dashboard._current_vision(tmp_path,tmp_path,state)['completed']==201
