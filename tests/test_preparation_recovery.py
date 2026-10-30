"""Offline preparation recovery: paid rules and real collector artifacts survive."""
import json
from pathlib import Path
import sys
from types import SimpleNamespace
import pytest

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'scripts'))
import web_console as web


def artifacts(root,token='a'*32):
    category=json.loads((ROOT/'config/categories/power_drill.json').read_text('utf-8'))
    category['category_key']='drill_'+token[:12]
    category['temu'].update(category_aliases=['电钻','power drill'],
        subject_filter={'mode':'TARGET_BODY_ONLY','reject_mixed_subjects':True,'exclude_subject_terms':['磨甲机','nail drill']},
        max_products_per_keyword=400,worker_count=4)
    category['amazon']['target_count']=2
    from selection_automation.rule_provider import settings
    from selection_automation.commercial import settings as commercial_settings
    category['rule_generation_provider']=settings()
    category['commercial']={**commercial_settings({'same_limit':5}),'same_operator':'lt'}
    scope={'category':category['category_name_zh'],'amazon_count':2,'per_keyword':400,'worker_count':4,
           'commercial':category['commercial'],'api_base_url':settings()['base_url'],'api_model':settings()['model'],'bestsellers_url':'','amazon_route_mode':'AUTO'}
    output=root/'runtime/web-console'/f'category-{token}.json'
    web.selection.save(output,category)
    web.selection.save(output.with_suffix('.progress.json'),{'stage':'complete','message':'本品类配置已生成并通过检查'})
    rows=[{'asin':f'B00000000{n}','rank':n,'title':'Power drill','product_url':f'https://www.amazon.com/dp/B00000000{n}',
           'detail_status':'详情页待补全','detail_error':{'code':'DETAIL_TIMEOUT','retryable':True,
               'url':'https://www.amazon.com/checkpoint/continue?token=NEVER_EXPOSE_THIS'}} for n in (1,2)]
    source=category['amazon']['bestsellers_url']
    payload={'schema_version':'amazon-bestsellers-frontend-v1','category_key':category['category_key'],'source_url':source,
             'target_count':2,'records':rows,'status':'RUNNING','listing_complete':True,'details_complete':False,
             'detail_queue':{'completed_count':0,'status':'RUNNING'}}
    web.selection.save(output.with_suffix('.amazon.json'),payload)
    web.selection.save(output.with_suffix('.amazon.json.checkpoint.json'),{'schema_version':'amazon-detail-checkpoint-v1',
        'scope':{'source_url':source,'category_key':category['category_key'],'target_count':2,'detail_standard':'LATEST_DETAIL_V2'},
        'status':'RUNNING','records':rows})
    return output,category,scope


def test_legacy_recovery_requires_unique_full_scope_and_real_artifacts(tmp_path):
    output,category,scope=artifacts(tmp_path)
    console=web.Console(tmp_path)
    assert console.recover_preparation(scope,legacy=True)==(output,'rules_ready')
    for field,value in [('category','键盘'),('amazon_count',3),('per_keyword',401),('worker_count',8),('api_model','other')]:
        assert console.recover_preparation({**scope,field:value},legacy=True) is None
    assert console.recover_preparation({**scope,'commercial':{'same_limit':6}},legacy=True) is None
    second,_,_=artifacts(tmp_path,'b'*32)
    with pytest.raises(web.ConsoleError,match='多份'):console.recover_preparation(scope,legacy=True)
    second.unlink()
    (tmp_path/'runs'/category['category_key']).mkdir(parents=True)
    assert console.recover_preparation(scope,legacy=True) is None


@pytest.mark.parametrize('corruption',['provider','subject','progress','amazon_scope','checkpoint_scope'])
def test_legacy_recovery_rejects_missing_or_mismatched_evidence(tmp_path,corruption):
    output,category,scope=artifacts(tmp_path)
    target=output
    if corruption=='provider':category['rule_generation_provider']['model']='other'
    elif corruption=='subject':category['temu'].pop('subject_filter')
    elif corruption=='progress':target=output.with_suffix('.progress.json');category={'stage':'error'}
    elif corruption=='amazon_scope':
        target=output.with_suffix('.amazon.json');category=web.selection.read(target);category['category_key']='other'
    else:
        target=output.with_suffix('.amazon.json.checkpoint.json');category=web.selection.read(target);category['scope']['target_count']=3
    web.selection.save(target,category)
    assert web.Console(tmp_path).recover_preparation(scope,legacy=True) is None


def test_web_generate_reuses_uuid_without_model_and_stop_survives_restart(tmp_path,monkeypatch):
    output,category,scope=artifacts(tmp_path)
    console=web.Console(tmp_path);monkeypatch.setattr(console,'owner',lambda:None)
    requests=[]
    class Thread:
        def __init__(self,target,args,daemon):requests.append((target,args))
        def start(self):pass
    monkeypatch.setattr(web.threading,'Thread',Thread)
    body={'category_name':scope['category'],'amazon_count':2,'per_keyword':400,'worker_count':4,
          'api_key':'TEST_PRIVATE_KEY','api_base_url':scope['api_base_url'],'api_model':scope['api_model'],'commercial':scope['commercial']}
    assert console.start(body)['preparation_resumed'] is True
    calls=[]
    def invoke(argv,**kwargs):
        calls.append(argv)
        assert not any('generate_category.py' in arg for arg in argv)
        assert argv[argv.index('--config')+1]==str(output)
        assert kwargs['env']['AMAZON_CONTINUATION_RECHECK']=='1'
        assert kwargs['env']['AMAZON_MANUAL_RECHECK']=='1'
        console.cancel.set()
        raise web.ConsoleError('CANCELLED','已停止')
    monkeypatch.setattr(console,'invoke',invoke)
    before=output.with_suffix('.amazon.json.checkpoint.json').read_bytes()
    console._start(requests[0][1][0])
    assert calls and console.draft is None and console.preparation_path==output
    assert output.with_suffix('.amazon.json.checkpoint.json').read_bytes()==before
    console.workspace_stop_pending=True
    console.finish_workspace_stop(None)
    assert console.pending_scope==scope and not console.workspace_stop_pending
    restarted=web.Console(tmp_path)
    assert restarted.preparation_path==output and restarted.status()['can_resume_preparation']
    assert restarted.draft is None and restarted.pending_key is None
    assert 'TEST_PRIVATE_KEY' not in (output.parent/'pending-generation.json').read_text('utf-8')


def test_true_collector_progress_replaces_completed_generation_and_redacts_gate(tmp_path):
    output,_,scope=artifacts(tmp_path)
    console=web.Console(tmp_path);console.preparation_path=output;console.pending_scope=scope
    console.preparation_stage='rules_ready';console.progress_file=output.with_suffix('.progress.json')
    console.busy=True;console.phase='正在采集 Amazon 商品标题、详情参数并提炼关键词'
    progress=console.generation_progress()
    assert progress['stage']=='amazon_details' and progress['amazon_gate_count']==2
    assert '0/2' in progress['message'] and '访问门禁' in progress['message']
    assert progress['amazon_error_codes']==['AMAZON_CONTINUATION_REQUIRED']
    assert 'NEVER_EXPOSE' not in json.dumps(progress) and 'checkpoint/continue' not in json.dumps(progress)
    console.busy=False;console.phase='准备已停止'
    assert console.generation_progress()['message'].startswith('准备已停止')
    assert '0/2' in console.generation_progress()['message']


@pytest.mark.parametrize('claimed_complete,verifier_code',[(False,0),(True,2)])
def test_partial_or_unverified_details_never_reach_keywords_or_paid_visual(tmp_path,monkeypatch,claimed_complete,verifier_code):
    output,category,scope=artifacts(tmp_path)
    amazon=output.with_suffix('.amazon.json');payload=web.selection.read(amazon)
    if claimed_complete:payload.update(status='COMPLETE',details_complete=True);web.selection.save(amazon,payload)
    console=web.Console(tmp_path);console.pending_scope=scope;console.preparation_path=output
    console.preparation_stage='rules_ready';console.persist_preparation()
    calls=[]
    def invoke(argv,**kwargs):
        calls.append(argv)
        assert not any('generate_category.py' in arg or 'extract_match_criteria.py' in arg for arg in argv)
        return SimpleNamespace(returncode=verifier_code if '--input-type=module' in argv else 0 if claimed_complete else 2)
    monkeypatch.setattr(console,'invoke',invoke)
    console._start({'category_name':scope['category'],'amazon_count':2,'per_keyword':400,
                   'api_key':'PRIVATE','api_base_url':scope['api_base_url'],'api_model':scope['api_model']})
    assert console.generation_error['code']=='AMAZON_DETAILS_INCOMPLETE'
    assert console.preparation_path==output and console.draft is None
    assert not output.with_suffix('.keywords.json').exists()


def test_explicit_invalid_checkpoint_fails_closed_before_model(tmp_path,monkeypatch):
    output,_,scope=artifacts(tmp_path)
    web.selection.save(output.parent/'pending-generation.json',{'scope':scope,
        'preparation':{'schema_version':'web-preparation-v1','category_file':output.name,'stage':'rules_ready'}})
    output.unlink()
    console=web.Console(tmp_path);monkeypatch.setattr(console,'owner',lambda:None)
    with pytest.raises(web.ConsoleError,match='校验失败'):
        console.start({'category_name':scope['category'],'amazon_count':2,'per_keyword':400,'worker_count':4,
            'api_key':'PRIVATE','api_base_url':scope['api_base_url'],'api_model':scope['api_model'],'commercial':scope['commercial']})


def test_explicit_preparation_can_switch_only_amazon_route_before_listing_exists(tmp_path,monkeypatch):
    output,_,scope=artifacts(tmp_path)
    for path in output.parent.glob('*.amazon.json*'):path.unlink()
    web.selection.save(output.parent/'pending-generation.json',{'scope':scope,
        'preparation':{'schema_version':'web-preparation-v1','category_file':output.name,'stage':'rules_ready'}})
    console=web.Console(tmp_path);monkeypatch.setattr(console,'owner',lambda:None)
    captured=[]
    class Thread:
        def __init__(self,target,args,daemon):captured.append(args[0])
        def start(self):pass
    monkeypatch.setattr(web.threading,'Thread',Thread)
    response=console.start({'category_name':scope['category'],'amazon_count':2,'per_keyword':400,'worker_count':4,
        'amazon_route_mode':'DIRECT','api_key':'PRIVATE','api_base_url':scope['api_base_url'],
        'api_model':scope['api_model'],'commercial':scope['commercial']})
    assert response['preparation_resumed'] is True and console.preparation_path==output
    assert console.pending_scope['amazon_route_mode']=='DIRECT'
    def invoke(argv,**kwargs):
        assert '--config' in argv and kwargs['env']['AMAZON_BROWSER_ROUTE_MODE']=='DIRECT'
        assert kwargs['env']['AMAZON_CONTINUATION_RECHECK']=='1'
        assert kwargs['env']['AMAZON_MANUAL_RECHECK']=='1'
        assert 'DEEPSEEK_API_KEY' not in kwargs['env']
        raise web.ConsoleError('CANCELLED','已停止')
    monkeypatch.setattr(console,'invoke',invoke)
    console._start(captured[0])
    assert console.preparation_path==output


def test_stopped_preparation_never_launches_another_child(tmp_path,monkeypatch):
    console=web.Console(tmp_path);console.cancel.set()
    monkeypatch.setattr(web.subprocess,'Popen',lambda *a,**k:pytest.fail('must not launch after Stop'))
    with pytest.raises(web.ConsoleError,match='已停止'):console.invoke(['not-a-command'],env={})


def visual_fixture(output,category,*,ready):
    from selection_automation.match_criteria import seed
    from selection_automation.visual_criteria import start,VERSION
    amazon=output.with_suffix('.amazon.json');payload=web.selection.read(amazon)
    payload.update(status='COMPLETE',details_complete=True)
    for n,row in enumerate(payload['records']):
        row['main_image_url']=f'https://m.media-amazon.com/images/I/fixture{n}.jpg'
    web.selection.save(amazon,payload)
    catalog=start(seed(category,payload))
    state='ready' if ready else 'partial'
    catalog.update(state=state,image_review={'version':VERSION,'state':state,'total_images':2,
        'completed_images':2 if ready else 1,'total_products':2,'reviewed_products':2 if ready else 1,
        'missing_asins':[],'failed_batches':0 if ready else 1})
    category['match_criteria']=catalog;web.selection.save(output,category)
    return payload


@pytest.mark.parametrize('returncode',[0,1])
def test_failed_or_partial_visual_review_never_becomes_completed_or_draft(tmp_path,monkeypatch,returncode):
    output,category,scope=artifacts(tmp_path);visual_fixture(output,category,ready=False)
    console=web.Console(tmp_path);console.pending_scope=scope;console.preparation_path=output
    console.preparation_stage='rules_ready';console.persist_preparation();calls=[]
    def invoke(argv,**kwargs):
        calls.append(argv)
        return SimpleNamespace(returncode=returncode if any('extract_match_criteria.py' in arg for arg in argv) else 0)
    monkeypatch.setattr(console,'invoke',invoke)
    monkeypatch.setattr(console,'explain_file',lambda *a:pytest.fail('partial visual review must not reach explanation'))
    console._start({'category_name':scope['category'],'amazon_count':2,'per_keyword':400,'api_key':'PRIVATE'})
    assert console.generation_error['code']=='VISUAL_PREPARATION_INCOMPLETE'
    assert console.preparation_stage=='rules_ready' and console.draft is None
    assert web.selection.read(output.parent/'pending-generation.json')['preparation']['stage']=='rules_ready'
    restarted=web.Console(tmp_path)
    assert restarted.preparation_stage=='rules_ready' and restarted.draft is None


@pytest.mark.parametrize('manifest_stage',['rules_ready','visual_complete'])
def test_successful_visual_review_of_current_amazon_is_not_requested_again(tmp_path,monkeypatch,manifest_stage):
    output,category,scope=artifacts(tmp_path);visual_fixture(output,category,ready=True)
    console=web.Console(tmp_path);console.pending_scope=scope;console.preparation_path=output
    console.preparation_stage=manifest_stage
    def invoke(argv,**kwargs):
        assert not any('extract_match_criteria.py' in arg or 'generate_category.py' in arg for arg in argv)
        return SimpleNamespace(returncode=0)
    monkeypatch.setattr(console,'invoke',invoke);monkeypatch.setattr(console,'explain_file',lambda source,*a:source)
    console._start({'category_name':scope['category'],'amazon_count':2,'per_keyword':400,'api_key':'PRIVATE'})
    assert console.generation_error is None and console.draft


def test_visual_complete_manifest_with_changed_amazon_stays_blocked_without_repay(tmp_path,monkeypatch):
    output,category,scope=artifacts(tmp_path);payload=visual_fixture(output,category,ready=True)
    payload['records'][0]['title']='Changed observed product title'
    web.selection.save(output.with_suffix('.amazon.json'),payload)
    console=web.Console(tmp_path);console.pending_scope=scope;console.preparation_path=output
    console.preparation_stage='visual_complete'
    def invoke(argv,**kwargs):
        assert not any('extract_match_criteria.py' in arg for arg in argv)
        return SimpleNamespace(returncode=0)
    monkeypatch.setattr(console,'invoke',invoke)
    for _ in range(2):
        console._start({'category_name':scope['category'],'amazon_count':2,'per_keyword':400,'api_key':'PRIVATE'})
        assert console.generation_error['code']=='VISUAL_CHECKPOINT_MISMATCH'
        assert console.preparation_stage=='visual_mismatch' and console.draft is None
