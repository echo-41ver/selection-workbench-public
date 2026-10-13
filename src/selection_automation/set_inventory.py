"""Bilateral visible set counts; no inferred inventory behind occlusion."""
from collections import Counter


def requirements(category):
    configured = category['stage3'].get('set_inventory_features', {})
    if not isinstance(configured, dict):
        raise ValueError('set inventory requirements must be an object')
    if not configured:
        return configured
    features = {f['id']: f for f in category['stage3'].get('identity_policy', {}).get('features', [])}
    for fid, types in configured.items():
        feature = features.get(fid, {})
        if (not category['stage3'].get('require_feature_comparisons') or
            feature.get('role') != 'identity' or not feature.get('required_visual') or
            feature.get('evidence') not in {'image', 'both'} or
            not isinstance(types, list) or not types or len(set(types)) != len(types) or
            any(not isinstance(t, str) or not t.strip() for t in types)):
            raise ValueError('set inventory must bind a required visible identity feature and item types')
    return configured


def validate_inventory(value, types):
    if not isinstance(value, dict) or set(value) != {'amazon', 'temu'}:
        raise ValueError('missing bilateral set inventory')
    for side in value.values():
        if (not isinstance(side, dict) or set(side) != {'complete', 'groups'} or
            type(side['complete']) is not bool or not isinstance(side['groups'], list)):
            raise ValueError('invalid visible set inventory')
        for group in side['groups']:
            if (not isinstance(group, dict) or set(group) != {'region', 'item_type', 'count'} or
                not isinstance(group['region'], str) or not group['region'].strip() or
                group['item_type'] not in types or type(group['count']) is not int or
                not 1 <= group['count'] <= 200):
                raise ValueError('invalid set inventory group')


def compare_inventory(value, types):
    """Complete bilateral counts can disprove SAME; partial counts stay unknown."""
    if value is None:
        return 'UNKNOWN'
    validate_inventory(value, types)
    if any(not side['complete'] or not side['groups'] for side in value.values()):
        return 'UNKNOWN'
    if any(group['item_type'] in {'other','unknown'} for side in value.values() for group in side['groups']):
        return 'UNKNOWN'
    totals = []
    for side in ('amazon', 'temu'):
        counts = Counter()
        for group in value[side]['groups']:
            counts[group['item_type']] += group['count']
        totals.append(counts)
    return 'SAME' if totals[0] == totals[1] else 'DIFFERENT'


def matching_visible_inventory(value, types):
    """Supporting visible counts only; the caller must prove core correspondence.

    Unnamed types and occlusion do not disprove an independently identified
    design. Keep their exact recorded groups/counts without inferring hidden
    items or converting the strict inventory verdict to SAME.
    """
    if value is None:
        return False
    validate_inventory(value, types)
    totals = []
    for side in ('amazon', 'temu'):
        counts = Counter()
        for group in value[side]['groups']:
            counts[group['item_type']] += group['count']
        totals.append(counts)
    return bool(totals[0]) and totals[0] == totals[1]
