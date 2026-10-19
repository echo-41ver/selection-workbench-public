"""Grouped wire sharing must preserve scalar cache keys and factual decisions."""
from category_fixtures import category_path
from copy import deepcopy
import hashlib
import json
from pathlib import Path

import pytest

from selection_automation.vision import decode, parse_visual_response, protocol
from test_set_inventory_identity import configured, side
from test_identity_policy import visual_facts

ROOT = Path(__file__).resolve().parents[1]
SCALAR_HASHES = {
    'bath_towel_set.json': 'c0c0f7ff9d1baa83fe6fa3e5c51a954cbc92c00586920e41c0bd0803ed8572c1',
    'power_drill.json': 'bff3148bf9ebffe987e34e20a436f0d8669edeab0e557fa30a28e058f0d79d3e',
    'shower_curtain_local_union_20260922.json': 'f4b83d4555904ec986790dfbcca50e98762f32a435be8c57e5a47a21a68de549',
}


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, separators=(',', ':')).encode()).hexdigest()


@pytest.mark.parametrize('filename', SCALAR_HASHES)
def test_scalar_prompt_and_schema_are_byte_identical_to_prechange(filename):
    cat = json.loads(category_path(filename).read_text(encoding='utf-8'))
    assert digest(protocol(cat, 1)) == SCALAR_HASHES[filename]


def test_inventory_scalar_prompt_and_schema_are_byte_identical_to_prechange():
    assert digest(protocol(configured(), 1)) == 'a2a46d8781889eb4d099a8fced5ad0fd9cb81d41d64d5e1b78d20c308995094a'


def responses(count=2):
    cat = configured()
    base = {'i': 'C00', 'c': .95, 'v': visual_facts(cat)['identity'],
            'm': {str(i): {'r': 'S', 'a': '基准弧形柄结构', 't': '候选弧形柄结构', 'h': 'NONE'}
                  for i in range(len(cat['stage3']['modules']))},
            'f': {'body_type': {'r': 'S', 'a': '基准前排六把牛排刀', 't': '候选前排六把牛排刀',
                               'inventory': {'amazon': side(), 'temu': side()}}}}
    legacy = {'candidates': [dict(deepcopy(base), i=f'C{i:02}') for i in range(count)]}
    grouped = {'amazon': {'body_type': {'a': base['f']['body_type']['a'], 'inventory': side()}},
               'candidates': deepcopy(legacy['candidates'])}
    for item in grouped['candidates']:
        del item['f']['body_type']['a']
        del item['f']['body_type']['inventory']['amazon']
    return cat, legacy, grouped


def test_group_schema_shares_only_fixed_amazon_features():
    cat = configured()
    prompt, schema = protocol(cat, 256)
    shared = schema['properties']['amazon']['properties']['body_type']
    candidate = schema['properties']['candidates']['items']['properties']
    feature = candidate['f']['properties']['body_type']
    assert shared['required'] == ['a', 'inventory']
    assert shared['properties']['inventory']['required'] == ['complete', 'groups']
    assert feature['required'] == ['r', 't', 'inventory']
    assert feature['properties']['inventory']['required'] == ['temu']
    assert candidate['m']['properties']['0']['required'] == ['r', 'a', 't', 'h']
    assert candidate['v']['required'] == ['verdict', 'design_comparison', 'matches', 'differences', 'uncertainties', 'reason']
    assert schema['properties']['candidates']['minItems'] == 256
    assert '逐柄数' in prompt and 'reason' in prompt and 'uncertainties=[]' in prompt


@pytest.mark.parametrize('temu_count,complete', [(6, True), (8, True), (6, False)])
def test_group_decode_is_lossless_and_preserves_inventory_decisions(temu_count, complete):
    cat, legacy, grouped = responses()
    for value in (legacy, grouped):
        value['candidates'][1]['f']['body_type']['inventory']['temu'] = side(temu_count, complete)
        value['candidates'][1]['m']['0']['a'] = '针对C01可定位的基准对照证据'
        value['candidates'][1]['v']['reason'] = '独立候选结论依据'
    original = deepcopy(grouped)
    out = decode(cat, grouped, ['C00', 'C01'])
    assert out == decode(cat, legacy, ['C00', 'C01'])
    assert grouped == original
    assert out['C01']['identity']['reason'] == '独立候选结论依据'
    out['C00']['feature_comparisons']['body_type']['inventory']['amazon']['groups'][0]['count'] = 99
    assert out['C01']['feature_comparisons']['body_type']['inventory']['amazon']['groups'][0]['count'] == 6


@pytest.mark.parametrize('missing', ['amazon', 'a', 'inventory', 'complete', 'groups', 'reason', 'uncertainties', 'h', 'feature', 'temu'])
def test_missing_actual_fields_are_rejected_without_defaults(missing):
    cat, _, grouped = responses()
    if missing == 'amazon':
        del grouped['amazon']
    elif missing in {'a', 'inventory'}:
        del grouped['amazon']['body_type'][missing]
    elif missing in {'complete', 'groups'}:
        del grouped['amazon']['body_type']['inventory'][missing]
    elif missing in {'reason', 'uncertainties'}:
        del grouped['candidates'][0]['v'][missing]
    elif missing == 'h':
        del grouped['candidates'][0]['m']['0']['h']
    elif missing == 'feature':
        del grouped['candidates'][0]['f']['body_type']
    else:
        del grouped['candidates'][0]['f']['body_type']['inventory']['temu']
    with pytest.raises(ValueError):
        decode(cat, grouped, ['C00', 'C01'])


@pytest.mark.parametrize('mutation', ['extra_base', 'repeated_a', 'repeated_inventory', 'bad_count', 'duplicate_id'])
def test_conflicting_or_invalid_group_facts_are_rejected(mutation):
    cat, _, grouped = responses()
    if mutation == 'extra_base':
        grouped['amazon']['unbound_feature'] = {'a': '额外事实'}
    elif mutation == 'repeated_a':
        grouped['candidates'][0]['f']['body_type']['a'] = '冲突重复事实'
    elif mutation == 'repeated_inventory':
        grouped['candidates'][0]['f']['body_type']['inventory']['amazon'] = side(8)
    elif mutation == 'bad_count':
        grouped['amazon']['body_type']['inventory']['groups'][0]['count'] = True
    else:
        grouped['candidates'][1]['i'] = 'C00'
    with pytest.raises(ValueError):
        decode(cat, grouped, ['C00', 'C01'])


def test_256_candidate_wire_removes_255_duplicate_inventory_descriptions():
    _, legacy, grouped = responses(256)
    old = json.dumps(legacy, ensure_ascii=False)
    new = json.dumps(grouped, ensure_ascii=False)
    assert new.count('基准前排六把牛排刀') == 1
    assert old.count('基准前排六把牛排刀') == 256
    assert new.count('"complete"') == 257
    assert old.count('"complete"') == 512
    assert len(new) < len(old)


def test_shared_group_json_can_be_parsed_in_existing_modes():
    cat, _, grouped = responses()
    text = json.dumps(grouped, ensure_ascii=False)
    assert parse_visual_response(text) == grouped
    assert parse_visual_response('核验结果如下：' + text, allow_preamble=True) == grouped
    with pytest.raises(ValueError):
        decode(cat, grouped, ['C00'])


def test_group_without_inventory_still_shares_amazon_feature_and_keeps_unknown():
    cat, legacy, grouped = responses()
    del cat['stage3']['set_inventory_features']
    del grouped['amazon']['body_type']['inventory']
    for value in (legacy, grouped):
        for item in value['candidates']:
            del item['f']['body_type']['inventory']
            item['f']['body_type']['r'] = 'U'
            item['f']['body_type']['t'] = ''
    assert decode(cat, grouped, ['C00', 'C01']) == decode(cat, legacy, ['C00', 'C01'])
    _, schema = protocol(cat, 2)
    assert schema['properties']['amazon']['properties']['body_type']['required'] == ['a']


def test_category_without_feature_comparisons_keeps_legacy_group_shape():
    cat = json.loads((ROOT / 'config/categories/power_drill.json').read_text(encoding='utf-8'))
    cat['stage3']['require_feature_comparisons'] = False
    _, schema = protocol(cat, 2)
    assert schema['required'] == ['candidates']
    assert 'f' not in schema['properties']['candidates']['items']['properties']


def test_short_group_schema_limits_prose_without_limiting_inventory_or_codes():
    cat = configured()
    prompt, schema = protocol(cat, 12)
    props = schema['properties']['candidates']['items']['properties']
    module = props['m']['properties']['0']['properties']
    assert module['a']['maxLength'] == module['t']['maxLength'] == 48
    assert 'maxLength' not in module['h'] and 'maxLength' not in module['r']
    identity = props['v']['properties']
    assert identity['reason']['maxLength'] == 120
    assert identity['design_comparison']['properties']['amazon']['maxLength'] == 80
    assert identity['matches']['items']['properties']['temu']['maxLength'] == 64
    assert identity['differences']['items']['properties']['amazon']['maxLength'] == 64
    assert identity['uncertainties']['items']['maxLength'] == 64
    assert 'maxLength' not in identity['differences']['items']['properties']['variant_field']
    base = schema['properties']['amazon']['properties']['body_type']['properties']
    assert base['a']['maxLength'] == 96
    assert props['f']['properties']['body_type']['properties']['t']['maxLength'] == 96
    assert 'maxLength' not in base['inventory']['properties']['groups']['items']['properties']['region']
    assert '上限约束本次组织短句' in prompt and '不逐模块重复m/f' in prompt
    assert 'm.a/m.t分别写可定位的双方短事实' in prompt


def test_short_generation_limits_do_not_truncate_or_reject_legacy_long_facts():
    cat, legacy, grouped = responses()
    long_evidence = '可见前排六个弧形柄且刀型构成逐件核实；' * 30
    for value in (legacy, grouped):
        value['candidates'][0]['m']['0']['a'] = long_evidence
        value['candidates'][0]['v']['reason'] = long_evidence
    decoded = decode(cat, grouped, ['C00', 'C01'])
    assert decoded == decode(cat, legacy, ['C00', 'C01'])
    assert decoded['C00']['modules']['body']['amazon_evidence'] == long_evidence
    assert decoded['C00']['identity']['reason'] == long_evidence
