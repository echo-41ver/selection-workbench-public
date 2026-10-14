from concurrent.futures import ThreadPoolExecutor
from selection_automation import work_dispatch as dispatch
import json
import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))


def test_recent_follows_same_keyword_sales_before_next_keyword(tmp_path):
    dispatch.sync(tmp_path,[{'query':'first spatula','max_products':100},
                            {'query':'next spatula','max_products':100}],['w1'])
    sales=dispatch.claim_pass(tmp_path,'w1');assert sales['sort']=='sales'
    dispatch.complete_pass(tmp_path,'w1',sales['query'],'sales')
    recent=dispatch.claim_pass(tmp_path,'w1')
    assert (recent['query'],recent['sort'])==(sales['query'],'recent')
    dispatch.complete_pass(tmp_path,'w1',recent['query'],'recent')
    assert dispatch.claim_pass(tmp_path,'w1')['query']!=sales['query']


def test_confirmed_empty_query_skips_both_sorts_and_preserves_next_job(tmp_path):
    dispatch.sync(tmp_path,[{'query':'empty spatula','max_products':100},
                            {'query':'next spatula','max_products':100}],['w1'])
    empty=dispatch.claim_pass(tmp_path,'w1')
    dispatch.complete_empty_query(tmp_path,'w1',empty['query'],{'confirmed':True})
    job=next(row for row in dispatch.read(tmp_path)['jobs'] if row['query']==empty['query'])
    assert job['complete'] and all(state['skipped'] for state in job['sort_passes'].values())
    assert dispatch.claim_pass(tmp_path,'w1')['query']=='next spatula'


def test_empty_checkpoint_requires_matching_query_and_explicit_source_evidence(tmp_path):
    from pipeline import confirmed_empty_checkpoint
    progress={'status':'KEYWORD_NO_RESULTS','unique_goods':0,'completion_basis':'EXPLICIT_EMPTY_QUERY_RESULTS',
              'no_results_evidence':{'confirmed':True,'protocol':'VISIBLE_EMPTY_RESULTS_STABLE_V2',
                'loading':False,'stable_ms':15000,'samples':3,'query':'empty spatula',
                'url':'https://www.temu.com/search_result.html?search_key=empty%20spatula'}}
    assert confirmed_empty_checkpoint(progress,'empty spatula')
    assert not confirmed_empty_checkpoint(progress,'another spatula')
    assert not confirmed_empty_checkpoint({**progress,'unique_goods':1},'empty spatula')
    assert not confirmed_empty_checkpoint({**progress,'manual_action_required':True},'empty spatula')
    old={**progress,'no_results_evidence':{**progress['no_results_evidence'],'protocol':'OLD'}}
    assert not confirmed_empty_checkpoint(old,'empty spatula')


def test_requeue_old_empty_skips_preserves_real_completed_passes(tmp_path):
    dispatch.sync(tmp_path,[{'query':'old empty','max_products':10},{'query':'collected','max_products':10}],['w1'])
    job=dispatch.claim_pass(tmp_path,'w1')
    dispatch.complete_empty_query(tmp_path,'w1',job['query'],{'confirmed':True})
    data=dispatch.read(tmp_path);other=next(row for row in data['jobs'] if row['query']!=job['query'])
    other['sort_passes']['sales'].update(started=True,complete=True);dispatch.save(tmp_path,data)
    assert dispatch.requeue_unsettled_empty_queries(tmp_path)==[job['query']]
    data=dispatch.read(tmp_path);old=next(row for row in data['jobs'] if row['query']==job['query'])
    assert not old['complete'] and all(not p['complete'] and not p['started'] for p in old['sort_passes'].values())
    assert next(row for row in data['jobs'] if row['query']!=job['query'])['sort_passes']['sales']['complete']
    assert dispatch.requeue_unsettled_empty_queries(tmp_path)==[]


def test_weighted_assignment_and_idle_stealing_keeps_started_owner(tmp_path):
    plan=[{'query':'category','max_products':1500}]+[{'query':str(i),'max_products':500} for i in range(9)]
    initial=dispatch.sync(tmp_path,plan,['w1','w2','w3','w4'])
    loads=[sum(r['max_products'] for r in initial['jobs'] if r['owner']==w) for w in initial['lanes']]
    assert max(loads)-min(loads)<=500
    original=dispatch.claim(tmp_path,'w1')
    taken=[]
    for _ in range(9):
        job=dispatch.claim(tmp_path,'w2');taken.append(job['query'])
        dispatch.complete(tmp_path,'w2',job['query'])
    assert len(set(taken))==9 and original['query'] not in taken
    assert dispatch.claim(tmp_path,'w2') is None
    assert dispatch.claim(tmp_path,'w1')['query']==original['query']


def test_concurrent_claim_and_restart_never_duplicates_ownership(tmp_path):
    names=[f'w{i}' for i in range(50)]
    plan=[{'query':str(i),'max_products':10} for i in range(50)]
    dispatch.sync(tmp_path,plan,names)
    with ThreadPoolExecutor(max_workers=50) as pool:
        claims=list(pool.map(lambda n:dispatch.claim(tmp_path,n),names))
    assert len({j['query'] for j in claims})==50
    restarted=dispatch.sync(tmp_path,plan+[{'query':'new','max_products':20}],names)
    assert all(next(j for j in restarted['jobs'] if j['query']==c['query'])['owner']==c['owner'] for c in claims)


def test_completed_pass_can_leave_only_the_unstarted_sibling_for_idle_lane(tmp_path):
    dispatch.sync(tmp_path,[{'query':'pump','max_products':100}],['w1','w2'])
    sales=dispatch.claim_pass(tmp_path,'w1')
    assert (sales['query'],sales['sort'])==('pump','sales')
    # While sales is active, even the same query's future pass stays protected.
    assert dispatch.claim_pass(tmp_path,'w2') is None
    dispatch.complete_pass(tmp_path,'w1','pump','sales')
    recent=dispatch.claim_pass(tmp_path,'w2')
    assert (recent['query'],recent['sort'])==('pump','recent')
    assert not dispatch.available(dispatch.read(tmp_path),'w1')
    dispatch.complete_pass(tmp_path,'w2','pump','recent')
    job=dispatch.read(tmp_path)['jobs'][0]
    assert job['complete'] is True
    assert dispatch.claim_pass(tmp_path,'w1') is None


def test_legacy_started_query_is_protected_during_dispatch_upgrade(tmp_path):
    dispatch.sync(tmp_path,[{'query':'old pump','max_products':100}],['w1','w2'])
    data=dispatch.read(tmp_path)
    data['jobs'][0].pop('sort_passes')
    data['jobs'][0].update(owner='w1',started=True,complete=False)
    dispatch.save(tmp_path,data)
    upgraded=dispatch.sync(tmp_path,[{'query':'old pump','max_products':100}],['w1','w2'])
    assert dispatch.claim_pass(tmp_path,'w2') is None
    sales=dispatch.claim_pass(tmp_path,'w1')
    assert sales['sort']=='sales'
    assert upgraded['jobs'][0]['sort_passes']['recent']['owner']=='w1'
    assert upgraded['jobs'][0]['sort_passes']['recent']['started'] is True


def test_completed_counts_follow_pass_owners_without_cross_lane_credit(tmp_path):
    data=dispatch.sync(tmp_path,[
        {'query':'split pump','max_products':100},
        {'query':'owned pump','max_products':100},
    ],['w1','w2'])
    split,owned=data['jobs']
    split.update(owner='w1',started=True,complete=True)
    split['sort_passes']={
        'sales':{'owner':'w1','started':True,'complete':True},
        'recent':{'owner':'w2','started':True,'complete':True},
    }
    owned.update(owner='w1',started=True,complete=True)
    owned['sort_passes']={
        'sales':{'owner':'w1','started':True,'complete':True},
        'recent':{'owner':'w1','started':True,'complete':True},
    }
    dispatch.save(tmp_path,data)
    data=dispatch.read(tmp_path)
    queries,passes=dispatch.completed_for_lane(data,'w1')
    assert queries==['owned pump']
    assert passes==[
        {'query':'split pump','sort':'sales'},
        {'query':'owned pump','sort':'sales'},
        {'query':'owned pump','sort':'recent'},
    ]
    queries,passes=dispatch.completed_for_lane(data,'w2')
    assert queries==['split pump']
    assert passes==[{'query':'split pump','sort':'recent'}]


def test_budget_increase_reopens_confirmed_limit_job_without_moving_owner_or_losing_evidence(tmp_path):
    old=[{'query':'portable pump','max_products':400,'source_asins':['A1'],
          'source_evidence':[{'asin':'A1','title':'Portable pump'}]}]
    dispatch.sync(tmp_path,old,['w1','w2'])
    data=dispatch.read(tmp_path);job=data['jobs'][0]
    job.update(owner='w2',started=True,complete=True,completed_at=10)
    dispatch.save(tmp_path,data)
    updated=dispatch.sync(tmp_path,[{'query':'portable pump','max_products':1200,
        'source_asins':['A1','A2'],'source_evidence':[{'asin':'A2','title':'Portable pump 12V'}]}],
        ['w1','w2'],resume_queries={'portable pump'})['jobs'][0]
    assert (updated['max_products'],updated['owner'],updated['started'],updated['complete'])==(1200,'w2',True,False)
    assert updated['source_asins']==['A1','A2']
    assert {item['asin'] for item in updated['source_evidence']}=={'A1','A2'}


def test_budget_never_decreases_and_complete_job_is_not_reopened_without_limit_evidence(tmp_path):
    data=dispatch.sync(tmp_path,[{'query':'pump','max_products':1200}],['w1'])
    data['jobs'][0].update(owner='w1',started=True,complete=True,completed_at=10)
    dispatch.save(tmp_path,data)
    updated=dispatch.sync(tmp_path,[{'query':'pump','max_products':400}],['w1'],
                           resume_queries={'pump'})['jobs'][0]
    assert updated['max_products']==1200 and updated['complete'] is True


def test_interrupted_owned_recent_pass_resumes_before_unstarted_sales(tmp_path):
    dispatch.sync(tmp_path,[{'query':'saved pump','max_products':400},
                            {'query':'new pump','max_products':100}],['w1'])
    data=dispatch.read(tmp_path)
    saved=next(job for job in data['jobs'] if job['query']=='saved pump')
    saved['sort_passes']['sales'].update(complete=True,started=True)
    saved['sort_passes']['recent'].update(owner='w1',started=True)
    dispatch.save(tmp_path,data)
    claimed=dispatch.claim_pass(tmp_path,'w1')
    assert (claimed['query'],claimed['sort'])==('saved pump','recent')
    pending=next(job for job in dispatch.read(tmp_path)['jobs'] if job['query']=='new pump')
    assert not pending['sort_passes']['sales']['started']


def test_upgrade_requeue_requires_current_confirmed_limit_and_skips_true_no_more_results(tmp_path):
    import sys
    from pathlib import Path
    sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
    from pipeline import limit_upgrade_resume_queries,confirmed_checkpoint_meets_budget
    run=tmp_path/'run';query='pump'
    dispatch.sync(run,[{'query':query,'max_products':400}],['w1'])
    data=dispatch.read(run);data['jobs'][0].update(owner='w1',started=True,complete=True)
    dispatch.save(run,data)
    target=[{'query':query,'max_products':1200}]
    for sort in ('sales','recent'):
        output=run/'temu/workers/w1'/(query+('--recent' if sort=='recent' else ''))
        output.mkdir(parents=True)
        evidence={'requested':sort,'state':'UI_CONFIRMED','query':query}
        (output/'products.jsonl').write_text(json.dumps({'goods_id':'g1','search_sort':evidence})+'\n')
        (output/'progress.json').write_text(json.dumps({'status':'KEYWORD_LIMIT_REACHED','unique_goods':1,'sales_sort':evidence}))
    assert limit_upgrade_resume_queries(run,target)=={query}
    sales=run/'temu/workers/w1'/query
    checkpoint=json.loads((sales/'progress.json').read_text())
    assert not confirmed_checkpoint_meets_budget(sales,checkpoint,query,'sales',1200)
    checkpoint['status']='KEYWORD_COMPLETE'
    assert confirmed_checkpoint_meets_budget(sales,checkpoint,query,'sales',1200)
    for sort in ('sales','recent'):
        output=run/'temu/workers/w1'/(query+('--recent' if sort=='recent' else ''))
        checkpoint=json.loads((output/'progress.json').read_text());checkpoint['status']='KEYWORD_COMPLETE'
        (output/'progress.json').write_text(json.dumps(checkpoint))
    assert limit_upgrade_resume_queries(run,target)==set()


def test_legacy_relevance_completion_requeues_without_changing_product_target(tmp_path):
    from pipeline import limit_upgrade_resume_queries, confirmed_checkpoint_meets_budget
    query='computer mouse';run=tmp_path/'run';items=[{'query':query,'max_products':400}]
    dispatch.sync(run,items,['w1'])
    data=dispatch.read(run);data['jobs'][0].update(owner='w1',started=True,complete=True);dispatch.save(run,data)
    output=run/'temu/workers/w1/computer-mouse';output.mkdir(parents=True)
    evidence={'requested':'sales','state':'UI_CONFIRMED','query':query}
    progress={'status':'KEYWORD_COMPLETE','unique_goods':1,'sales_sort':evidence,
              'stop_reason':'CONFIGURED_COMPLETE_ROUNDS_WITHOUT_NEW_RELEVANT_GOODS'}
    raw=json.dumps({'goods_id':'g1','search_sort':evidence})+'\n'
    (output/'products.jsonl').write_text(raw)
    (output/'progress.json').write_text(json.dumps(progress))
    assert limit_upgrade_resume_queries(run,items)=={query}
    assert not confirmed_checkpoint_meets_budget(output,progress,query,'sales',400)
    assert (output/'products.jsonl').read_text()==raw
    progress['stop_reason']='NO_MORE_RESULTS_CONFIRMED'
    (output/'progress.json').write_text(json.dumps(progress))
    assert limit_upgrade_resume_queries(run,items)==set()
    assert confirmed_checkpoint_meets_budget(output,progress,query,'sales',400)
