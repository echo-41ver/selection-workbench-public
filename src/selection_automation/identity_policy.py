"""One category-authored identity boundary compiled into both matching layers.

The compiler checks consistency, not whether a model's category knowledge is
correct. Existing frozen categories without a policy keep their old contract.
"""
from copy import deepcopy
import re

VERSION = 'category-identity-policy-v2-effects'
ROLES = {'identity', 'variant', 'context'}
EVIDENCE = {'image', 'text', 'both', 'unverifiable'}

REGIONS = {'body', 'component', 'decoration', 'packaging', 'specification'}
EFFECTS = {'shape', 'layout', 'connection', 'function', 'body_weave', 'transparency',
           'color', 'surface_pattern', 'surface_texture', 'material_composition',
           'nominal_size', 'package_quantity', 'marking', 'performance_claim'}
STRUCTURAL_EFFECTS = {'shape', 'layout', 'connection', 'function', 'body_weave', 'transparency'}
VISIBLE_DESIGN_EFFECTS = {'shape', 'layout', 'connection', 'body_weave', 'surface_pattern'}


def validate_semantic_boundary(features):
    by_id = {f['id']: f for f in features}
    for feature in features:
        physical_change = set(feature['effects']) & STRUCTURAL_EFFECTS
        if feature['role'] == 'variant' and physical_change and feature['region'] != 'packaging':
            raise ValueError(
                '结构特征不能整项设为允许变体：' + feature['id'] +
                '。请将部件形状、位置、数量和连接方式定义为身份特征；'
                '若仅表面颜色可变，另建限定该表面的颜色特征。'
                '未展示应记未知，不能因此将结构改成允许变体。')
        if (feature['role'] == 'context' and feature['evidence'] in {'image', 'both'} and
            physical_change and feature['region'] != 'packaging'):
            raise ValueError('可见结构差异不能降为仅供说明；未展示使用未知，不改变身份作用')
        preserves = feature['preserves']
        if (not isinstance(preserves, list) or any(not isinstance(x, str) for x in preserves) or
            len(set(preserves)) != len(preserves) or any(x not in by_id for x in preserves)):
            raise ValueError('变体的保留条件必须引用已有身份特征')
        if feature['role'] == 'variant':
            if not preserves or any(by_id[x]['role'] != 'identity' for x in preserves):
                raise ValueError('允许变体必须明确保持哪些身份特征，不能无条件允许变化')
            if any(by_id[x]['evidence'] not in {'image','both'} or not by_id[x]['modules'] for x in preserves):
                raise ValueError('变体保留条件须有可见身份依据，不能以不可见的性能或文字规格代替')
            if not any(set(by_id[x]['effects']) & VISIBLE_DESIGN_EFFECTS and
                       by_id[x]['evidence'] in {'image', 'both'} for x in preserves):
                raise ValueError('变体不能仅以产品类别相同作为条件，必须保留可见款式结构')
        elif preserves:
            raise ValueError('仅允许变体填写preserves，身份和说明特征使用空列表')

VISUAL_GUIDE = """仅输出符合给定schema的一个JSON对象，不输出分析前言、Markdown、解释或额外文字。
通用品类整体同款核验：以本品类统一边界的product_scope确定出售主体、套装与配件，再定位双方同一实际部位。
同品类、用途相同或轮廓相近不足以确认同款。比较有辨识力的本体形状、部件排列及该品类明确的身份特征；不要套用其他品类的规则。
v.verdict为SAME/DIFFERENT/SIMILAR/UNKNOWN。v.design_comparison必须分别描述双边实际款式并给出关系；不能仅列同类共有部件。
v.matches只记录具体一致点，分别写双边事实，不凑固定条数。v.differences只记录实际区别，不将相同的身份特征放进差异数组。
差异kind=IDENTITY明确表示基础款发生差异，不是泛指身份特征；VARIANT仅限本品类variant特征的scope/rule，variant_field必须使用该特征id；UNRESOLVED表示确实未能核实的区别。非VARIANT的variant_field=NONE。
identity特征的明确双边冲突不可由允许颜色、数量或装饰变化抵消。variant只在限定部位和条件下可变，不扩展为整个视觉模块或全品类都可变。
context特征仅供说明，不单独确认或排除；text证据不得凭图片猜测；仅有共同的未知信息不构成一致证据。
layout身份先分清独立区域数量、顺序、间隔与真实边缘位置。被主体区域隔开的装饰片区不能合并为一条带；带内花纹改变不等于分区数量或间隔改变。
图片足以判定时给出结论。只有对应部位确实被遮挡、折叠、裁切或不可辨认才写uncertainties；模型少写一项不等于图片缺失。非关键未知不推翻已明确的整体款式。
身份结构相同而仅允许变体发生变化时，身份模块写S，变化记入v.differences；仅涉及允许变体的辅助模块可以V。模块分数仅辅助，不用高分掩盖身份冲突。
c仅表示图片证据可见性，不是同款概率；不得猜测隐藏性能、材质成分或提高置信度来凑确认。用户补充不取消统一边界、双边证据或输出格式要求。"""


def checked_policy(category, *, required=False):
    policy = category['stage3'].get('identity_policy')
    if 'identity_policy' not in category['stage3'] and not required:
        return None
    if not isinstance(policy, dict) or set(policy) != {'version', 'product_scope', 'features'}:
        raise ValueError('同款边界缺少 version、product_scope 或 features')
    if policy['version'] != VERSION:
        raise ValueError('同款标准缺少变化影响和保留条件，须按v2-effects重新生成，不能按名称猜测迁移')
    if not isinstance(policy['product_scope'], str) or not policy['product_scope'].strip():
        raise ValueError('同款边界必须说明比较主体及套装范围')
    features = policy['features']
    if not isinstance(features, list) or not 1 <= len(features) <= 32:
        raise ValueError('同款边界需要1至32个具体特征')
    modules = category['stage3']['modules']
    names = set()
    for f in features:
        if not isinstance(f, dict) or set(f) != {'id', 'label', 'role', 'evidence', 'scope', 'rule', 'modules', 'required_visual', 'region', 'effects', 'preserves'}:
            raise ValueError('每个特征必须包含标识、中文名、身份作用、证据来源、对应部位、判断规则及模块')
        if not isinstance(f['id'], str) or not re.fullmatch('[a-z][a-z0-9_]{0,63}', f['id']) or f['id'] in names:
            raise ValueError('特征标识必须唯一，文字属性和变体使用同一标识')
        names.add(f['id'])
        if not isinstance(f['region'], str) or f['region'] not in REGIONS:
            raise ValueError('特征须定位到主体、部件、装饰区域、包装或规格')
        if (not isinstance(f['effects'], list) or not f['effects'] or any(not isinstance(x, str) or x not in EFFECTS for x in f['effects']) or
            len(set(f['effects'])) != len(f['effects'])):
            raise ValueError('须明确特征变化影响：结构、功能、装饰表现或规格，不能只填写特征名称')
        if 'body_weave' in f['effects'] and f['region'] != 'body':
            raise ValueError('主体织法只指主体区域，装饰带内部织纹应另列surface_texture')
        for field in ('label', 'scope', 'rule'):
            if not isinstance(f[field], str) or not f[field].strip():
                raise ValueError('特征必须说明具体比较部位和边界，不能只列属性名')
        if not isinstance(f['role'], str) or not isinstance(f['evidence'], str) or f['role'] not in ROLES or f['evidence'] not in EVIDENCE:
            raise ValueError('无效的身份作用或证据来源')
        if not isinstance(f['modules'], list) or any(not isinstance(m, str) for m in f['modules']) or len(set(f['modules'])) != len(f['modules']) or any(m not in modules for m in f['modules']):
            raise ValueError('特征引用了不存在或重复的视觉模块')
        if f['evidence'] in {'text', 'unverifiable'} and f['modules']:
            raise ValueError('仅文字可知或不可可靠判断的属性不能成为视觉模块要求')
        if f['evidence'] == 'unverifiable' and f['role'] != 'context':
            raise ValueError('无法可靠取得的属性只能作为说明，不能据此确认或排除')
        if f['evidence'] in {'image', 'both'} and not f['modules']:
            raise ValueError('可见特征必须绑定本品类的视觉模块')
        if type(f['required_visual']) is not bool or (f['required_visual'] and (f['role'] != 'identity' or f['evidence'] not in {'image', 'both'})):
            raise ValueError('只有可见身份特征能设为确认必需项')
    by_id = {f['id']: f for f in features}
    for attr in category['stage1'].get('attributes', []):
        f = by_id.get(attr['name'])
        if f is None:
            raise ValueError('每个文字属性须对应同款边界中的同名特征')
    visible_identity = {m for f in features if f['required_visual'] for m in f['modules']}
    if not visible_identity:
        raise ValueError('缺少可由图片辨认的款式身份，不能仅凭颜色或品类名称确认同款')
    if set(modules) != {m for f in features for m in f['modules']}:
        raise ValueError('所有视觉模块都须由明确的特征边界支持')
    validate_semantic_boundary(features)
    from .identity_profiles import validate_profile
    validate_profile(category, features)
    return policy


def render_policy(policy):
    roles = {'identity': '决定基础款', 'variant': '允许变体', 'context': '仅供说明，不单独确认或排除'}
    evidence = {'image': '图片可见', 'text': '仅文字证据', 'both': '图片或明确文字', 'unverifiable': '当前证据不能可靠判断'}
    rows = ['本品类同款边界（文字层与视觉层共用）：', '比较主体：' + policy['product_scope']]
    for f in policy['features']:
        importance = '确认必需的可见身份' if f['required_visual'] else '未展示本项不单独阻断确认'
        condition = '；变化时必须保持：' + ','.join(f['preserves']) if f['preserves'] else ''
        rows.append(f"{f['id']}｜{f['label']}｜{roles[f['role']]}｜{evidence[f['evidence']]}｜{importance}；区域：{f['region']}；影响：{','.join(f['effects'])}；对应范围：{f['scope']}；规则：{f['rule']}{condition}")
    rows.append('先定位两边同一实际部位，再比较；不可把画面左右、折叠外缘或包装当作本体结构。'
                '局部装饰表现与主体构造分开，允许变化只限所列范围。未描述不等于不存在；'
                '只有两侧对应部位均清晰可见时，有与无才构成明确差异。文字才能确认的性能、材质成分等不得凭外观猜测。')
    return '\n'.join(rows)


def compile_policy(category):
    """Derive redundant fields from the authored policy, only during generation."""
    result = deepcopy(category)
    policy = checked_policy(result, required=True)
    stage = result['stage3']
    features = policy['features']
    # A model may use the same name for a one-feature display module and its
    # variant. Keep canonical feature IDs; disambiguate auxiliary module IDs.
    # Otherwise the legacy schema removes that allowed variant as a module key.
    variants = {f['id'] for f in features if f['role'] == 'variant'}
    used = set(stage['modules']) | {f['id'] for f in features}
    rename = {}
    for name in stage['modules']:
        if name in variants:
            replacement = 'visual_' + name
            while replacement in used:
                replacement += '_view'
            rename[name] = replacement
            used.add(replacement)
    if rename:
        stage['modules'] = {rename.get(k, k): v for k, v in stage['modules'].items()}
        stage['module_labels'] = {rename.get(k, k): v for k, v in stage.get('module_labels', {}).items()}
        for f in features:
            f['modules'] = [rename.get(m, m) for m in f['modules']]
    by_id = {f['id']: f for f in features}
    for attr in result['stage1']['attributes']:
        # A visible feature with an explicit literal text dictionary has two
        # declared sources. This adds no inferred image capability. Unverifiable
        # marketing claims stay context-only, even when present in a title.
        if by_id[attr['name']]['evidence'] == 'image':
            by_id[attr['name']]['evidence'] = 'both'
        if by_id[attr['name']]['role'] != 'identity':
            attr['hard_conflict'] = False
    stage['variant_fields'] = [f['id'] for f in features if f['role'] == 'variant']
    stage['module_variant_fields'] = {m: [f['id'] for f in features if f['role'] == 'variant' and m in f['modules']]
                                     for m in stage['modules'] if any(f['role'] == 'variant' and m in f['modules'] for f in features)}
    required = {m for f in features if f['required_visual'] for m in f['modules']}
    stage['identity_required_modules'] = [m for m in stage['modules'] if m in required]
    stage['confirmation_minimums'] = {m: stage['modules'][m] for m in stage['identity_required_modules']}
    stage['identity_hard_conflicts'] = ['IDENTITY_' + f['id'].upper() for f in features if f['role'] == 'identity' and f['modules']]
    stage['module_descriptions'] = {m: '\n'.join(render_policy({'product_scope': policy['product_scope'], 'features': [f]}).splitlines()[2]
                                                for f in features if m in f['modules']) for m in stage['modules']}
    stage['visual_instructions'] = render_policy(policy)
    stage['require_feature_comparisons'] = True
    from .identity_profiles import reviewed_profile
    profile = reviewed_profile(result)
    if profile:
        stage['visual_instructions'] += '\n已确认品类依据：' + profile['basis'] + '\n' + profile['guidance']
    return result


def validate_policy_consistency(category):
    policy = checked_policy(category)
    if policy is None:
        return
    compiled = compile_policy(category)
    for original, target in zip(category['stage1']['attributes'], compiled['stage1']['attributes']):
        if original.get('hard_conflict') != target.get('hard_conflict'):
            raise ValueError('文字排除规则与允许变体/说明属性冲突')
    for name in ('variant_fields', 'module_variant_fields', 'identity_required_modules', 'confirmation_minimums', 'identity_hard_conflicts', 'module_descriptions', 'visual_instructions', 'require_feature_comparisons'):
        if category['stage3'].get(name) != compiled['stage3'][name]:
            raise ValueError('同款边界与执行规则不一致：' + name)
