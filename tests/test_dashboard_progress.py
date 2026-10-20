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
