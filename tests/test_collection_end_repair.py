import json
from pathlib import Path
from types import SimpleNamespace
import pytest
from selection_automation.empty_results import record_empty_observation,confirmed_empty_checkpoint
from selection_automation import work_dispatch as dispatch
import pipeline


def observation(query='washer'):
    return {'status':'STOPPED_EMPTY_QUERY_RESULTS','unique_goods':0,'raw_collected_count':0,
        'automatic_retry':True,'manual_action_required':False,'no_results_evidence':{
            'confirmed':True,'protocol':'VISIBLE_EMPTY_RESULTS_STABLE_V2','loading':False,
            'stable_ms':31000,'samples':20,'query':query,
            'url':'https://www.temu.com/search_result.html?search_key='+query}}


def test_polling_same_empty_page_cannot_count_as_independent_searches(tmp_path):
    progress=observation();path=tmp_path/'checks.json'
    for _ in range(3):assert record_empty_observation(path,progress,'washer','same-profile') is None
    evidence=record_empty_observation(path,progress,'washer','fresh-profile')
    checked={**progress,'status':'KEYWORD_NO_RESULTS','completion_basis':'EXPLICIT_EMPTY_QUERY_RESULTS','no_results_evidence':evidence}
    assert confirmed_empty_checkpoint(checked,'washer')
    assert not confirmed_empty_checkpoint(checked,'another query')
    assert not confirmed_empty_checkpoint({**checked,'raw_collected_count':40},'washer')


def test_premature_same_limit_checkpoints_reopen_without_deleting_collected_rows(tmp_path):
    run=tmp_path/'run';plan=[{'query':'washer','max_products':1200}]
    dispatch.sync(run,plan,['w1'])
    for sort,count in [('sales',880),('recent',600)]:
        job=dispatch.claim_pass(run,'w1');assert job['sort']==sort
        dispatch.complete_pass(run,'w1','washer',sort)
        out=run/'temu/workers/w1'/('washer'+('--recent' if sort=='recent' else ''));out.mkdir(parents=True)
        evidence={'requested':sort,'state':'UI_CONFIRMED','query':'washer'}
        (out/'products.jsonl').write_text(json.dumps({'goods_id':'saved','search_sort':evidence})+'\n')
        (out/'progress.json').write_text(json.dumps({'status':'KEYWORD_COMPLETE','unique_goods':1,
            'raw_collected_count':count,'sales_sort':evidence,
            'completion_basis':'CONFIGURED_COMPLETE_ROUNDS_WITHOUT_NEW_RELEVANT_OR_UNCLASSIFIED_GOODS'}))
    resume=pipeline.limit_upgrade_resume_queries(run,plan);assert resume=={'washer'}
    result=dispatch.sync(run,plan,['w1'],repair_queries=resume)
    assert not result['jobs'][0]['complete']
    assert all(not row['complete'] for row in result['jobs'][0]['sort_passes'].values())
    assert json.loads((out/'products.jsonl').read_text())['goods_id']=='saved'


@pytest.mark.parametrize('still_empty',[False,True])
def test_pipeline_researches_empty_query_in_fresh_environment_before_skipping(tmp_path,monkeypatch,still_empty):
    monkeypatch.setattr(pipeline,'ROOT',tmp_path)
    monkeypatch.setattr(pipeline,'ensure_proxy_chain',lambda *args:False)
    monkeypatch.setattr(pipeline,'interruptible_wait',lambda *args:None)
    category={'category_key':'fixture'};base=pipeline.run_root(category)
    sessions=tmp_path/'sessions.json';sessions.parent.mkdir(exist_ok=True)
    manifest={'workers':[{'name':'worker-01-new','profile_id':'first','webdriver_url':'http://first',
        'session_id_file':'first.sid','bidi_url_file':'first.bidi'}]}
    def write(path,value):path.parent.mkdir(parents=True,exist_ok=True);path.write_text(json.dumps(value))
    write(sessions,manifest)
    write(tmp_path/'config/proxy_policy.json',{'session_rotation_policy':'NEW_ADSPOWER_PROFILE_AND_NEW_PROXY_SESSION',
        'delete_old_profile_first_via_adspower':True,'worker_chain_ports':[19081]})
    write(pipeline.keyword_plan_path(category),{'keywords':[{'query':'washer','max_products':2}]})
    calls=[];rotations=[]
    def collect(command,**kwargs):
        calls.append(command.copy());out=Path(command[command.index('--output')+1]);out.mkdir(parents=True,exist_ok=True)
        if len(calls)==1 or still_empty:
            write(out/'progress.json',observation());return 2
        sort=command[command.index('--sort')+1]
        write(out/'progress.json',{'status':'KEYWORD_LIMIT_REACHED','unique_goods':2,'raw_collected_count':2,
            'sales_sort':{'state':'UI_CONFIRMED','query':'washer','requested':sort}})
        return 0
    def rotate(command,**kwargs):
        rotations.append(command)
        manifest['workers'][0].update(profile_id='second',webdriver_url='http://second',session_id_file='second.sid',bidi_url_file='second.bidi')
        write(sessions,manifest);return SimpleNamespace(returncode=0)
    monkeypatch.setattr(pipeline,'run_collector',collect);monkeypatch.setattr(pipeline.subprocess,'run',rotate)
    pipeline.collect_temu(base/'category.json',category,sessions,'worker-01-new')
    assert len(rotations)==1 and len(calls)==(2 if still_empty else 3)
    job=dispatch.read(base)['jobs'][0];assert job['complete']
    assert all(bool(row.get('skipped'))==still_empty for row in job['sort_passes'].values())
