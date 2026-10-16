from copy import deepcopy
import pytest

from test_identity_policy import category, visual_facts
from selection_automation.identity_policy import compile_policy
from selection_automation.rules import score, validate
from selection_automation.vision import protocol, decode


def configured():
    cat = compile_policy(category('刀具套装'))
    cat['stage3']['set_inventory_features'] = {'body_type': ['steak', 'chef', 'scissors']}
    return cat


def side(count=6, complete=True, kind='steak'):
    return {'complete': complete, 'groups': [{'region': '前排', 'item_type': kind, 'count': count}]}


def facts(cat, a=6, t=6, complete=True):
    value = visual_facts(cat)
    value['feature_comparisons']['body_type']['inventory'] = {
        'amazon': side(a), 'temu': side(t, complete)}
    return value


def test_eight_vs_six_handles_cannot_confirm_despite_perfect_scores():
    cat = configured()
    result = score(cat, facts(cat, 8, 6))
    assert result['total_score'] == 100
    assert result['state'] == '不同款'


def test_same_count_different_knife_types_is_identity_conflict():
    cat = configured(); value = facts(cat)
    value['feature_comparisons']['body_type']['inventory']['temu'] = side(6, kind='chef')
    assert score(cat, value)['state'] == '不同款'


def test_occlusion_keeps_unknown_not_different_or_confirmed():
    cat = configured(); value = facts(cat, 8, 6, complete=False)
    assert score(cat, value)['state'] == '待补充证据'


def test_equal_verified_inventory_confirms():
    cat = configured()
    assert score(cat, facts(cat))['state'] == '确认同款'


@pytest.mark.parametrize('kind',['other','unknown'])
def test_unrecognized_knife_type_cannot_confirm_even_if_claimed_complete(kind):
    cat=configured();cat['stage3']['set_inventory_features']['body_type'] += ['other','unknown']
    value=facts(cat)
    value['feature_comparisons']['body_type']['inventory']={s:side(7,kind=kind) for s in ('amazon','temu')}
    assert score(cat,value)['state']=='待补充证据'


def test_explicit_storage_accessory_type_does_not_make_verified_inventory_unknown():
    cat=configured();cat['stage3']['set_inventory_features']['body_type'] += ['block','bag','sheath','peeler']
    value=facts(cat)
    for s in ('amazon','temu'):
        value['feature_comparisons']['body_type']['inventory'][s]['groups'].append({'region':'侧边刀袋','item_type':'bag','count':1})
    assert score(cat,value)['state']=='确认同款'


def test_contextual_branding_cannot_alone_become_identity_exclusion():
    cat=configured();value=facts(cat)
    context=next(f for f in cat['stage3']['identity_policy']['features'] if f['role']=='context')
    value['identity']['differences']=[{'field':context['id'],'amazon':'Brand A','temu':'Brand B','kind':'IDENTITY','variant_field':'NONE'}]
    value['identity']['verdict']='DIFFERENT'
    assert score(cat,value)['state']=='待复核'
    value['identity']['differences'].append({'field':'body_type','amazon':'前排八刀','temu':'前排六刀','kind':'IDENTITY','variant_field':'NONE'})
    assert score(cat,value)['state']=='不同款'


def test_old_multiple_knives_evidence_without_inventory_is_not_confirmation():
    cat = configured(); value = facts(cat)
    del value['feature_comparisons']['body_type']['inventory']
    value['identity']['uncertainties'] = ['前排小刀数量被遮挡无法确认']
    assert score(cat, value)['state'] == '待补充证据'


def test_noncritical_uncertainty_does_not_block_verified_inventory():
    cat = configured(); value = facts(cat)
    value['identity']['uncertainties'] = ['包装背面文字未展示']
    assert score(cat, value)['state'] == '确认同款'


def test_inventory_protocol_requires_structured_bilateral_counts():
    cat = configured(); prompt, schema = protocol(cat, 1)
    feature = schema['properties']['candidates']['items']['properties']['f']['properties']['body_type']
    assert 'inventory' in feature['required']
    assert '逐柄数' in prompt
    raw = {'candidates': [{'i': 'x', 'c': .95, 'v': visual_facts(cat)['identity'],
           'm': {str(i): {'r': 'S', 'a': '弧形柄结构', 't': '弧形柄结构', 'h': 'NONE'}
                 for i in range(len(cat['stage3']['modules']))},
           'f': {'body_type': {'r': 'S', 'a': '前排八把', 't': '前排六把',
                 'inventory': {'amazon': side(8), 'temu': side(6)}}}}]}
    decoded = decode(cat, raw, ['x'])['x']
    assert score(cat, decoded)['state'] == '不同款'
    del raw['candidates'][0]['f']['body_type']['inventory']
    with pytest.raises(ValueError): decode(cat, raw, ['x'])


@pytest.mark.parametrize('count', [True, 0, -1, 6.0, '6', 201])
def test_invalid_count_cannot_be_fact(count):
    cat = configured(); value = facts(cat)
    value['feature_comparisons']['body_type']['inventory']['amazon']['groups'][0]['count'] = count
    with pytest.raises(ValueError): score(cat, value)


def test_inventory_cannot_bind_variant_or_unconfigured_feature():
    cat = configured(); cat['stage3']['set_inventory_features'] = {'finish': ['knife']}
    with pytest.raises(ValueError): validate(cat)


def test_unconfigured_other_categories_keep_existing_protocol():
    cat = compile_policy(category()); value = visual_facts(cat)
    assert score(cat, value)['state'] == '确认同款'
    _, schema = protocol(cat, 1)
    assert 'inventory' not in schema['properties']['candidates']['items']['properties']['f']['properties']['body_type']['properties']


def test_legacy_null_policy_without_inventory_remains_supported():
    from selection_automation.set_inventory import requirements
    assert requirements({'stage3': {'identity_policy': None}}) == {}
