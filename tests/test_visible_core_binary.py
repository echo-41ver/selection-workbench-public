from copy import deepcopy

import pytest

from selection_automation.identity_policy import compile_policy
from selection_automation.rules import score
from selection_automation.codex_visual_review import normalize_review
from test_identity_policy import category, visual_facts
from test_codex_visual_review import fixture


def configured():
    cat = category()
    cat['stage3']['modules'] = {'body': 70, 'rear': 10, 'appearance': 20}
    cat['stage3']['identity_policy']['features'].append({
        'id':'rear_detail','label':'后部连接','role':'identity','evidence':'image',
        'region':'component','effects':['connection'],'preserves':[],
        'scope':'后部连接形状','rule':'均展示时判断差异，不展示为未知',
        'modules':['rear'],'required_visual':False})
    cat['stage3']['identity_policy']['features'][1]['preserves'].append('rear_detail')
    cat = compile_policy(cat)
    cat['visual_decision'] = 'visible_core_binary_v1'
    facts = visual_facts(cat)
    facts['feature_comparisons']['rear_detail'].update(relation='UNKNOWN',amazon='',temu='')
    facts['modules']['rear'].update(relation='UNKNOWN',amazon_evidence='',temu_evidence='')
    facts['modules']['appearance']['relation'] = 'VARIANT'
    facts['identity']['differences'] = [{'field':'表面颜色','amazon':'红色','temu':'蓝色',
        'kind':'VARIANT','variant_field':'finish'}]
    return cat, facts


def test_color_does_not_require_unseen_optional_identity_or_invent_match():
    cat, facts = configured()
    before = deepcopy(facts)
    result = score(cat, facts)
    assert result['state'] == '同款（存在规格变体）'
    assert result['feature_comparisons']['rear_detail']['relation'] == 'UNKNOWN'
    assert facts == before
    assert 'rear_detail' in cat['stage3']['identity_policy']['features'][1]['preserves']


def test_visible_optional_core_conflict_still_excludes():
    cat, facts = configured()
    facts['feature_comparisons']['rear_detail'].update(relation='DIFFERENT',
        amazon='后部单段固定连接',temu='后部两段铰接连接')
    assert score(cat, facts)['state'] == '不同款'


def test_unestablished_core_is_excluded_with_explicit_basis():
    cat, facts = configured()
    core = next(f['id'] for f in cat['stage3']['identity_policy']['features'] if f['required_visual'])
    facts['feature_comparisons'][core].update(relation='UNKNOWN',amazon='',temu='')
    result = score(cat, facts)
    assert result['state'] == '不同款'
    assert result['decision_basis'] == 'CURRENT_IMAGE_CORE_NOT_ESTABLISHED'
    assert '当前主图未体现' in result['reason']


def test_overall_unknown_does_not_veto_explicit_same_core_and_design():
    cat, facts = configured()
    facts['identity']['verdict'] = 'UNKNOWN'
    result = score(cat, facts)
    assert result['state'] == '同款（存在规格变体）'
    assert result['decision_basis'] == 'VISIBLE_CORE_CORRESPONDENCE'
    assert facts['identity']['verdict'] == 'UNKNOWN'
    facts['identity']['design_comparison']['relation'] = 'SIMILAR'
    assert score(cat, facts)['state'] == '不同款'


def test_import_recomputes_final_status_from_facts():
    cat, facts = configured()
    pair, row = fixture('UNKNOWN')
    row['visual_facts'] = facts
    result = normalize_review(cat, row, pair, 'binary-01')
    assert result['review_status'] == 'SAME'
    assert result['model_review_status'] == 'UNKNOWN'
    row.pop('visual_facts')
    with pytest.raises(ValueError, match='完整原图事实'):
        normalize_review(cat, row, pair, 'binary-02')


def test_missing_set_inventory_cannot_confirm_from_generic_same_text():
    from test_set_inventory_identity import configured as set_category, facts as set_facts
    cat=set_category();cat['visual_decision']='visible_core_binary_v1'
    facts=set_facts(cat)
    facts['feature_comparisons']['body_type'].pop('inventory')
    assert score(cat,facts)['state']=='不同款'


@pytest.mark.parametrize('kind,complete', [('unknown', True), ('other', True), ('unknown', False)])
def test_identified_core_with_matching_visible_counts_does_not_need_type_names(kind, complete):
    from test_set_inventory_identity import configured as set_category, facts as set_facts, side
    cat=set_category();cat['visual_decision']='visible_core_binary_v1'
    cat['stage3']['set_inventory_features']['body_type'] += ['other','unknown']
    facts=set_facts(cat)
    facts['feature_comparisons']['body_type']['inventory']={s:side(7,complete,kind) for s in ('amazon','temu')}
    before=deepcopy(facts)
    assert score(cat,facts)['state']=='确认同款'
    assert facts==before
    facts['feature_comparisons']['body_type']['inventory']['temu']['groups'][0]['count']=5
    assert score(cat,facts)['state']=='不同款'
    facts['feature_comparisons']['body_type']['inventory']['temu']['groups'][0]['count']=7
    facts['identity']['design_comparison']['relation']='SIMILAR'
    assert score(cat,facts)['state']=='不同款'


def test_surface_variant_still_requires_its_named_preserved_features():
    cat,facts=configured()
    variant=next(f for f in cat['stage3']['identity_policy']['features'] if f['role']=='variant')
    variant['effects']=['surface_texture']
    variant['preserves']=[next(f['id'] for f in cat['stage3']['identity_policy']['features'] if f['required_visual'])]
    assert score(cat,facts)['state']=='同款（存在规格变体）'
    facts['feature_comparisons'][variant['preserves'][0]]['relation']='SIMILAR'
    assert score(cat,facts)['state']=='不同款'
