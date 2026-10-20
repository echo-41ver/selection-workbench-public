"""The valid zero-checkbox UI case must not emit a forbidden empty object."""
from copy import deepcopy

import pytest

from test_match_criteria import configured, products
from selection_automation.compact_visual import protocol, decode
from selection_automation.match_criteria import select, selected
from selection_automation.rules import score


def no_visual_conditions(products, mode):
    category=configured(products)
    text_ids=[row['id'] for row in selected(category,'text')]
    category['match_criteria']=select(category['match_criteria'],text_ids)
    if mode=='auto':category['match_criteria']['visual_mode']='auto'
    else:category['match_criteria'].pop('visual_mode',None)
    assert not selected(category,'visual') and selected(category,'text')
    return category


def candidate(identity='C00', relation='S'):
    return {'i':identity,'r':relation,'a':'圆形镜头与右侧支架','t':'圆形镜头与右侧支架',
            'why':'比较实际主体基础款','c':.95}


def assert_no_empty_objects(value):
    if isinstance(value,dict):
        if value.get('type')=='object':
            assert value['properties']
            assert set(value['required'])==set(value['properties'])
            assert value['additionalProperties'] is False
        for child in value.values():assert_no_empty_objects(child)
    elif isinstance(value,list):
        for child in value:assert_no_empty_objects(child)


@pytest.mark.parametrize('mode',['auto','selected'])
@pytest.mark.parametrize('count',[1,2])
def test_zero_visual_conditions_wire_has_no_empty_object(products,mode,count):
    category=no_visual_conditions(products,mode)
    before=deepcopy(category)
    prompt,schema=protocol(category,count)
    item=schema['properties']['candidates']['items']
    assert set(item['properties'])=={'i','r','a','t','why','c'}
    assert 'q' not in item['required'] and '不输出q字段' in prompt and 'q={}' not in prompt
    assert_no_empty_objects(schema)
    assert category==before


@pytest.mark.parametrize('mode',['auto','selected'])
@pytest.mark.parametrize('relation',['S','L','D','U'])
def test_zero_condition_new_and_historical_wire_produce_identical_facts_and_score(products,mode,relation):
    category=no_visual_conditions(products,mode)
    current={'candidates':[candidate(relation=relation)]}
    historical=deepcopy(current);historical['candidates'][0]['q']={}
    current_facts=decode(category,current,['C00'])['C00']
    old_facts=decode(category,historical,['C00'])['C00']
    assert current_facts==old_facts
    assert current_facts['condition_comparisons']=={}
    assert score(category,current_facts)==score(category,old_facts)
    if mode=='selected':assert score(category,current_facts)['state']=='待补充证据'


@pytest.mark.parametrize('invalid_q',[None,[],{'unexpected':{'r':'S','a':'x','t':'x'}}])
def test_legacy_q_is_accepted_only_as_an_empty_object(products,invalid_q):
    category=no_visual_conditions(products,'auto');row=candidate();row['q']=invalid_q
    with pytest.raises(ValueError):decode(category,{'candidates':[row]},['C00'])


@pytest.mark.parametrize('missing_key',['i','r','a','t','why','c'])
def test_omitting_q_does_not_make_other_required_facts_optional(products,missing_key):
    category=no_visual_conditions(products,'auto');row=candidate();row.pop(missing_key)
    with pytest.raises(ValueError):decode(category,{'candidates':[row]},['C00'])


def test_zero_condition_group_still_binds_candidates_and_rejects_extra_fields(products):
    category=no_visual_conditions(products,'auto')
    raw={'candidates':[candidate(),{**candidate('C01'),'q':{}}]}
    assert set(decode(category,raw,['C00','C01']))=={'C00','C01'}
    bad=deepcopy(raw);bad['candidates'][1]['i']='C00'
    with pytest.raises(ValueError):decode(category,bad,['C00','C01'])
    bad=deepcopy(raw);bad['candidates'][0]['opaque']={}
    with pytest.raises(ValueError):decode(category,bad,['C00','C01'])
    bad=deepcopy(raw);bad['extra']='unexpected'
    with pytest.raises(ValueError):decode(category,bad,['C00','C01'])


@pytest.mark.parametrize('bad_q',['omitted',{}, {'unexpected':{'r':'S','a':'x','t':'x'}},
                                  {'body_type':{'r':'S','a':'x'}},
                                  {'body_type':{'r':'X','a':'x','t':'x'}}])
def test_selected_visual_evidence_is_still_required_and_strict(products,bad_q):
    category=configured(products);row=candidate()
    prompt,schema=protocol(category,1)
    item=schema['properties']['candidates']['items']
    assert set(item['properties']['q']['properties'])=={'body_type'} and 'q' in item['required']
    assert_no_empty_objects(schema)
    if bad_q!='omitted':row['q']=bad_q
    with pytest.raises(ValueError):decode(category,{'candidates':[row]},['C00'])
