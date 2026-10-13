import json
from types import SimpleNamespace
import os
from pathlib import Path

import pytest

from selection_automation.stage3_billing import billing_hold,retry_authorizations,authorized_retry_attempt
from selection_automation import stage3_progress as progress
from test_stage3_billing_isolation import write
import run_category_layer3 as runner


def fixture(base,baseline=2):
    intents=base/'request_intents.jsonl';results=base/'results.jsonl'
    write(intents,[{'request_id':'original','status':'started','pairs':1,'vision_revision_keys':['key']}])
    write(results,[{'asin':'A','goods_id':'T','vision_revision_key':'key','attempt':baseline,
                   'api_status':'failed','billing_state':'UNKNOWN'}])
    auth={'version':1,'authorizations':[{'authorization_id':'human-approval','authorized_at':100,
          'max_extra_attempts':1,'scope':[{'original_request_id':'original','asin':'A','goods_id':'T',
                                        'vision_revision_key':'key','baseline_attempt':baseline}]}]}
    path=base/'billing_retry_authorizations.json';path.write_text(json.dumps(auth),encoding='utf-8')
    return intents,results,path,auth


def test_original_unknown_can_retry_once_without_settling_or_erasing_invoice(tmp_path):
    intents,results,path,auth=fixture(tmp_path);before=intents.read_bytes()
    hold=billing_hold(intents,results)
    assert not hold['pairs'] and not hold['fail_closed']
    a=retry_authorizations(intents,results)
    assert authorized_retry_attempt(a,('A','T'),['key'])==3
    assert authorized_retry_attempt(a,('A','T'),['other-key']) is None
    assert intents.read_bytes()==before and runner.unresolved_request_intents(intents)=={'original'}


def test_retry_unknown_new_request_id_is_held_and_cannot_retry_twice(tmp_path):
    intents,results,path,auth=fixture(tmp_path)
    with intents.open('a') as f:f.write(json.dumps({'request_id':'retry-new','status':'started','pairs':1,'vision_revision_keys':['key']})+'\n')
    with results.open('a') as f:f.write(json.dumps({'asin':'A','goods_id':'T','vision_revision_key':'key','attempt':3,'api_status':'failed','billing_state':'UNKNOWN'})+'\n')
    hold=billing_hold(intents,results)
    assert hold['pairs']=={('A','T')} and hold['request_ids']=={'retry-new'}
    assert authorized_retry_attempt(retry_authorizations(intents,results),('A','T'),['key']) is None


@pytest.mark.parametrize('key',['key','different-compatible-key'])
def test_success_is_never_paid_again_even_across_revision(tmp_path,key):
    intents,results,path,auth=fixture(tmp_path)
    with results.open('a') as f:f.write(json.dumps({'asin':'A','goods_id':'T','vision_revision_key':key,'attempt':3,'api_status':'success'})+'\n')
    assert authorized_retry_attempt(retry_authorizations(intents,results),('A','T'),['key']) is None


@pytest.mark.parametrize('side',['amazon','temu'])
def test_old_authorized_success_blocks_billing_without_counting_changed_image_as_complete(tmp_path,monkeypatch,side):
    from selection_automation.vision import compatible_vision_keys
    root=Path(__file__).resolve().parents[1]
    category=json.loads((root/'config/categories/power_drill.json').read_text(encoding='utf-8'))
    images={name:tmp_path/name for name in ('amazon','temu')}
    for image in images.values():image.write_bytes(b'image-old')
    pair={'asin':'A','goods_id':'T','pair_revision':'p','vector_score':100,
          **{name+'_image_path':str(image) for name,image in images.items()}}
    old=compatible_vision_keys(category,pair)[0]
    folder=tmp_path/'stage123';folder.mkdir()
    intents,results,path,auth=fixture(folder)
    write(intents,[{'request_id':'original','status':'started','pairs':1,'vision_revision_keys':[old]}])
    write(results,[{**pair,'vision_revision_key':old,'attempt':2,'api_status':'failed','billing_state':'UNKNOWN'},
                   {**pair,'vision_revision_key':old,'attempt':3,'api_status':'success'}])
    auth['authorizations'][0]['scope'][0]['vision_revision_key']=old
    path.write_text(json.dumps(auth),encoding='utf-8')
    write(progress.candidate_path(tmp_path),[pair])
    preserved={file:file.read_bytes() for file in (intents,results,path)}
    before=progress.snapshot(tmp_path,category,history=False)
    assert before['api_completed']==1 and before['billing_retry_consumed_pairs']==0
    image=images[side];stat=image.stat();image.write_bytes(b'image-new')
    os.utime(image,ns=(stat.st_atime_ns,stat.st_mtime_ns))
    current=compatible_vision_keys(category,pair)[0]
    assert current!=old
    after=progress.snapshot(tmp_path,category,history=False)
    assert (after['completed'],after['api_completed'],after['failed'],after['processed'])==(0,0,0,0)
    assert (after['pending'],after['pending_after_codex'],after['runnable_pending'],after['pending_requests'])==(1,1,0,0)
    assert after['billing_retry_consumed_pairs']==1
    assert not after['billing_hold_fail_closed'] and after['billing_unknown_pairs']==0
    assert authorized_retry_attempt(retry_authorizations(intents,results),('A','T'),[current]) is None
    assert {file:file.read_bytes() for file in preserved}==preserved
    # A current free review can resolve the visual pending state without payment.
    from selection_automation import codex_visual_review
    monkeypatch.setattr(codex_visual_review,'load_reviews',lambda *args,**kwargs:{
        ('A','T',current):{'review_status':'DIFFERENT'}})
    reviewed=progress.snapshot(tmp_path,category,history=False)
    assert reviewed['api_completed']==0 and reviewed['codex_completed']==1
    assert reviewed['pending_after_codex']==0 and reviewed['billing_retry_consumed_pairs']==0


@pytest.mark.parametrize('change',['wrong_pair','wrong_key','extra_retries','missing_mapping','incomplete_group'])
def test_bad_authorization_or_missing_binding_fails_closed(tmp_path,change):
    intents,results,path,auth=fixture(tmp_path)
    item=auth['authorizations'][0]['scope'][0]
    if change=='wrong_pair':item['goods_id']='other'
    if change=='wrong_key':item['vision_revision_key']='other'
    if change=='extra_retries':auth['authorizations'][0]['max_extra_attempts']=2
    if change=='missing_mapping':results.unlink()
    if change=='incomplete_group':write(intents,[{'request_id':'original','status':'started','pairs':2,'vision_revision_keys':['key','absent']}])
    path.write_text(json.dumps(auth),encoding='utf-8')
    assert billing_hold(intents,results)['fail_closed']


def test_runner_uses_one_authorized_extra_attempt_then_skips_completed(tmp_path,monkeypatch):
    intents,results,path,auth=fixture(tmp_path)
    category=tmp_path/'category.json';category.write_text('{}')
    candidate=tmp_path/'candidate.jsonl'
    write(candidate,[{'asin':'A','goods_id':'T','pair_revision':'p','amazon_image_path':'a','temu_image_path':'t'}])
    monkeypatch.setattr(runner,'load_category',lambda p:{'stage3':{}})
    monkeypatch.setattr(runner,'compatible_vision_keys',lambda c,r:['key'])
    attempts=[]
    def consume(args,cat,fatal,key,pending):
        for row in pending:
            attempts.append(row['attempt'])
            with results.open('a') as f:f.write(json.dumps({**row,'api_status':'success'})+'\n')
    monkeypatch.setattr(runner,'consume',consume)
    args=SimpleNamespace(category=category,results=results,validate_only=False,max_pairs=0)
    runner.run(args,candidate);runner.run(args,candidate)
    assert attempts==[3]


def test_authorized_retry_inflight_not_confused_with_original_failed_row(tmp_path,monkeypatch):
    folder=tmp_path/'stage123';folder.mkdir()
    intents,results,path,auth=fixture(folder)
    with intents.open('a') as f:f.write(json.dumps({'request_id':'new-live','status':'started','time':101,
         'pairs':1,'vision_revision_keys':['key']})+'\n')
    monkeypatch.setattr(progress,'live_consumer_started',lambda base:100)
    held=progress.progress_billing_hold(tmp_path)
    assert held['inflight_pairs']==1 and not held['pairs'] and not held['fail_closed']


def test_consumer_never_accepts_missing_attempt_for_authorized_pair(tmp_path,monkeypatch):
    import openai
    intents,results,path,auth=fixture(tmp_path)
    monkeypatch.setattr(openai,'OpenAI',lambda **kwargs:pytest.fail('must not construct paid client'))
    row={'asin':'A','goods_id':'T','vision_revision_key':'other-key'}
    args=SimpleNamespace(results=results,validate_only=False,max_requests=1)
    runner.consume(args,{'stage3':{'modules':{'one':100}}},tmp_path/'blocked.json','offline',iter([row]))
