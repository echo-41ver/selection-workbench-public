"""Local two-way selection decisions over unchanged, auditable image facts."""
from copy import deepcopy

MODE = 'visible_core_binary_v1'


def binary_mode(category):
    if (category.get('match_criteria') or {}).get('visual_mode')=='auto':return False
    return category.get('visual_decision') == MODE


def scoring_category(category):
    if not binary_mode(category):
        return category
    result = deepcopy(category)
    features = (result['stage3'].get('identity_policy') or {}).get('features', [])
    required = {f['id'] for f in features if f['required_visual']}
    for feature in features:
        # Surface colour/branding does not require unseen controls or internals.
        # Every visible identity conflict still passes the normal identity gate.
        if feature['role'] == 'variant' and set(feature['effects']) <= {'color', 'marking'}:
            core = [fid for fid in feature['preserves'] if fid in required]
            if core:
                feature['preserves'] = core
    return result


def final_decision(category, scored):
    if not binary_mode(category):
        return scored
    result = dict(scored)
    state = scored['state']
    result['evidence_state'] = state
    if state in {'确认同款', '同款（存在规格变体）', '不同款'}:
        return result
    from .identity import bilateral, concrete
    identity = scored.get('identity') or {}
    design = identity.get('design_comparison') or {}
    features = (category['stage3'].get('identity_policy') or {}).get('features', [])
    core = [f['id'] for f in features if f['required_visual']]
    comparisons = scored.get('feature_comparisons') or {}
    core_visible = bool(core) and all(
        comparisons.get(fid, {}).get('relation') == 'SAME' and bilateral(comparisons[fid])
        for fid in core)
    concrete_matches = any(bilateral(m) and concrete(m.get('feature', '')) for m in identity.get('matches', []))
    clear_design = design.get('relation') == 'SAME' and bilateral(design)
    def variant_supported(feature):
        harmless_optional = (set(feature['effects']) <= {'color', 'marking'} or
                             feature.get('region') == 'packaging')
        return all(
            (comparisons.get(fid, {}).get('relation') == 'SAME' and bilateral(comparisons[fid])) or
            (harmless_optional and fid not in core and
             comparisons.get(fid, {}).get('relation', 'UNKNOWN') == 'UNKNOWN')
            for fid in feature['preserves'])
    allowed = {f['id'] for f in features if f['role'] == 'variant' and variant_supported(f)}
    differences = identity.get('differences', [])
    changes_allowed = all(d.get('kind') == 'VARIANT' and d.get('variant_field') in allowed and bilateral(d)
                          for d in differences)
    module_conflict = any(m['relation'] == 'DIFFERENT' for m in scored['modules'].values())
    from .set_inventory import requirements, compare_inventory, matching_visible_inventory
    inventory_supported = all(
        compare_inventory(comparisons.get(fid, {}).get('inventory'), requirement) == 'SAME' or
        matching_visible_inventory(comparisons.get(fid, {}).get('inventory'), requirement)
        for fid, requirement in requirements(category).items())
    if (core_visible and concrete_matches and clear_design and changes_allowed and
            inventory_supported and concrete(identity.get('reason', '')) and
            not module_conflict and not scored.get('contradictory_modules') and
            identity.get('verdict') in {'SAME', 'UNKNOWN'}):
        result['state'] = '同款（存在规格变体）' if differences else '确认同款'
        result['decision_basis'] = 'VISIBLE_CORE_CORRESPONDENCE'
        result['reason'] = '主图主体款式及必需核心结构有双边对应；无关未展示部位不阻断；' + identity['reason']
        return result
    if (identity.get('verdict') == 'SIMILAR' and design.get('relation') == 'SIMILAR'
            and bilateral(design) and concrete(identity.get('reason', ''))
            and not scored.get('contradictory_modules')
            and scored.get('effective_confidence', 0) >= .55):
        result['state'] = '相似款'
        result['decision_basis'] = 'VISIBLE_SIMILAR_DESIGN'
        result['reason'] = identity['reason']
        return result
    result['state'] = '待补充证据'
    result['decision_basis'] = 'INSUFFICIENT_VISIBLE_EVIDENCE'
    result['reason'] = '当前图片尚不能确认商品关系；' + scored['reason']
    return result
