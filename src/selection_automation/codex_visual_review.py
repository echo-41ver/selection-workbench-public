"""Separate, revision-bound Codex image reviews; never an API/billing result."""
from __future__ import annotations
from copy import deepcopy
import json
from pathlib import Path
import re
import time

from .identity import concrete
from .jsonl_records import read_jsonl
from .rules import score
from .vision import REL, decode

SOURCE = 'CODEX_VISUAL_REVIEW'
CONFIRMED_STATES = {'确认同款', '同款（存在规格变体）'}


def identity_feature_ids(category, raw):
    """Resolve configured labels and bounded aliases, never guess a new feature."""
    features = (category['stage3'].get('identity_policy') or {}).get('features', [])
    configured = {feature['id'] for feature in features}
    names = {fid:fid for fid in configured}
    for feature in features:
        label = feature.get('label')
        if isinstance(label,str) and label:
            names[label] = feature['id'] if label not in names or names[label]==feature['id'] else None
    knife_aliases = {
        '套装构成':'knife_set_composition','构成':'knife_set_composition','刀型构成':'knife_set_composition',
        '刀型':'blade_shape_profile','刀刃':'blade_shape_profile',
        '刀柄':'handle_construction','刀柄结构':'handle_construction',
        '刀颈':'bolster_ferrule_geometry','护指':'bolster_ferrule_geometry',
        '刀座':'storage_block_structure','刀架':'storage_block_structure'}
    for alias,fid in knife_aliases.items():
        if fid in configured:
            names.setdefault(alias,fid)
    values = [raw] if isinstance(raw,str) else raw if isinstance(raw,list) else []
    resolved = []
    for value in values:
        if not isinstance(value,str):
            return []
        parts = [value.strip()] if value.strip() in names else re.split('[,，/／]',value)
        for part in parts:
            fid = names.get(part.strip())
            if fid is None:
                return []
            if fid not in resolved:
                resolved.append(fid)
    return resolved


def review_paths(base):
    return sorted((Path(base) / 'stage123/codex_reviews').glob('*.jsonl'))


def usable_review(category, row):
    if category.get('visual_decision') != 'visible_core_binary_v1':
        return True
    try:
        score(category, checked_facts(category, row['visual_facts'], row['goods_id']))
    except (KeyError, TypeError, ValueError):
        return False
    return True


def load_reviews(base, expected=None, *, by_revision=False, category=None):
    result = {}
    ranks = {}
    for path in review_paths(base):
        modified = path.stat().st_mtime
        for row in read_jsonl(path):
            pair = (row.get('asin'), row.get('goods_id'))
            if row.get('review_source') != SOURCE or row.get('review_status') not in {'SAME','SIMILAR', 'DIFFERENT', 'UNKNOWN'}:
                raise ValueError('invalid normalized Codex review')
            if expected is None or row.get('vision_revision_key') in expected.get(pair, set()):
                identity = (*pair,row.get('vision_revision_key')) if by_revision else pair
                classified = row.get('classified_at')
                rank = (classified if isinstance(classified, (int, float)) else modified, path.name)
                if identity not in ranks or rank >= ranks[identity]:
                    result[identity] = row
                    ranks[identity] = rank
    return {key: row for key, row in result.items() if usable_review(category, row)} if category else result


def checked_facts(category, facts, goods_id):
    """Require all current wire fields and use the existing strict decoder."""
    from .match_criteria import enabled
    if enabled(category):
        if not isinstance(facts,dict):raise ValueError('incomplete selected visual facts')
        identity=facts['identity'];design=identity['design_comparison']
        reverse={'SAME':'S','SIMILAR':'L','DIFFERENT':'D','UNKNOWN':'U'}
        candidate={'i':goods_id,'c':facts['overall_confidence'],'r':reverse[identity['verdict']],
            'a':design['amazon'],'t':design['temu'],'why':identity['reason'],
            'q':{fid:{'r':reverse[row['relation']],'a':row['amazon'],'t':row['temu']}
                 for fid,row in facts['condition_comparisons'].items()}}
        if design['relation']!=identity['verdict']:raise ValueError('contradictory selected whole-product design')
        return decode(category,{'candidates':[candidate]},[goods_id])[goods_id]
    modules = category['stage3']['modules']
    if category['stage3'].get('codex_require_module_facts'):
        required = {'overall_confidence', 'identity', 'modules'} | ({'feature_comparisons'} if category['stage3'].get('require_feature_comparisons') else set())
        # decode() adds this derived field to normalized facts. Accepting it makes
        # import idempotent while it is ignored and recomputed from module relations.
        if (not isinstance(facts, dict) or not required.issubset(facts) or
                set(facts) - required - {'variant_differences'}):
            raise ValueError('incomplete visual facts object')
        if not isinstance(facts.get('modules'), dict) or set(facts['modules']) != set(modules):
            raise ValueError('incomplete visual module facts')
        for module in modules:
            row = facts['modules'][module]
            if not isinstance(row, dict) or set(row) != {'relation', 'amazon_evidence', 'temu_evidence', 'hard_conflict_code'}:
                raise ValueError(f'incomplete visual facts for {module}')
            if row.get('relation') not in REL.values() or not all(isinstance(row.get(key), str) for key in ('amazon_evidence', 'temu_evidence', 'hard_conflict_code')):
                raise ValueError(f'invalid visual facts for {module}')
            if row['relation'] != REL['U'] and any(
                    '\ufffd' in row[key] or re.search(r'\?{3,}', row[key])
                    for key in ('amazon_evidence', 'temu_evidence')):
                raise ValueError(f'corrupted visual evidence for {module}')
        evidence_pairs = [
            (row['amazon_evidence'].strip(), row['temu_evidence'].strip())
            for row in facts['modules'].values()
            if row['relation'] != REL['U'] and row['amazon_evidence'].strip() and row['temu_evidence'].strip()
        ]
        if any(evidence_pairs.count(pair) >= 3 for pair in set(evidence_pairs)):
            raise ValueError('reused evidence across visual modules')
    reverse = {value: key for key, value in REL.items()}
    if (facts.get('overall_confidence')==0 and facts.get('identity',{}).get('verdict') in {'SAME','SIMILAR','DIFFERENT'}
            and any(module.get('relation')!='UNKNOWN' and module.get('amazon_evidence','').strip()
                    and module.get('temu_evidence','').strip() for module in facts['modules'].values())):
        raise ValueError('证据清晰度0与已填写的双边明确事实矛盾，请按原图修正；禁止照抄模板占位值')
    candidate = {'i': goods_id, 'c': facts['overall_confidence'], 'v': facts['identity'],
                 'm': {str(i): {'r': reverse[facts['modules'][key]['relation']],
                                 'a': facts['modules'][key]['amazon_evidence'],
                                 't': facts['modules'][key]['temu_evidence'],
                                 'h': facts['modules'][key]['hard_conflict_code']}
                       for i, key in enumerate(category['stage3']['modules'])}}
    if category['stage3'].get('require_feature_comparisons'):
        candidate['f'] = {}
        for fid, feature in facts['feature_comparisons'].items():
            row = {'r': reverse[feature['relation']], 'a': feature['amazon'], 't': feature['temu']}
            if 'inventory' in feature:
                row['inventory'] = deepcopy(feature['inventory'])
            candidate['f'][fid] = row
    return decode(category, {'candidates': [candidate]}, [goods_id])[goods_id]


def supersedes_api(category, review):
    """Only an explicitly authorized, complete image recheck replaces API output."""
    if review.get('supersedes_api') is not True:
        return False
    strict = deepcopy(category)
    strict['stage3']['codex_require_module_facts'] = True
    try:
        checked_facts(strict, review['visual_facts'], review['goods_id'])
    except (KeyError, TypeError, ValueError):
        return False
    return True


def normalize_review(category, row, pair, task_id):
    for key in ('asin', 'goods_id', 'vision_revision_key'):
        if row.get(key) != pair.get(key) or not row.get(key):
            raise ValueError(f'Codex review {key} does not match manifest')
    status = row.get('review_status')
    if status not in {'SAME','SIMILAR', 'DIFFERENT', 'UNKNOWN'}:
        raise ValueError('invalid Codex review status')
    if any(not isinstance(row.get(key), str) or not row[key].strip()
           for key in ('amazon_evidence', 'temu_evidence', 'reason')):
        strict = deepcopy(category)
        strict['stage3']['codex_require_module_facts'] = True
        try:
            facts = checked_facts(strict, row['visual_facts'], row['goods_id'])
        except (KeyError, TypeError, ValueError):
            facts = None
        if facts is not None:
            row = deepcopy(row)
            for side in ('amazon', 'temu'):
                key = f'{side}_evidence'
                if not isinstance(row.get(key), str) or not row[key].strip():
                    row[key] = '；'.join(dict.fromkeys(
                        module[key].strip() for module in facts['modules'].values() if module[key].strip()))
            if not isinstance(row.get('reason'), str) or not row['reason'].strip():
                row['reason'] = score(category, facts)['reason']
    for key in ('amazon_evidence', 'temu_evidence', 'reason', 'reviewer', 'model'):
        if not isinstance(row.get(key), str) or not row[key].strip():
            raise ValueError(f'missing Codex review {key}')
    value = {key: row[key] for key in ('asin', 'goods_id', 'vision_revision_key', 'review_status',
             'amazon_evidence', 'temu_evidence', 'reason', 'reviewer', 'model')}
    value.update(review_source=SOURCE, task_id=task_id,
                 amazon_image_path=pair['amazon_image_path'], temu_image_path=pair['temu_image_path'])
    if 'identity_feature' in row:
        feature_ids = identity_feature_ids(category,row['identity_feature'])
        value['identity_feature'] = feature_ids
    if category['stage3'].get('codex_require_module_facts') and 'visual_facts' not in row:
        raise ValueError('pump Codex reviews require valid complete module facts')
    if status == 'DIFFERENT' and (not all(concrete(row[key]) for key in ('amazon_evidence', 'temu_evidence', 'reason')) or
                                 row['amazon_evidence'].strip() == row['temu_evidence'].strip()):
        value['review_status'] = 'UNKNOWN'
        value['validation_note'] = '双侧具体身份差异证据不足'
    policy = category['stage3'].get('identity_policy') or {}
    identity_ids = {feature['id'] for feature in policy.get('features', []) if feature['role']=='identity'}
    if (status == 'DIFFERENT' and 'identity_feature' in row and
        (not feature_ids or any(not isinstance(fid,str) or fid not in identity_ids for fid in feature_ids))):
        value['review_status'] = 'UNKNOWN'
        value['validation_note'] = '差异部位不是当前品类的身份特征'
    if 'visual_facts' in row:
        try:
            facts = checked_facts(category, row['visual_facts'], row['goods_id'])
            scored = score(category, facts)
        except (KeyError, TypeError, ValueError):
            facts = None
            scored = None
        if facts is not None:
            value['visual_facts'] = facts
            value['module_result'] = scored
        elif category['stage3'].get('codex_require_module_facts'):
            raise ValueError('pump Codex reviews require valid complete module facts')
        if status == 'SAME' and (scored is None or scored['state'] not in CONFIRMED_STATES):
            value['review_status'] = 'UNKNOWN'
            value['validation_note'] = '完整现协议事实未通过确认同款门槛'
        elif status == 'DIFFERENT' and scored and scored['state'] in CONFIRMED_STATES:
            value['review_status'] = 'UNKNOWN'
            value['validation_note'] = '复核结论与完整事实评分矛盾'
    elif status == 'SAME':
        value['review_status'] = 'UNKNOWN'
        value['validation_note'] = '缺少完整现协议事实，不能确认同款'
    if status=='SIMILAR':
        facts=value.get('visual_facts') or {}
        identity=facts.get('identity',{});design=identity.get('design_comparison',{})
        if (identity.get('verdict')!='SIMILAR' or design.get('relation')!='SIMILAR'
            or not all(concrete(design.get(side,'')) for side in ('amazon','temu'))
            or value['module_result']['state'].startswith('不同款')
            or value['module_result'].get('contradictory_modules')):
            raise ValueError('类似款必须有明确的整体近似款式和双边设计事实')
        value['module_result']={**value['module_result'],'state':'相似款','reason':row['reason']}
    from .visual_decision import binary_mode
    if binary_mode(category):
        if value.get('visual_facts') is None:
            raise ValueError('二分类需要完整原图事实；分析失败应重试，不能生成商品结论')
        scored = value['module_result']
        value['model_review_status'] = status
        value['review_status'] = ('SAME' if scored['state'] in CONFIRMED_STATES else
            'SIMILAR' if scored['state']=='相似款' else
            'DIFFERENT' if scored['state'].startswith('不同款') else 'UNKNOWN')
        value['reason'] = scored['reason']
        value.pop('validation_note', None)
    if status=='SIMILAR' and 'visual_facts' not in value:
        raise ValueError('类似款需要完整双边图片事实')
    if pair.get('supersedes_api') is True:
        value['supersedes_api'] = True
        if not supersedes_api(category, value):
            raise ValueError('replacement review requires complete original-image facts')
    return value


def import_reviews(category, manifest, rows, destination, task_id):
    if not re.fullmatch(r'[A-Za-z0-9_-]+', task_id):
        raise ValueError('invalid review task ID')
    pairs = manifest['pairs'] if isinstance(manifest, dict) else manifest
    bound = {(row['asin'], row['goods_id']): row for row in pairs}
    if len(bound) != len(pairs):
        raise ValueError('duplicate manifest pairs')
    normalized = {}
    for row in rows:
        pair_key = (row.get('asin'), row.get('goods_id'))
        if pair_key not in bound or pair_key in normalized:
            raise ValueError('unexpected or duplicate Codex review pair')
        normalized[pair_key] = normalize_review(category, row, bound[pair_key], task_id)
    destination = Path(destination)
    prior = {(row['asin'], row['goods_id']): row for row in read_jsonl(destination)}
    def content(row):
        return {k:v for k,v in (row or {}).items() if k != 'classified_at'}
    if destination.exists() and all(content(prior.get(key))==content(value) for key,value in normalized.items()):
        return {'imported': len(normalized), 'total_task_reviews': len(prior), 'destination': str(destination),
                'source': SOURCE, 'api_requests_sent': 0, 'changed': False}
    observed = time.time()
    for key,value in normalized.items():
        if content(prior.get(key)) != content(value):
            prior[key] = {**value, 'classified_at': observed}
    destination.parent.mkdir(parents=True, exist_ok=True)
    from .merge_temu_snapshots import atomic_write
    atomic_write(destination,''.join(json.dumps(row,ensure_ascii=False)+'\n' for row in prior.values()))
    return {'imported': len(normalized), 'total_task_reviews': len(prior), 'destination': str(destination),
            'source': SOURCE, 'api_requests_sent': 0, 'changed': True}
