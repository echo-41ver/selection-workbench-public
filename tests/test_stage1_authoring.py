from copy import deepcopy
import json
from pathlib import Path
import pytest

from selection_automation.stage1_authoring import compile_stage1_rules
from selection_automation.rules import text_gate


def category():
    return {'stage1':{'text_gate':'declarative','attributes':[
        {'name':'connection','hard_conflict':True,'values':{
            'wired':['wired keyboard','有线键盘'],'wireless':['wireless keyboard','无线键盘']},
         'ambiguous_phrases':['for','适用于','compatible with','wired and wireless']}]},
        'stage3':{'modules':{},'variant_fields':[],'rule_version':'test'}}


@pytest.mark.parametrize('left,right',[
    ('Wired keyboard for PC','Wireless keyboard for Windows'),
    ('有线键盘，适用于电脑','无线键盘，适用于电脑'),
    ('Wired keyboard compatible with Windows','Wireless keyboard compatible with Mac'),
])
def test_generic_host_context_cannot_mask_explicit_conflict(left,right):
    original=category();before=deepcopy(original);compiled=compile_stage1_rules(original)
    assert text_gate(compiled,left,right)['stage1_status']=='TEXT_HARD_EXCLUDE'
    assert original==before
    assert compile_stage1_rules(compiled)==compiled


@pytest.mark.parametrize('left,right',[
    ('Case for wired keyboard','Wireless keyboard'),
    ('适用于有线键盘的配件','无线键盘'),
    ('Wireless keyboard compatible with wired keyboard','Wired keyboard'),
    ('Wired and wireless keyboard','Wired keyboard'),
    ('Keyboard for PC','Wireless keyboard'),
    ('Not a wired keyboard','Wireless keyboard'),
])
def test_real_compatibility_negation_multimode_and_missing_evidence_stay_uncertain(left,right):
    assert not text_gate(compile_stage1_rules(category()),left,right)['excluded']


def test_silently_disabled_hard_rule_is_rejected_before_task_creation():
    cat=category();cat['stage1']['attributes'][0]['ambiguous_phrases']=['wired keyboard','有线键盘']
    with pytest.raises(ValueError,match='明确冲突也被规则判成不足'):compile_stage1_rules(cat)


def test_variant_and_nonhard_attributes_are_not_forced_to_exclude():
    cat=category();cat['stage3']['variant_fields']=['connection']
    assert not text_gate(compile_stage1_rules(cat),'wired keyboard','wireless keyboard')['excluded']


def test_frozen_reader_does_not_silently_rewrite_existing_tasks(tmp_path):
    from selection_automation.category_config import load_category
    root=Path(__file__).resolve().parents[1]
    cat=json.loads((root/'config/categories/power_drill.json').read_text('utf-8'))
    cat['stage1']['attributes'][0]['ambiguous_phrases']=['for']
    path=tmp_path/'category.json';path.write_text(json.dumps(cat),encoding='utf-8')
    assert load_category(path)['stage1']['attributes'][0]['ambiguous_phrases']==['for']
    authored=load_category(path,compile_authored_policy=True)
    assert 'for' not in authored['stage1']['attributes'][0]['ambiguous_phrases']
