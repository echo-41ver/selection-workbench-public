import json
from pathlib import Path
from types import SimpleNamespace

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
        {'asin':'A','goods_id':str(i),'response_id':'ra' if i<10 else 'rb','api_status':'success'}
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
    write(base/'stage123/results.jsonl',[{'response_id':'failed','api_status':'failed'}])
    assert progress.observed_rate(base) is None


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
