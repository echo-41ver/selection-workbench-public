import json
from copy import deepcopy
from pathlib import Path
import sys
import pytest
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'scripts'))
from plain_rules import apply_plain_rules,UnsupportedRules
from generate_category import CategoryGenerationError
from rule_editor import draft_view


def fixture():return json.loads((ROOT/'config/categories/power_drill.json').read_text('utf-8'))
def response(cat):
    view=draft_view(cat,'x')
    return dict(supported=True,stage1_attributes=cat['stage1']['attributes'],stage2_threshold=view['stage2_threshold'],stage3_prompt=view['stage3_text'],plain_summary='先用文字排除明确不同的商品，再按图片相似度筛选，最后比较清晰可见的结构。',changes=[])


def test_explain_does_not_change_rules_or_call_twice():
    cat=fixture();calls=[]
    out=apply_plain_rules(cat,'',model=lambda *a,**k:(calls.append(k) or response(cat)))
    assert len(calls)==1 and calls[0]['retry_truncated'] is False
    assert calls[0]['max_tokens']==8000
    assert out['stage1']==cat['stage1'] and out['stage2']==cat['stage2'] and out['stage3']==cat['stage3']


def test_visual_instruction_saves_locally_without_paid_echo_or_other_layer_mutation():
    cat=fixture();before=deepcopy(cat)
    out=apply_plain_rules(cat,'只比较实际出售的泵体及管路，背景油桶不参与判断。',scope='stage3',
                         model=lambda *a,**k:pytest.fail('visual prose must not call paid model'))
    assert out['stage3']['prompt_override']=='只比较实际出售的泵体及管路，背景油桶不参与判断。'
    assert out['stage3']['modules']==before['stage3']['modules']
    assert out['stage1']==before['stage1'] and out['stage2']==before['stage2'] and cat==before


def test_explain_retries_explicit_truncation_once_without_changing_rules():
    cat=fixture();before=deepcopy(cat);calls=[]
    def model(prompt,**kwargs):
        calls.append(kwargs)
        if len(calls)==1: raise CategoryGenerationError('截断','MODEL_OUTPUT_TRUNCATED')
        return response(cat)
    out=apply_plain_rules(cat,'',model=model)
    assert calls==[{'max_tokens':8000,'retry_truncated':False},{'max_tokens':16000,'retry_truncated':False}]
    assert cat==before
    assert all(out[key]==before[key] for key in ('stage1','stage2','stage3'))


@pytest.mark.parametrize('code,expected_calls',[
    ('MODEL_OUTPUT_TRUNCATED',2),('MODEL_NETWORK_ERROR',1),
    ('MODEL_INVALID_JSON',1),('MODEL_HTTP_429',1)])
def test_explain_failure_is_bounded_and_describes_explanation_only(code,expected_calls):
    cat=fixture();before=deepcopy(cat);calls=[]
    def model(*args,**kwargs):
        calls.append(kwargs)
        raise CategoryGenerationError('品类生成失败；未创建任务',code)
    with pytest.raises(CategoryGenerationError,match='中文说明生成失败.*原执行规则已保留') as error:
        apply_plain_rules(cat,'',model=model)
    assert error.value.code==code and len(calls)==expected_calls
    assert '未创建任务' not in str(error.value)
    assert cat==before


def test_explain_retry_still_refuses_rule_mutation():
    cat=fixture();before=deepcopy(cat);calls=[]
    def model(*args,**kwargs):
        calls.append(kwargs)
        if len(calls)==1: raise CategoryGenerationError('截断','MODEL_OUTPUT_TRUNCATED')
        raw=response(cat);raw['stage2_threshold']=2
        return raw
    with pytest.raises(ValueError,match='仅解释请求不得修改规则'):
        apply_plain_rules(cat,'',model=model)
    assert len(calls)==2 and cat==before


def test_edit_does_not_retry_truncation():
    calls=[]
    def model(*args,**kwargs):
        calls.append(kwargs)
        raise CategoryGenerationError('截断','MODEL_OUTPUT_TRUNCATED')
    with pytest.raises(CategoryGenerationError,match='规则修改生成失败.*原执行规则已保留'):
        apply_plain_rules(fixture(),'修改门槛',model=model)
    assert calls==[{'max_tokens':16000,'retry_truncated':False}]


def test_apply_only_allowed_fields():
    cat=fixture();raw=response(cat);raw.update(stage2_threshold=75,changes=['第二层门槛改为75分。'])
    out=apply_plain_rules(cat,'门槛改为75分',model=lambda *a,**k:raw)
    assert out['stage2']['candidate_threshold']==75
    assert out['amazon']==cat['amazon'] and out['category_key']==cat['category_key']
    assert out['stage3']['modules']==cat['stage3']['modules']


@pytest.mark.parametrize('kind',['unsupported','extra','english','explain_mutation','bad_threshold'])
def test_invalid_or_unexpressible_refuses_original_mutation(kind):
    cat=fixture();before=deepcopy(cat);raw=response(cat);instruction='修改'
    if kind=='unsupported':raw['supported']=False
    if kind=='extra':raw['company']={}
    if kind=='english':raw['plain_summary']='English JSON'
    if kind=='explain_mutation':raw['stage2_threshold']=2;instruction=''
    if kind=='bad_threshold':raw['stage2_threshold']=float('nan')
    with pytest.raises(ValueError):apply_plain_rules(cat,instruction,model=lambda *a,**k:raw)
    assert cat==before
