"""No network/provider calls: exact repair scope and durable one-shot budget."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
from selection_automation import schema_billing_retry as repair
from selection_automation.stage3_billing import billing_hold,retry_authorizations,authorized_retry_attempt
from selection_automation.match_criteria import selected,select
from test_match_criteria import configured,products


def write(path,rows):
    path.parent.mkdir(parents=True,exist_ok=True)
    path.write_text(''.join(json.dumps(r)+'\n' for r in rows),encoding='utf-8')


@pytest.fixture
def failed(tmp_path,products):
    category=configured(products);category['category_key']='fixture'
    ids=[r['id'] for r in selected(category,'text')]
    category['match_criteria']=select(category['match_criteria'],ids)
    category['match_criteria']['visual_mode']='auto'
    from selection_automation.stage3_provider import settings,apply,from_category
    apply(category,settings({'mode':'multimodal_api','base_url':'https://api.deepseek.com','model':'deepseek-flash','protocol':'responses','reasoning_effort':'default','workers':1,'concurrency_mode':'manual'}))
    folder=tmp_path/'runs/fixture/stage123';folder.mkdir(parents=True)
    images={side:tmp_path/(side+'.image') for side in ('amazon','temu')}
    for side,path in images.items():path.write_bytes(('fixture-'+side).encode())
    pair={'asin':'A','goods_id':'T','pair_revision':'pair-unchanged','vector_score':99,
          **{side+'_image_path':str(path) for side,path in images.items()}}
    binding=repair.protocol_binding(category,pair)
    intent={'request_id':'original','status':'started','pairs':1,'vision_revision_keys':[binding['original_key']]}
    result={**{k:pair[k] for k in ('asin','goods_id','pair_revision')},'vision_revision_key':binding['original_key'],
            'api_status':'failed','billing_state':'UNKNOWN','failure_status':400,'attempt':1}
    error={'code':'invalid_request_error','type':'invalid_request_error','message':repair.ERROR+' (request_id: provider-original)'}
    provider={k:from_category(category)[k] for k in ('mode','base_url','model','protocol')}
    key_identity=hashlib.sha256(b'https://api.deepseek.com\nTEST_ONLY_KEY').hexdigest()
    block={'status':400,'key_identity':key_identity,'provider':provider,'provider_error':error}
    write(folder/'request_intents.jsonl',[intent]);write(folder/'results.jsonl',[result])
    write(folder/'exception_diagnostics.jsonl',[{'request_id':'original','status_code':400,'provider_error':error}])
    (folder/'provider_blocked.json').write_text(json.dumps(block),encoding='utf-8')
    candidate=folder.parent/'stage12/candidates.jsonl';write(candidate,[pair])
    return SimpleNamespace(folder=folder,category=category,pair=pair,binding=binding,candidate=candidate,
        key_identity=key_identity,block=block,intent=intent,result=result)


def test_exact_old_to_new_repair_keeps_unknown_and_block_bytes(failed):
    f=failed;original={p:p.read_bytes() for p in [f.folder/'request_intents.jsonl',f.folder/'results.jsonl',f.folder/'provider_blocked.json']}
    auth=repair.create_authorization(f.folder,f.category,f.candidate)
    assert auth['scope'][0]['original_vision_revision_key']==f.binding['original_key']
    assert auth['scope'][0]['retry_vision_revision_key']==f.binding['retry_key']
    assert auth['original_protocol_sha256']!=auth['retry_protocol_sha256']
    assert all(p.read_bytes()==b for p,b in original.items())
    view=retry_authorizations(f.folder/'request_intents.jsonl',f.folder/'results.jsonl')
    assert authorized_retry_attempt(view,('A','T'),[f.binding['retry_key']])==2
    assert authorized_retry_attempt(view,('A','T'),[f.binding['original_key']]) is None
    assert not billing_hold(f.folder/'request_intents.jsonl',f.folder/'results.jsonl')['pairs']
    assert 'settled' not in (f.folder/'request_intents.jsonl').read_text()


@pytest.mark.parametrize('change',['image','rules','provider','model','unrepaired','other_error','401','402','403','429','partial_group'])
def test_nonexact_repair_or_external_gate_cannot_get_consent_budget(failed,change):
    f=failed
    if change=='image':Path(f.pair['temu_image_path']).write_bytes(b'changed-image')
    elif change=='rules':f.category['match_criteria']['automatic_focus']=['changed questions']
    elif change in ('provider','model'):
        field='base_url' if change=='provider' else 'model'
        f.category['stage3']['provider'][field]='https://other.invalid' if change=='provider' else 'different-model'
    elif change=='unrepaired':f.category['match_criteria']['visual_mode']='selected'
    elif change=='partial_group':
        f.intent.update(pairs=2,vision_revision_keys=[f.binding['original_key'],'missing']);write(f.folder/'request_intents.jsonl',[f.intent])
    else:
        f.block['status']=400 if change=='other_error' else int(change)
        if change=='other_error':f.block['provider_error']['message']='Account quota exceeded'
        (f.folder/'provider_blocked.json').write_text(json.dumps(f.block),encoding='utf-8')
    with pytest.raises((ValueError,KeyError)):
        repair.create_authorization(f.folder,f.category,f.candidate)
    assert not (f.folder/repair.FILE).exists()


def test_claim_is_durable_exact_and_no_second_authorization(failed):
    f=failed;auth=repair.create_authorization(f.folder,f.category,f.candidate)
    repair.validate_current(auth,f.category,f.pair,f.key_identity,f.folder)
    with pytest.raises(ValueError):repair.validate_current(auth,f.category,f.pair,'other-key-identity',f.folder)
    repair.claim(f.folder,auth['authorization_id'],'new-request')
    # A crash before intent/result does not restore this one send budget.
    with pytest.raises(ValueError):repair.claim(f.folder,auth['authorization_id'],'another-request')
    with pytest.raises(ValueError):repair.create_authorization(f.folder,f.category,f.candidate)
    view=retry_authorizations(f.folder/'request_intents.jsonl',f.folder/'results.jsonl')
    assert view['schema_retry']['claimed']
    assert authorized_retry_attempt(view,('A','T'),[f.binding['retry_key']]) is None


def test_tamper_or_missing_audit_fails_closed(failed):
    f=failed;auth=repair.create_authorization(f.folder,f.category,f.candidate)
    (f.folder/'schema-retry-audit'/auth['authorization_id']/'provider_blocked.json').unlink()
    assert billing_hold(f.folder/'request_intents.jsonl',f.folder/'results.jsonl')['fail_closed']


@pytest.mark.parametrize('field,value',[('max_extra_attempts',True),('authorized_at',float('nan')),
    ('authorized_at',float('inf')),('authorized_at',float('-inf'))])
def test_malformed_budget_or_time_fails_closed(failed,field,value):
    f=failed;auth=repair.create_authorization(f.folder,f.category,f.candidate)
    auth[field]=value;(f.folder/repair.FILE).write_text(json.dumps(auth),encoding='utf-8')
    with pytest.raises(ValueError):repair.read_authorization(f.folder)
    assert billing_hold(f.folder/'request_intents.jsonl',f.folder/'results.jsonl')['fail_closed']


@pytest.mark.parametrize('key,revision',[('old-vision','pair-unchanged'),('current','old-pair')])
def test_unrelated_historical_success_cannot_exempt_current_unknown(failed,key,revision):
    f=failed;repair.create_authorization(f.folder,f.category,f.candidate)
    result={**f.result,'api_status':'success','pair_revision':revision,
            'vision_revision_key':f.binding['retry_key'] if key=='current' else key,'attempt':1}
    write(f.folder/'results.jsonl',[f.result,result])
    view=repair.billing_view(f.folder)
    assert not view['success_pairs']
    assert authorized_retry_attempt(view,('A','T'),[f.binding['retry_key']])==2


def test_stale_request_cannot_authorize(failed):
    f=failed
    with pytest.raises(ValueError):repair.create_authorization(f.folder,f.category,f.candidate,'different-request')
    assert not (f.folder/repair.FILE).exists()


def test_runner_filters_all_new_pairs_and_consumed_retry(failed,monkeypatch):
    import run_category_layer3 as runner
    f=failed;auth=repair.create_authorization(f.folder,f.category,f.candidate)
    write(f.candidate,[f.pair,{**f.pair,'goods_id':'MARKET-NOT-AUTHORIZED'}])
    monkeypatch.setattr(runner,'load_category',lambda p:f.category)
    monkeypatch.setattr(runner,'ROOT',f.folder.parents[2])
    monkeypatch.setattr(runner,'admitted',lambda *a:True)
    monkeypatch.setattr('selection_automation.codex_visual_review.load_reviews',lambda *a,**k:{})
    batches=[]
    monkeypatch.setattr(runner,'consume',lambda args,cat,fatal,key,pending:batches.append(list(pending)))
    args=SimpleNamespace(category=Path('fixture'),results=f.folder/'results.jsonl',validate_only=False,max_pairs=0)
    runner.run(args,f.candidate)
    assert [r['goods_id'] for r in batches[0]]==['T'] and batches[0][0]['attempt']==2
    repair.claim(f.folder,auth['authorization_id'],'new-request')
    runner.run(args,f.candidate)
    assert batches[1]==[]


def test_web_consent_does_not_launch_or_settle(failed,monkeypatch):
    import web_console as web
    f=failed;console=web.Console(f.folder.parents[2]);console.key='fixture'
    monkeypatch.setattr(console,'current_run_key',lambda:('fixture',False))
    monkeypatch.setattr('selection_automation.stage3_progress.controller_alive',lambda b:False)
    monkeypatch.setattr('selection_automation.stage3_progress.candidate_path',lambda b:f.candidate)
    monkeypatch.setattr(web,'codex_review_control',lambda *a,**k:{'running':False})
    monkeypatch.setattr(console,'invoke',lambda *a,**k:pytest.fail('consent must not launch'))
    (f.folder.parent/'category.json').write_text(json.dumps(f.category),encoding='utf-8')
    before=(f.folder/'request_intents.jsonl').read_bytes()
    with pytest.raises(web.ConsoleError):console.authorize_schema_retry({'category_key':'fixture','original_request_id':'original','accept_unknown_cost':False})
    result=console.authorize_schema_retry({'category_key':'fixture','original_request_id':'original','accept_unknown_cost':True})
    assert result['ok'] and '原费用仍未知' in result['message']
    assert (f.folder/'request_intents.jsonl').read_bytes()==before


@pytest.mark.parametrize('live',[True,None,False])
def test_workspace_stop_checks_both_detached_writers(tmp_path,monkeypatch,live):
    import web_console as web
    console=web.Console(tmp_path);seen=[]
    def alive(root,base,name):seen.append(name);return live if name=='temu_union' else False
    monkeypatch.setattr('local_service_recovery.service_alive',alive)
    done,states=console.workspace_writers_stopped(tmp_path/'runs/fixture')
    assert done is (live is False) and seen==['temu_union','stage12']
    assert states['temu_union'] is live


@pytest.mark.parametrize('live',[True,None,False])
def test_stop_does_not_clear_task_until_writers_confirmed_stopped(tmp_path,monkeypatch,live):
    import web_console as web
    import time
    console=web.Console(tmp_path);console.key='fixture';console.workspace_stop_pending=True
    console.workspace_stop_started=time.monotonic()-35;cleared=[]
    monkeypatch.setattr(console,'clear_workspace',lambda:cleared.append(True))
    monkeypatch.setattr(console,'workspace_writers_stopped',lambda base:(live is False,{'temu_union':live,'stage12':False}))
    monkeypatch.setattr('selection_automation.stage3_progress.controller_alive',lambda base:False)
    monkeypatch.setattr(web,'codex_review_control',lambda *a,**k:{'running':False})
    console.finish_workspace_stop(None)
    assert bool(cleared) is (live is False)
    if live is not False:
        assert console.key=='fixture' and console.workspace_stop_pending and '身份核对' in console.phase


@pytest.mark.parametrize('outcome',['unknown','received-invalid','success','high-review'])
def test_consumer_sends_only_original_pair_once_and_never_rearms(failed,monkeypatch,outcome):
    import run_category_layer3 as runner
    from contextlib import nullcontext
    from selection_automation import stage3_provider,stage3_transport
    f=failed
    if outcome=='high-review':
        f.category['stage3']['provider'].pop('reasoning_effort')
        f.binding=repair.protocol_binding(f.category,f.pair)
        f.intent['vision_revision_keys']=[f.binding['original_key']]
        f.result['vision_revision_key']=f.binding['original_key']
        write(f.folder/'request_intents.jsonl',[f.intent]);write(f.folder/'results.jsonl',[f.result])
    auth=repair.create_authorization(f.folder,f.category,f.candidate)
    f.category['stage3']['reasoning_effort']='low'
    # Fixture defaults include explicit provider reasoning; leave actual binding unchanged.
    sends=[]
    class Client:
        def __enter__(self):return self
        def __exit__(self,*a):pass
        def prepare(self,*a,**k):return b'fixture-not-network'
        @property
        def responses(self):return self
        def create(self,**kwargs):
            sends.append(kwargs)
            if outcome=='unknown':
                error=RuntimeError('fixture HTTP400');error.status_code=400
                error.body={'error':{'code':'invalid_request_error','message':'fixture second rejection'}}
                raise error
            return SimpleNamespace(usage=None,id='fixture-response',status='completed',output_text='fixture')
    monkeypatch.setattr(runner,'ROOT',f.folder.parents[2])
    monkeypatch.setattr(stage3_provider,'request_key',lambda provider:'TEST_ONLY_KEY')
    monkeypatch.setattr(stage3_provider,'VisualClient',lambda *a,**k:Client())
    monkeypatch.setattr(stage3_transport,'create_transport',lambda *a,**k:nullcontext(object()))
    monkeypatch.setattr(runner,'cached_image_url',lambda *a:'data:image/png;base64,fixture')
    monkeypatch.setattr('selection_automation.visual_examples.example_content',lambda *a:[])
    if outcome=='received-invalid':
        monkeypatch.setattr(runner,'parse_visual_response_compatible',lambda *a,**k:(_ for _ in ()).throw(ValueError('fixture format')))
    else:
        monkeypatch.setattr(runner,'parse_visual_response_compatible',lambda *a,**k:({},[]))
        monkeypatch.setattr(runner,'decode',lambda *a:{'C00':{'overall_confidence':.1 if outcome=='high-review' else 1}})
        monkeypatch.setattr(runner,'score',lambda *a:{'hard_conflicts':['fixture conflict'] if outcome=='high-review' else [],'state':'不同款'})
    pair={**f.pair,'vision_revision_key':f.binding['retry_key'],'attempt':2}
    args=SimpleNamespace(results=f.folder/'results.jsonl',validate_only=False,max_requests=0)
    before=(f.folder/'provider_blocked.json').read_bytes()
    try:runner.consume(args,f.category,f.folder/'provider_blocked.json',f.key_identity,iter([pair,pair,{**pair,'goods_id':'MARKET'}]))
    except SystemExit:pass
    assert len(sends)==1 and args.max_requests==1
    assert (f.folder/'provider_blocked.json').read_bytes()==before
    assert repair.read_authorization(f.folder)['claimed']
    try:runner.consume(args,f.category,f.folder/'provider_blocked.json',f.key_identity,iter([pair]))
    except SystemExit:pass
    assert len(sends)==1
    intents=repair._rows(f.folder/'request_intents.jsonl')
    assert len([r for r in intents if r.get('status')=='started'])==2
    assert not any(r.get('request_id')=='original' and r.get('status')=='settled' for r in intents)
    if outcome=='high-review':
        evidence=repair._rows(f.folder/'evidence_rechecks.jsonl')
        assert len(evidence)==1 and evidence[0]['requires_human_review']
        assert evidence[0]['visual_facts']['overall_confidence']==.1 and evidence[0]['action']=='ONE_SHOT_BUDGET_EXHAUSTED'
        results=repair._rows(f.folder/'results.jsonl')
        assert results[-1]['api_status']=='failed' and results[-1]['billing_state']=='KNOWN'
        assert 'SCHEMA_RETRY_REVIEW_REQUIRED' in results[-1]['failure_reason']
        assert results[-1]['response_id']=='fixture-response'
        assert repair._rows(f.folder/'response_errors.jsonl')[-1]['response_id']=='fixture-response'


def test_consumed_budget_watcher_never_starts_another_consumer(failed,monkeypatch):
    import watch_stage3 as watch
    f=failed;auth=repair.create_authorization(f.folder,f.category,f.candidate)
    repair.claim(f.folder,auth['authorization_id'],'new-request')
    (f.folder/'paid_enabled.json').write_text('{}')
    category_path=f.folder.parent/'category.json';category_path.write_text(json.dumps(f.category),encoding='utf-8')
    monkeypatch.setattr(watch,'ROOT',f.folder.parents[2])
    monkeypatch.setattr(watch,'ensure_publisher',lambda *a:None)
    monkeypatch.setattr(watch.subprocess,'Popen',lambda *a,**k:pytest.fail('consumed budget cannot launch consumer'))
    watch.watch(category_path)
    assert '预算已使用' in json.loads((f.folder/'controller.json').read_text('utf-8'))['phase']


def test_progress_signature_tracks_authorization_and_durable_claim(failed):
    from selection_automation.stage3_progress import snapshot_signature
    f=failed;before=snapshot_signature(f.folder.parent,f.category)
    auth=repair.create_authorization(f.folder,f.category,f.candidate)
    authorized=snapshot_signature(f.folder.parent,f.category)
    repair.claim(f.folder,auth['authorization_id'],'new-request')
    claimed=snapshot_signature(f.folder.parent,f.category)
    assert before!=authorized!=claimed


def test_retry_scope_preserves_other_completed_history_but_blocks_new_pairs(failed,monkeypatch):
    from selection_automation import stage3_progress as progress,vision
    f=failed;repair.create_authorization(f.folder,f.category,f.candidate)
    completed={**f.pair,'goods_id':'OTHER-COMPLETED'};waiting={**f.pair,'goods_id':'OTHER-PENDING'}
    write(f.candidate,[f.pair,completed,waiting])
    write(f.folder/'results.jsonl',[f.result,{**completed,'vision_revision_key':vision.compatible_vision_keys(f.category,completed)[0],
          'api_status':'success','attempt':1}])
    monkeypatch.setattr(progress,'candidate_path',lambda base:f.candidate)
    monkeypatch.setattr(progress,'admitted',lambda *a:True)
    result=progress.snapshot(f.folder.parent,f.category,history=False)
    assert result['api_completed']==1 and result['total']==3
    assert result['schema_retry']['excluded_pairs']==1 and result['runnable_pending']==1
