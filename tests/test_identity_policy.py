from copy import deepcopy
import json
from pathlib import Path

import pytest

from selection_automation.identity_policy import compile_policy, validate_policy_consistency, VERSION
from selection_automation.rules import text_gate, validate, score
from selection_automation.vision import protocol, vision_revision

ROOT = Path(__file__).resolve().parents[1]


def category(name='台灯'):
    c = json.loads((ROOT / 'config/categories/power_drill.json').read_text('utf-8'))
    c['category_name_zh'] = name
    c['stage1'] = {'text_gate': 'declarative', 'rule_version': 'test', 'attributes': [
        {'name': 'body_type', 'hard_conflict': True, 'values': {'fixed': ['fixed body'], 'jointed': ['jointed body']}},
        {'name': 'finish', 'hard_conflict': True, 'values': {'red': ['red'], 'blue': ['blue']}},
        {'name': 'capacity', 'hard_conflict': False, 'values': {'small': ['capacity small'], 'large': ['capacity large']}}]}
    c['stage3']['modules'] = {'body': 80, 'appearance': 20}
    c['stage3']['identity_policy'] = {'version': VERSION, 'product_scope': name + '本体，不含展示背景', 'features': [
        {'id': 'body_type', 'label': '主体构造', 'role': 'identity', 'evidence': 'both', 'scope': '本体轮廓与连接部位',
         'rule': '对应部件的造型和连接布局不同属于不同款', 'modules': ['body'], 'required_visual': True,
         'region': 'body', 'effects': ['shape','connection'], 'preserves': []},
        {'id': 'finish', 'label': '表面颜色', 'role': 'variant', 'evidence': 'both', 'scope': '主体表面',
         'rule': '形状与结构一致时允许表面颜色改变', 'modules': ['appearance'], 'required_visual': False,
         'region': 'body', 'effects': ['color'], 'preserves': ['body_type']},
        {'id': 'capacity', 'label': '标称容量', 'role': 'context', 'evidence': 'text', 'scope': '商品明确标注',
         'rule': '只记录文字值，不凭主图估算容量', 'modules': [], 'required_visual': False,
         'region': 'specification', 'effects': ['nominal_size'], 'preserves': []}]}
    return c


@pytest.mark.parametrize('name', ['台灯', '收纳箱', '宠物食盆', '遥控器', '电钻'])
def test_new_categories_share_compiler_not_towel_templates(name):
    c = compile_policy(category(name))
    validate(c)
    assert c['stage3']['identity_required_modules'] == ['body']
    assert c['stage3']['variant_fields'] == ['finish']
    assert c['stage3']['module_variant_fields'] == {'appearance': ['finish']}
    assert not text_gate(c, 'fixed body red', 'fixed body blue')['excluded']
    assert text_gate(c, 'fixed body red', 'jointed body red')['excluded']
    prompt, _ = protocol(c, 1)
    assert name in prompt
    assert '浴巾' not in prompt and '浴帘' not in prompt
    assert '仅文字证据' in prompt
    assert c['stage3']['confirmation_minimums'] == {'body': 80}


@pytest.mark.parametrize('field,value', [
    ('variant_fields', []), ('identity_required_modules', ['appearance']),
    ('visual_instructions', '允许任何款式'), ('module_descriptions', {'body': '颜色不同就排除', 'appearance': '外观'}),
])
def test_persisted_rule_tampering_cannot_diverge_from_policy(field, value):
    c = compile_policy(category())
    c['stage3'][field] = value
    with pytest.raises(ValueError, match='不一致'):
        validate_policy_consistency(c)


def test_allowed_variant_cannot_be_reenabled_as_text_exclusion():
    c = compile_policy(category())
    c['stage1']['attributes'][1]['hard_conflict'] = True
    with pytest.raises(ValueError, match='文字排除'):
        validate(c)


@pytest.mark.parametrize('change', ['hidden_visual', 'unverifiable_identity', 'missing_mapping', 'no_visible_identity'])
def test_bad_authoring_cannot_be_compiled(change):
    c = category()
    features = c['stage3']['identity_policy']['features']
    if change == 'hidden_visual': features[2]['modules'] = ['body']
    if change == 'unverifiable_identity': features[0].update(evidence='unverifiable', modules=[])
    if change == 'missing_mapping': c['stage1']['attributes'][0]['name'] = '另一个同义名称'
    if change == 'no_visible_identity': features[0]['role'] = 'context'
    with pytest.raises(ValueError):
        compile_policy(c)


def test_policy_change_invalidates_visual_cache(tmp_path):
    p = tmp_path / 'image'; p.write_bytes(b'image')
    pair = {'asin': 'a', 'goods_id': 't', 'pair_revision': 'p', 'amazon_image_path': str(p), 'temu_image_path': str(p)}
    c = compile_policy(category())
    before = vision_revision(c, pair)
    c['stage3']['identity_policy']['features'][1]['rule'] = '仅外包装颜色可变，本体颜色不可变'
    assert vision_revision(compile_policy(c), pair) != before


def test_legacy_category_is_not_silently_rewritten():
    c = json.loads((ROOT / 'config/categories/power_drill.json').read_text('utf-8'))
    original = deepcopy(c)
    validate(c)
    assert c == original


def test_variant_and_module_name_collision_is_compiled_without_losing_the_variant():
    c = category()
    c['stage3']['modules'] = {'body': 80, 'finish': 20}
    c['stage3']['identity_policy']['features'][1]['modules'] = ['finish']
    c = compile_policy(c)
    assert c['stage3']['variant_fields'] == ['finish']
    assert c['stage3']['module_variant_fields'] == {'visual_finish': ['finish']}
    validate(c)
    assert compile_policy(c) == c


def test_optional_identity_feature_does_not_become_required():
    c = category()
    c['stage3']['modules'] = {'body': 70, 'rear': 10, 'appearance': 20}
    c['stage3']['identity_policy']['features'].append({'id': 'rear_socket', 'label': '背部插口', 'role': 'identity',
        'evidence': 'image', 'scope': '背部接口', 'rule': '两侧均展示且接口布局不同时排除，未展示不要求补齐',
        'modules': ['rear'], 'required_visual': False,
        'region': 'component', 'effects': ['layout'], 'preserves': []})
    c = compile_policy(c)
    assert c['stage3']['identity_required_modules'] == ['body']
    assert 'IDENTITY_REAR_SOCKET' in c['stage3']['identity_hard_conflicts']


def test_explicit_text_dictionary_adds_text_source_without_inventing_image_capability():
    c = category()
    c['stage3']['identity_policy']['features'][1]['evidence'] = 'image'
    c = compile_policy(c)
    assert c['stage3']['identity_policy']['features'][1]['evidence'] == 'both'
    assert not text_gate(c, 'red', 'blue')['excluded']
    assert compile_policy(c) == c


def test_unverifiable_marketing_text_remains_context_only():
    c = category()
    c['stage3']['identity_policy']['features'][2]['evidence'] = 'unverifiable'
    c['stage1']['attributes'][2]['hard_conflict'] = True
    c = compile_policy(c)
    assert not text_gate(c, 'capacity small', 'capacity large')['excluded']
    assert c['stage3']['identity_policy']['features'][2]['evidence'] == 'unverifiable'
    assert c['stage3']['identity_required_modules'] == ['body']


@pytest.mark.parametrize('name,feature,label', [
    ('收纳箱', 'handle_design', '把手设计'),
    ('收纳箱', 'interior_visible', '内部隔断'),
    ('遥控器', 'button_layout', '按键布局'),
    ('投影仪', 'port_layout', '接口布局'),
])
def test_structural_variation_is_rejected_across_categories(name, feature, label):
    c = category(name)
    c['stage3']['identity_policy']['features'].append({
        'id': feature, 'label': label, 'role': 'variant', 'evidence': 'image',
        'scope': label, 'rule': '可能存在批次或型号变体，允许形状位置改变',
        'modules': ['body'], 'required_visual': False,
        'region': 'component', 'effects': ['layout'], 'preserves': ['body_type']})
    with pytest.raises(ValueError, match='结构特征不能整项'):
        compile_policy(c)


def test_missing_text_does_not_exclude_or_reclassify_structure():
    c = compile_policy(category('收纳箱'))
    assert not text_gate(c, 'fixed body', 'storage box')['excluded']
    assert c['stage3']['identity_policy']['features'][0]['role'] == 'identity'


@pytest.mark.parametrize('name', ['台灯', '收纳箱', '宠物食盆', '遥控器', '投影仪', '电钻'])
def test_effects_reject_structure_even_with_unrelated_feature_name(name):
    c = category(name)
    f = c['stage3']['identity_policy']['features'][1]
    f.update(label='普通特征', scope='可见部位', effects=['layout'])
    with pytest.raises(ValueError, match='结构特征不能整项'):
        compile_policy(c)


def test_visible_structure_cannot_be_context_to_bypass_guard():
    c = category()
    f = c['stage3']['identity_policy']['features'][1]
    f.update(role='context', effects=['connection'], preserves=[])
    with pytest.raises(ValueError, match='可见结构差异'):
        compile_policy(c)


def test_variant_requires_actual_design_preservation_not_only_same_use():
    c = category()
    c['stage3']['identity_policy']['features'][0]['effects'] = ['function']
    with pytest.raises(ValueError, match='可见款式结构'):
        compile_policy(c)


def test_outer_packaging_shape_can_vary_while_body_design_is_preserved():
    c = category()
    f = c['stage3']['identity_policy']['features'][1]
    f.update(region='packaging', effects=['shape'], scope='本体外部纸盒，主体不变', rule='主体设计一致时允许纸盒形状变化')
    assert 'finish' in compile_policy(c)['stage3']['variant_fields']


def towel_policy():
    c = category('浴巾套装')
    fs = c['stage3']['identity_policy']['features']
    for ident, region, effects, role, condition, rule in [
        ('weave', 'body', ['body_weave'], 'identity', [], '仅比较装饰区域之外的毛圈或华夫格主体织法'),
        ('band', 'decoration', ['layout'], 'identity', [], '比较对应真实边缘的条带区域位置、组数、间隔；不比较带内图案'),
        ('motif', 'decoration', ['surface_pattern'], 'variant', ['weave','band'], '主体织法和条带区域布局一致时允许带内花卉、线纹不同'),
        ('band_finish', 'decoration', ['surface_texture'], 'variant', ['weave','band'], '布局对应时允许带内平织、阴刻装饰纹理表现不同，不扩展至主体织法'),
    ]:
        fs.append({'id': ident, 'label': ident, 'region': region, 'effects': effects,
                   'role': role, 'preserves': condition, 'evidence': 'image',
                   'scope': rule, 'rule': rule, 'modules': ['body'], 'required_visual': role == 'identity'})
    return c


def test_towel_reviewed_pattern_and_internal_finish_are_scoped_variants():
    c = compile_policy(towel_policy())
    validate(c)
    assert {'motif','band_finish'} <= set(c['stage3']['variant_fields'])
    prompt, _ = protocol(c, 1)
    assert '不得将装饰带内织纹混入主体织法' in prompt
    assert 'weave,band' in prompt
    assert '已确认品类依据' in prompt


@pytest.mark.parametrize('mutation', ['pattern_identity','layout_variant','local_weave','missing_condition'])
def test_towel_rule_cannot_reintroduce_confirmed_semantic_errors(mutation):
    c = towel_policy(); fs = {f['id']: f for f in c['stage3']['identity_policy']['features']}
    if mutation == 'pattern_identity': fs['motif'].update(role='identity', preserves=[])
    if mutation == 'layout_variant': fs['band'].update(role='variant', required_visual=False, preserves=['weave'])
    if mutation == 'local_weave': fs['band_finish']['effects'] = ['body_weave']
    if mutation == 'missing_condition': fs['motif']['preserves'] = ['body_type']
    with pytest.raises(ValueError): compile_policy(c)


def curtain_policy():
    c = category('浴帘')
    fs = c['stage3']['identity_policy']['features']
    fs.append({'id':'print','label':'印花元素及版式','region':'body','effects':['surface_pattern'],
               'role':'identity','preserves':[],'evidence':'image','scope':'完整帘面实际元素排列',
               'rule':'印花不同属于不同款，主题相同不构成印花一致','modules':['body'],'required_visual':True})
    fs.append({'id':'opacity','label':'透明度','region':'body','effects':['transparency'],
               'role':'identity','preserves':[],'evidence':'both','scope':'帘体实际透视程度',
               'rule':'明确透明、半透明和不透明冲突为不同款，不凭光线推断','modules':['body'],'required_visual':False})
    c['stage1']['attributes'].append({'name':'opacity','hard_conflict':True,
        'values':{'clear':['clear curtain'],'opaque':['opaque curtain']}})
    return c


def test_curtain_transparency_stays_identity_but_unstated_value_does_not_exclude():
    c = compile_policy(curtain_policy()); validate(c)
    assert text_gate(c, 'clear curtain', 'opaque curtain')['excluded']
    assert not text_gate(c, 'clear curtain', 'curtain')['excluded']
    assert not text_gate(c, 'fixed body red', 'fixed body blue')['excluded']
    assert '不能推断透明度' in protocol(c, 1)[0]


def test_curtain_print_cannot_inherit_towel_pattern_variation():
    c = curtain_policy()
    c['stage3']['identity_policy']['features'][-2].update(role='variant',required_visual=False,preserves=['body_type'])
    with pytest.raises(ValueError, match='已确认品类边界'):
        compile_policy(c)


def test_old_unreviewed_policy_cannot_be_relabelled_as_v2():
    c = category()
    c['stage3']['identity_policy']['version'] = 'category-identity-policy-v1'
    with pytest.raises(ValueError, match='不能按名称猜测'):
        compile_policy(c)


def visual_facts(c):
    from identity_fixtures import identity
    return {'identity': identity(), 'overall_confidence': .95,
        'feature_comparisons':{f['id']:{'relation':'SAME','amazon':'上部圆弧连接、右下双凹槽',
                                      'temu':'上部圆弧连接、右下双凹槽'}
            for f in c['stage3']['identity_policy']['features'] if f['role']=='identity' and f['evidence'] in {'image','both'}},
        'modules': {
        k: {'relation': 'SAME', 'amazon_evidence': '上部圆弧连接、右下双凹槽',
            'temu_evidence': '上部圆弧连接、右下双凹槽', 'hard_conflict_code': 'NONE'}
        for k in c['stage3']['modules']}}


@pytest.mark.parametrize('name', ['台灯', '收纳箱', '宠物食盆', '遥控器', '电钻', '投影仪'])
def test_permitted_color_with_actual_same_structure_confirms(name):
    c = compile_policy(category(name)); facts = visual_facts(c)
    facts['identity']['differences'] = [{'field':'表面颜色', 'amazon':'红色外壳', 'temu':'蓝色外壳',
                                       'kind':'VARIANT','variant_field':'finish'}]
    facts['modules']['appearance']['relation'] = 'VARIANT'
    assert score(c, facts)['state'] == '同款（存在规格变体）'
    facts['identity']['differences'].append({'field':'连接结构','amazon':'固定连接',
        'temu':'两段铰接','kind':'IDENTITY','variant_field':'NONE'})
    assert score(c, facts)['state'] == '不同款'


def test_optional_image_unknown_does_not_become_inferred_match():
    c = category()
    c['stage3']['modules'] = {'body':70,'rear':10,'appearance':20}
    c['stage3']['identity_policy']['features'].append({'id':'rear_detail','label':'后部连接',
        'role':'identity','evidence':'image','region':'component','effects':['connection'],
        'preserves':[],'scope':'后部连接形状','rule':'均展示时才判断有无，不展示为未知',
        'modules':['rear'],'required_visual':False})
    c = compile_policy(c); facts = visual_facts(c)
    facts['modules']['rear'].update(relation='UNKNOWN',amazon_evidence='',temu_evidence='')
    facts['feature_comparisons']['rear_detail'].update(relation='UNKNOWN',amazon='',temu='')
    assert score(c, facts)['state'] == '确认同款'
    assert score(c, facts)['modules']['rear']['relation'] == 'UNKNOWN'
    c['stage3']['identity_policy']['features'][1]['preserves'].append('rear_detail')
    c = compile_policy(c)
    facts['identity']['differences'] = [{'field':'表面颜色','amazon':'红色','temu':'蓝色',
        'kind':'VARIANT','variant_field':'finish'}]
    assert score(c, facts)['state'] == '待复核'


def test_region_conflict_overrides_perfect_auxiliary_modules():
    c = compile_policy(towel_policy()); facts = visual_facts(c)
    facts['feature_comparisons']['band'] = {'relation':'DIFFERENT',
        'amazon':'宽装饰区后隔主体毛圈再有三个窄区', 'temu':'两个窄平织区之间为主体毛圈'}
    result = score(c,facts)
    assert result['total_score'] == 100
    assert result['state'] == '不同款'


def test_missing_required_region_fact_cannot_be_inferred_from_same_modules():
    c = compile_policy(towel_policy()); facts = visual_facts(c)
    facts['feature_comparisons'].pop('band')
    result = score(c,facts)
    assert result['state'] == '待复核'
    assert '缺少有效的身份部位双边对照' in result['reason']
    assert '不可见' not in result['reason']


def test_auxiliary_unknown_does_not_replace_clear_region_facts():
    c = compile_policy(category()); facts = visual_facts(c)
    facts['modules']['body'].update(relation='UNKNOWN',amazon_evidence='',temu_evidence='')
    assert score(c,facts)['state'] == '确认同款'


def test_clear_similar_region_is_review_not_invisible_evidence():
    c = compile_policy(towel_policy()); facts = visual_facts(c)
    facts['feature_comparisons']['band']['relation'] = 'SIMILAR'
    result = score(c,facts)
    assert result['state'] == '待复核'
    assert '不可见' not in result['reason']
