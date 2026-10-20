from copy import deepcopy

import pytest

from selection_automation.rules import attribute_values, text_gate
from selection_automation.stage1_authoring import compile_stage1_rules, normalize_attribute_aliases


def category():
    # Saved provider response used strings despite the requested array schema.
    return {'stage1':{'text_gate':'declarative','attributes':[{
        'name':'power_source','hard_conflict':True,'values':{
            'battery':'battery operated|battery-powered|电池供电|电池式',
            'corded':'plug-in|AC adapter|插电式|电源适配器',
            'dual':'battery and plug-in|dual power|双供电'},
        'ambiguous_phrases':['rechargeable battery','USB power']}]},
        'stage3':{'modules':{},'variant_fields':[],'rule_version':'test'}}


def test_provider_pipe_strings_compile_as_phrases_without_changing_original():
    original=category();before=deepcopy(original)
    compiled=compile_stage1_rules(original)
    assert original==before and compile_stage1_rules(compiled)==compiled
    assert compiled['stage1']['attributes'][0]['values']['battery']==[
        'battery operated','battery-powered','电池供电','电池式']
    assert text_gate(compiled,'battery-powered feeder','plug-in feeder')['excluded']


@pytest.mark.parametrize('text,expected',[
    ('battery and plug-in feeder',{'dual'}),('BATTERY AND PLUG-IN',{'dual'}),
    ('plug-in feeder',{'corded'}),('电池式喂食器',{'battery'}),
    ('dual power; plug-in',{'dual','corded'}),
])
def test_more_specific_authored_alias_wins_only_within_the_same_span(text,expected):
    aliases=normalize_attribute_aliases(category()['stage1']['attributes'][0]['values'])
    assert attribute_values(text,aliases)==(expected,False)


def test_negated_specific_alias_remains_uncertain_and_cannot_hard_exclude():
    compiled=compile_stage1_rules(category())
    aliases=compiled['stage1']['attributes'][0]['values']
    assert attribute_values('not battery and plug-in',aliases)==(set(),True)
    assert not text_gate(compiled,'not battery and plug-in','plug-in')['excluded']


def test_identical_aliases_for_incompatible_values_are_still_rejected():
    value=category();value['stage1']['attributes'][0]['values']={'a':['same'],'b':['same']}
    with pytest.raises(ValueError,match='明确冲突也被规则判成不足'):
        compile_stage1_rules(value)


@pytest.mark.parametrize('values',[{'a':'||'},{'a':[]},{'a':[1]},'a|b'])
def test_invalid_alias_structures_are_not_silently_treated_as_characters(values):
    with pytest.raises(ValueError):normalize_attribute_aliases(values)


def test_fullwidth_separator_and_repeated_aliases_are_only_format_normalization():
    assert normalize_attribute_aliases({'x':' one ｜ two | one '})=={'x':['one','two']}
