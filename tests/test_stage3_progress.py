import json
from pathlib import Path
from types import SimpleNamespace
import pytest

from selection_automation import stage3_progress as progress


def test_isolated_reader_uses_normal_windows_priority_without_changing_unix_nice(monkeypatch):
    import os,psutil
    priorities=[]
    monkeypatch.setattr(psutil,'Process',lambda:SimpleNamespace(nice=priorities.append))
    progress.configure_reader_priority()
    assert priorities==[psutil.NORMAL_PRIORITY_CLASS if os.name=='nt' else 10]


def test_agent_similar_is_counted_and_does_not_crash_live_progress(tmp_path,monkeypatch):
    import selection_automation.codex_visual_review as reviews
    base=tmp_path/'task';base.mkdir()
    write(progress.candidate_path(base),[{'asin':'A','goods_id':'T','pair_revision':'p'}])
    monkeypatch.setattr(progress,'compatible_vision_keys',lambda *a,**kw:('v',))
    monkeypatch.setattr(reviews,'load_reviews',lambda *a,**kw:{('A','T','v'):{'review_status':'SIMILAR'}})
    result=progress.snapshot(base,{},history=False)
    assert result['completed']==result['codex_similar']==1 and result['pending']==0


def write(path, rows):
    path.parent.mkdir(parents=True,exist_ok=True)
    path.write_text(''.join(json.dumps(r)+'\n' for r in rows),encoding='utf-8')


def test_progress_signature_stats_large_inputs_and_tracks_revision_billing_and_reviews(tmp_path,monkeypatch):
    base=tmp_path/'task';base.mkdir()
    path=progress.candidate_path(base);write(path,[{'large':'never parsed by signature'}])
    opened=Path.open
    def guarded_open(self,*args,**kwargs):
        if self.suffix=='.jsonl':raise AssertionError('signature must not parse candidate/results/review data')
        return opened(self,*args,**kwargs)
    monkeypatch.setattr(Path,'open',guarded_open)
    alive=[False];monkeypatch.setattr(progress,'controller_alive',lambda _:alive[0])
    cat={'stage3':{'admission_threshold':65}}
    first=progress.snapshot_signature(base,cat)
    assert progress.snapshot_signature(base,cat)==first
    assert progress.snapshot_signature(base,{'stage3':{'admission_threshold':66}})!=first
    for filename in ('results.jsonl','request_intents.jsonl','billing_retry_authorizations.json',
                     'requests.jsonl','codex_reviews/review.jsonl'):
        target=base/'stage123'/filename;target.parent.mkdir(parents=True,exist_ok=True)
        before=progress.snapshot_signature(base,cat)
        with opened(target,'w',encoding='utf-8') as stream:stream.write('changed')
        assert progress.snapshot_signature(base,cat)!=before
    before=progress.snapshot_signature(base,cat);alive[0]=True
    assert progress.snapshot_signature(base,cat)!=before


def controller_state(base, **changes):
    from selection_automation.merge_temu_snapshots import atomic_write
    row={'pid':7,'created':100,'phase':'已暂停派发，结果和断点已保存','updated_at':101,**changes}
    path=base/'stage123/controller.json';path.parent.mkdir(parents=True,exist_ok=True)
    atomic_write(path,json.dumps(row,ensure_ascii=False))
    return path


def test_controller_heartbeat_atomic_replace_keeps_completed_reader_current(tmp_path,monkeypatch):
    (tmp_path/'category.json').write_text('{}',encoding='utf-8')
    write(progress.candidate_path(tmp_path),[{'asin':'A','goods_id':'T','pair_revision':'p'}])
    controller_state(tmp_path)
    monkeypatch.setattr(progress,'controller_alive',lambda _:True)
    original=progress.snapshot
    def snapshot_with_heartbeat(*args,**kwargs):
        result=original(*args,**kwargs)
        controller_state(tmp_path,updated_at=999999.123)
        return result
    monkeypatch.setattr(progress,'snapshot',snapshot_with_heartbeat)
    document=progress.snapshot_document(tmp_path)
    assert document['progress']['total']==document['progress']['pending']==1
    assert document['progress']['completed']==0
    assert document['stable'] is True
    # The web cache restores signature rows after a JSON worker response.
    serialized=json.loads(json.dumps(document['signature']))
    restored=(serialized[0],tuple(tuple(row) for row in serialized[1]),serialized[2])
    assert restored==progress.snapshot_signature(tmp_path,{})


@pytest.mark.parametrize('change',[{'pid':8},{'created':101},{'phase':'分析中'},
                                 {'error':True},{'error_code':'INPUT_INVALID'},
                                 {'new_dispatch_state':{'pending':2}}])
def test_controller_state_changes_still_invalidate_progress(tmp_path,monkeypatch,change):
    controller_state(tmp_path)
    monkeypatch.setattr(progress,'controller_alive',lambda _:True)
    before=progress.snapshot_signature(tmp_path,{})
    controller_state(tmp_path,updated_at=102,**change)
    assert progress.snapshot_signature(tmp_path,{})!=before


@pytest.mark.parametrize('target', ['candidate','stage12_config.json','category.json',
                                  'stage123/results.jsonl','stage123/request_intents.jsonl'])
def test_real_input_or_inflight_settlement_during_reader_stays_dirty(tmp_path,monkeypatch,target):
    (tmp_path/'category.json').write_text('{}',encoding='utf-8')
    controller_state(tmp_path)
    monkeypatch.setattr(progress,'controller_alive',lambda _:True)
    path=progress.candidate_path(tmp_path) if target=='candidate' else tmp_path/target
    def changed_during_read(*args,**kwargs):
        path.parent.mkdir(parents=True,exist_ok=True)
        path.write_text(json.dumps({'pair_revision':'new','status':'settled','billing_state':'KNOWN'}),encoding='utf-8')
        controller_state(tmp_path,updated_at=200)
        return {'total':1,'completed':0}
    monkeypatch.setattr(progress,'snapshot',changed_during_read)
    assert progress.snapshot_document(tmp_path)['stable'] is False


@pytest.mark.parametrize('content',['{bad json','[]','null'])
def test_invalid_controller_cannot_be_cached_as_healthy_state(tmp_path,monkeypatch,content):
    path=controller_state(tmp_path)
    path.write_text(content,encoding='utf-8')
    monkeypatch.setattr(progress,'controller_alive',lambda _:False)
    with pytest.raises(ValueError):progress.snapshot_signature(tmp_path,{})


def test_unreadable_controller_is_not_silently_normalized(tmp_path,monkeypatch):
    path=controller_state(tmp_path)
    original=Path.read_text
    def denied(self,*args,**kwargs):
        if self==path:raise PermissionError('controller is not readable')
        return original(self,*args,**kwargs)
    monkeypatch.setattr(Path,'read_text',denied)
    with pytest.raises(PermissionError):progress.snapshot_signature(tmp_path,{})


def test_incremental_queue_uses_current_revision_and_preserves_retries(tmp_path,monkeypatch):
    base=tmp_path/'task';base.mkdir()
    monkeypatch.setattr(progress,'compatible_vision_keys',lambda _,r,**kwargs:(r['pair_revision'],))
    rows=[{'asin':'A','goods_id':str(i),'pair_revision':str(i)} for i in range(4)]
    write(progress.candidate_path(base),[rows[0],*rows])
    write(base/'stage123/results.jsonl',[
        {'asin':'A','goods_id':'0','vision_revision_key':'0','api_status':'success'},
        {'asin':'A','goods_id':'1','vision_revision_key':'old','api_status':'success'},
        {'asin':'A','goods_id':'2','vision_revision_key':'2','api_status':'failed','attempt':1},
        {'asin':'A','goods_id':'3','vision_revision_key':'3','api_status':'failed','attempt':2}])
    s=progress.snapshot(base,{},history=False)
    assert (s['total'],s['completed'],s['failed'],s['pending'],s['percent'])==(4,1,1,2,50)
    assert s['eta_seconds'] is None
    write(progress.candidate_path(base),[*rows,{'asin':'B','goods_id':'new','pair_revision':'new'}])
    assert progress.snapshot(base,{},history=False)['pending']==3


@pytest.mark.parametrize('mode,expected',[('local_model',1),('multimodal_api',0)])
def test_only_current_exhausted_no_charge_local_failures_are_explicitly_retryable(tmp_path,monkeypatch,mode,expected):
    base=tmp_path/'task';base.mkdir()
    monkeypatch.setattr(progress,'compatible_vision_keys',lambda _,r,**kw:(r['pair_revision'],))
    rows=[{'asin':'A','goods_id':str(i),'pair_revision':str(i)} for i in range(5)]
    write(progress.candidate_path(base),rows)
    write(base/'stage123/results.jsonl',[
        {**rows[0],'vision_revision_key':'0','api_status':'failed','attempt':2,'billing_state':'NO_CHARGE'},
        {**rows[1],'vision_revision_key':'1','api_status':'failed','attempt':2,'billing_state':'KNOWN'},
        {**rows[2],'vision_revision_key':'old','api_status':'failed','attempt':2,'billing_state':'NO_CHARGE'},
        {**rows[3],'vision_revision_key':'3','api_status':'failed','attempt':1,'billing_state':'NO_CHARGE'},
        {**rows[4],'vision_revision_key':'4','api_status':'failed','attempt':2,'billing_state':'NO_CHARGE'},
        {**rows[4],'vision_revision_key':'4','api_status':'success','attempt':2,'billing_state':'KNOWN'}])
    result=progress.snapshot(base,{'stage3':{'provider':{'mode':mode}}},history=False)
    assert result['retryable_local_failed']==expected
    assert (result['completed'],result['failed'],result['pending'])==(1,2,2)


def test_observed_speed_merges_concurrent_requests_and_estimates_remaining(tmp_path,monkeypatch):
    base=tmp_path/'task';base.mkdir()
    write(progress.candidate_path(base),[{'asin':'A','goods_id':str(i)} for i in range(20)])
    write(base/'stage123/request_intents.jsonl',[
        {'request_id':'a','status':'started','time':100,'pairs':10},
        {'request_id':'b','status':'started','time':100,'pairs':10},
        {'request_id':'a','status':'settled','time':110,'billing_state':'KNOWN'},
        {'request_id':'b','status':'settled','time':110,'billing_state':'KNOWN'},
        {'request_id':'unresolved','status':'started','time':120,'pairs':100}])
    write(base/'stage123/requests.jsonl',[
        {'request_id':'a','response_id':'ra'}, {'request_id':'b','response_id':'rb'}])
    write(base/'stage123/results.jsonl',[
        {'asin':'A','goods_id':str(i),'response_id':'ra' if i<10 else 'rb','api_status':'success','model':'d1'}
        for i in range(20)])
    s=progress.snapshot(base,{},history=False)
    assert s['rate_per_hour']==7200 and s['eta_seconds'] is None
    assert s['billing_hold_fail_closed'] and s['runnable_pending']==0


def test_history_uses_successful_group_duration_and_current_parallelism(tmp_path):
    old=tmp_path/'old'; old.mkdir()
    write(old/'stage123/results.jsonl',[
        {'asin':'A','goods_id':str(i),'response_id':'r','api_status':'success',
         'model':'fixture','elapsed_seconds':60} for i in range(12)])
    write(old/'stage123/requests.jsonl',[
        {'response_id':'r','pairs':12}, {'response_id':'failed','pairs':12}])
    base=tmp_path/'task';base.mkdir()
    write(progress.candidate_path(base),[{'asin':'A','goods_id':str(i)} for i in range(120)])
    cat={'stage3':{'model':'fixture','batch_size':12,'max_concurrency':64}}
    result=progress.snapshot(base,cat)
    assert result['rate_source']=='history_group_normalized'
    assert result['rate_per_hour']==46080 and result['eta_seconds']==60
    assert result['pending_requests']==10 and result['historical_sample_groups']==1
    assert result['batch_size']==12 and result['max_concurrency']==64


def test_settled_but_failed_visual_requests_are_not_measured_completed_speed(tmp_path):
    base=tmp_path/'task';base.mkdir()
    write(base/'stage123/request_intents.jsonl',[
        {'request_id':'r','status':'started','time':100,'pairs':12},
        {'request_id':'r','status':'settled','time':110,'billing_state':'KNOWN'}])
    write(base/'stage123/requests.jsonl',[{'request_id':'r','response_id':'failed'}])
    write(base/'stage123/results.jsonl',[{'response_id':'failed','api_status':'failed','model':'fixture'}])
    assert progress.observed_rate(base,'fixture') is None


def native_rate_records(base):
    model='LiquidAI/d1-3B'
    results=[{'asin':'A','goods_id':str(i),'vision_revision_key':f'v{i}',
              'api_status':'success','model':model,'response_id':None,'elapsed_seconds':15}
             for i in range(2)]
    requests=[{'request_id':f'n{i}','response_id':None,'status':'received','pairs':1}
              for i in range(2)]
    intents=[row for i in range(2) for row in (
        {'request_id':f'n{i}','status':'started','time':100+i*15,'pairs':1,'vision_revision_keys':[f'v{i}']},
        {'request_id':f'n{i}','status':'settled','time':115+i*15,'billing_state':'KNOWN'})]
    return model,results,requests,intents


def test_current_native_rate_uses_received_settled_revision_links_not_old_remote_speed(tmp_path,monkeypatch):
    base=tmp_path/'task';base.mkdir()
    model,results,requests,intents=native_rate_records(base)
    results.append({'asin':'A','goods_id':'old','vision_revision_key':'old','api_status':'success',
                    'model':'old-remote','response_id':'remote'})
    requests.append({'request_id':'old','response_id':'remote','status':'received','pairs':1})
    intents.extend([{'request_id':'old','status':'started','time':1,'pairs':1},
                    {'request_id':'old','status':'settled','time':2,'billing_state':'KNOWN'}])
    write(base/'stage123/results.jsonl',results);write(base/'stage123/requests.jsonl',requests)
    write(base/'stage123/request_intents.jsonl',intents)
    write(progress.candidate_path(base),[{'asin':'A','goods_id':str(i),'pair_revision':f'v{i}'} for i in range(4)])
    monkeypatch.setattr(progress,'compatible_vision_keys',lambda _,r,**kwargs:(r['pair_revision'],))
    cat={'stage3':{'model':'old-remote','batch_size':1,'provider':{
        'mode':'local_model','model':model,'base_url':'http://127.0.0.1:7863/v1/decide',
        'protocol':'decisions','workers':1}}}
    paths=[base/'stage123'/n for n in ('results.jsonl','requests.jsonl','request_intents.jsonl')]
    before=[p.read_bytes() for p in paths]
    result=progress.snapshot(base,cat,history=False)
    assert result['rate_source']=='current' and result['rate_per_hour']==240
    assert result['completed']==2 and result['pending']==2 and result['eta_seconds']==30
    assert [p.read_bytes() for p in paths]==before


@pytest.mark.parametrize('change', ['retry_key','duplicate_result','missing_key','missing_received',
    'failed_request','unsettled','unknown','no_charge','wrong_model','missing_model','bad_span','incomplete',
    'failed_result','duplicate_received','duplicate_started','duplicate_settled'])
def test_native_rate_rejects_ambiguous_failed_or_unsettled_links(tmp_path,change):
    base=tmp_path/'task';base.mkdir()
    model,results,requests,intents=native_rate_records(base)
    results=results[:1];requests=requests[:1];intents=intents[:2]
    if change=='retry_key':
        intents.extend([{'request_id':'earlier','status':'started','time':80,'pairs':1,'vision_revision_keys':['v0']},
                        {'request_id':'earlier','status':'settled','time':81,'billing_state':'NO_CHARGE'}])
    if change=='duplicate_result':results.append(dict(results[0]))
    if change=='missing_key':intents[0].pop('vision_revision_keys')
    if change=='missing_received':requests=[]
    if change=='failed_request':requests[0]['status']='failed'
    if change=='unsettled':intents=intents[:1]
    if change=='unknown':intents[1]['billing_state']='UNKNOWN'
    if change=='no_charge':intents[1]['billing_state']='NO_CHARGE'
    if change=='wrong_model':results[0]['model']='old-remote'
    if change=='missing_model':results[0].pop('model')
    if change=='bad_span':intents[1]['time']=99
    if change=='incomplete':requests[0]['response_status']='incomplete'
    if change=='failed_result':results[0]['api_status']='failed'
    if change=='duplicate_received':requests.append(dict(requests[0]))
    if change=='duplicate_started':intents.append(dict(intents[0]))
    if change=='duplicate_settled':intents.append(dict(intents[1]))
    write(base/'stage123/results.jsonl',results);write(base/'stage123/requests.jsonl',requests)
    write(base/'stage123/request_intents.jsonl',intents)
    assert progress.observed_rate(base,model) is None


def test_native_rate_merges_real_overlapping_request_spans(tmp_path):
    base=tmp_path/'task';base.mkdir()
    model,results,requests,intents=native_rate_records(base)
    intents[2]['time']=100;intents[3]['time']=115
    write(base/'stage123/results.jsonl',results);write(base/'stage123/requests.jsonl',requests)
    write(base/'stage123/request_intents.jsonl',intents)
    assert progress.observed_rate(base,model)==2/15


def test_missing_current_model_evidence_never_falls_back_to_other_model_history(tmp_path):
    old=tmp_path/'old';old.mkdir()
    write(old/'stage123/results.jsonl',[{'asin':'A','goods_id':str(i),'response_id':'remote',
        'api_status':'success','model':'old-remote','elapsed_seconds':1} for i in range(12)])
    write(old/'stage123/requests.jsonl',[{'response_id':'remote','pairs':12}])
    base=tmp_path/'task';base.mkdir()
    write(progress.candidate_path(base),[{'asin':'A','goods_id':'pending'}])
    cat={'stage3':{'model':'old-remote','batch_size':1,'provider':{
        'mode':'local_model','model':'LiquidAI/d1-3B','base_url':'http://127.0.0.1:7863/v1/decide',
        'protocol':'decisions','workers':1}}}
    result=progress.snapshot(base,cat)
    assert result['rate_per_hour'] is None and result['eta_seconds'] is None
    assert result['rate_source'] is None and result['historical_sample_groups']==0


def test_stopped_authorization_never_constructs_paid_client(tmp_path,monkeypatch):
    import openai
    import run_category_layer3 as runner
    monkeypatch.setenv('DEEPSEEK_API_KEY','offline-test')
    monkeypatch.setattr(openai,'OpenAI',lambda **kw:(_ for _ in ()).throw(AssertionError('paid request')))
    marker=tmp_path/'paid_enabled.json';marker.write_text('{}')
    (tmp_path/'STOP').write_text('{}')
    args=SimpleNamespace(validate_only=False,max_requests=0,results=tmp_path/'results.jsonl',authorization=marker)
    runner.consume(args,{'stage3':{'modules':{'one':100},'max_concurrency':1}},tmp_path/'blocked.json','offline',
        iter([{'asin':'A','goods_id':'1','amazon_image_path':'unused','pair_revision':'p'}]))
    assert not args.results.exists() and not (tmp_path/'request_intents.jsonl').exists()
def test_readonly_snapshot_document_marks_changes_without_claiming_current(tmp_path,monkeypatch):
    from selection_automation import stage3_progress as progress
    (tmp_path/'category.json').write_text('{}',encoding='utf-8')
    identities=iter([('before',(),False),('after',(),False)])
    monkeypatch.setattr(progress,'snapshot_signature',lambda *args:next(identities))
    monkeypatch.setattr(progress,'snapshot',lambda *args,**kwargs:{'total':7,'completed':5})
    document=progress.snapshot_document(tmp_path)
    assert document['category_key']==tmp_path.name
    assert document['progress']['completed']==5 and document['stable'] is False
    assert document['signature'][0]=='before'
