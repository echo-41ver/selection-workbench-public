"""Ask for category semantics once; fill mechanical configuration locally."""
from copy import deepcopy
import json
from .category_contract import category_contract

FORMAT = 'compact-category-v1'


def authoring_prompt(name, intent, evidence, reviewed):
    policy = category_contract()['field_requirements']['stage3.identity_policy']
    listing = {k: evidence.get(k) for k in ('url', 'title', 'heading', 'rank_count')}
    return (
        '为 Amazon–TEMU 同款核验生成精简品类规则，只返回一个 JSON。不要返回程序配置、模块、权重、提示词或长篇说明。'
        '字段：format="compact-category-v1",category_key(英文下划线标识),product_scope(出售主体/套装范围),'
        'queries(1–3个英文主体关键词),aliases(主体英文/简体/繁体及地区称呼),'
        'exclude_terms(明确其他出售主体的中英文名词，不能排除共享功能词),keyword_features(有标题依据才使用的英文细化词),'
        'attributes(独立文字属性列表：name属性标识,hard_conflict布尔,values字面值到同义短语数组,ambiguous_phrases数组；不要求与图片特征同名),'
        'threshold(本品类第二层0–100保守召回阈值),threshold_reason_zh(一句中文依据),features(1–8项必要特征)。'
        'features沿用下述特征契约，但省略version和modules；不重复描述同一部位，scope和rule各用一句短语。'
        'attributes只列必要互斥属性，每个取值最多3个常用字面同义词；不要穷举规格、型号或数值。整体回答控制在2000中文字以内。'
        '至少一个主图通常可见的identity特征设required_visual=true；不可要求主图证明隐藏部位、精确尺寸、成分或性能。'
        '规则须说明双方实际造型、比例或布局如何对应，不能只写都有屏幕/支架等品类通用部件。不可核实项用context，不作为看图必需条件。'
        '结构、部件位置和构成不能整项设为变体；局部颜色/装饰变化须界定范围并用preserves引用保留的结构。'
        '文字仅双边明确互斥才排除，缺失/多值/否定/适配语境不硬排除；歧义短语绑定具体属性，不能单独填for/适用于。'
        '阈值优先减少不同角度/背景造成的漏检，不照抄固定模板，不声称实测最优。'
        '\n特征契约：' + policy + '\n品类：' + name + '\n目标：' + intent +
        '\n已核实榜单：' + json.dumps(listing, ensure_ascii=False, separators=(',', ':')) +
        '\n已确认品类边界：' + json.dumps(reviewed, ensure_ascii=False, separators=(',', ':')))


def expand(proposal, name, url, amazon_count, temu_count):
    # Existing provider integrations may still return the full supported contract.
    if (isinstance(proposal, dict) and proposal.get('schema_version')=='category-automation-v1'
        and {'amazon','temu','stage1','stage2','stage3'}<=set(proposal)):
        return deepcopy(proposal)
    proposal=normalize(proposal,name)
    features = deepcopy(proposal['features'])
    if not isinstance(features, list) or not 1 <= len(features) <= 8:
        raise ValueError('精简品类规则需要1至8项必要特征')
    visible = [f for f in features if f.get('evidence') in {'image', 'both'}]
    if not visible:
        raise ValueError('缺少可由主图判断的特征')
    config = category_contract()['template']
    config.update(category_key=proposal['category_key'], category_name_zh=name)
    config['amazon'].update(bestsellers_url=url, target_count=amazon_count)
    config['temu'].update(base_queries=proposal['queries'], include_terms=proposal['aliases'],
                         category_aliases=proposal['aliases'], severe_exclude_terms=proposal['exclude_terms'],
                         keyword_features=proposal['keyword_features'], max_products_per_keyword=temu_count)
    config['temu']['subject_filter']['exclude_subject_terms'] = proposal['exclude_terms']
    config['stage1'].update(rule_version=FORMAT, attributes=proposal['attributes'])
    config['stage2'].update(candidate_threshold=proposal['threshold'], threshold_reason_zh=proposal['threshold_reason_zh'])
    modules = {}
    for f in features:
        f['modules'] = ['view_' + f['id']] if f in visible else []
    for i, f in enumerate(visible):
        modules['view_' + f['id']] = 100 // len(visible) + (i < 100 % len(visible))
    config['stage3'].update(modules=modules, module_labels={'view_' + f['id']: f['label'] for f in visible},
                            visual_instructions='按品类特征比较可见事实，未展示不等于不存在。',
                            identity_policy={'version':'category-identity-policy-v2-effects',
                                             'product_scope':proposal['product_scope'], 'features':features},
                            rule_version=FORMAT, prompt_version=FORMAT, schema_version=FORMAT)
    config['rule_authoring_format'] = FORMAT
    return config


def normalize(value,name):
    """Normalize known field spellings/framing, never generate comparison facts."""
    if not isinstance(value,dict):raise ValueError('品类规则须为JSON对象')
    for key in ('config','configuration','rules','category_rules'):
        if isinstance(value.get(key),dict) and not any(k in value for k in ('features','attributes','stage1')):
            value=value[key];break
    stage1=value.get('stage1') or {};stage2=value.get('stage2') or {};stage3=value.get('stage3') or {}
    temu=value.get('temu') or {};policy=value.get('identity_policy') or stage3.get('identity_policy') or {}
    sources={
        'category_key':[value.get('category_key')],
        'product_scope':[value.get('product_scope'),policy.get('product_scope')],
        'queries':[value.get('queries'),value.get('base_queries'),temu.get('base_queries')],
        'aliases':[value.get('aliases'),value.get('category_aliases'),temu.get('category_aliases')],
        'exclude_terms':[value.get('exclude_terms'),value.get('severe_exclude_terms'),temu.get('severe_exclude_terms')],
        'keyword_features':[value.get('keyword_features'),temu.get('keyword_features')],
        'attributes':[value.get('attributes'),value.get('stage1_attributes'),stage1.get('attributes')],
        'threshold':[value.get('threshold'),value.get('candidate_threshold'),stage2.get('candidate_threshold')],
        'threshold_reason_zh':[value.get('threshold_reason_zh'),stage2.get('threshold_reason_zh')],
        'features':[value.get('features'),policy.get('features')]}
    result={'format':FORMAT}
    for key,choices in sources.items():
        known=[v for v in choices if v is not None]
        if known and any(v!=known[0] for v in known[1:]):raise ValueError('规则重复字段含义冲突：'+key)
        if known:result[key]=deepcopy(known[0])
    # These are collection hints rather than mandatory comparison constraints.
    result.setdefault('aliases',[name,*(result.get('queries') or [])])
    result.setdefault('exclude_terms',[]);result.setdefault('keyword_features',[])
    missing={'category_key','product_scope','queries','attributes','threshold','threshold_reason_zh','features'}-set(result)
    if missing:raise ValueError('精简品类规则缺少：'+','.join(sorted(missing)))
    for feature in result['features']:
        if isinstance(feature,dict):
            feature.pop('modules',None)
            if feature.get('evidence')=='unverifiable':
                # The model itself declared this unobservable. It cannot be a
                # mandatory visual identity condition, regardless of its label.
                feature.update(role='context',required_visual=False,preserves=[])
    return result


def add_summary(config):
    if config.get('rule_authoring_format') != FORMAT:
        return config
    features = config['stage3']['identity_policy']['features']
    labels = {f['id']: f['label'] for f in features}
    text = '、'.join(labels.get(a['name'],a['name']) for a in config['stage1']['attributes'] if a['hard_conflict'])
    visual = '；'.join(f['label'] + '：' + f['rule'] for f in features if f['required_visual'])
    config['plain_stage1'] = '文字候选条件：' + (text or '以实际商品参数及用户勾选为准') + '。仅勾选项双方明确冲突才排除；一边缺失继续第二、三层。'
    config['plain_stage3'] = '图片候选条件：' + visual + '。按实际勾选核验主体基础款；明确有无差异与未拍到分开处理。'
    config['plain_rules_summary'] = config['plain_stage1'] + '\n第二层：' + config['stage2']['threshold_reason_zh'] + '\n' + config['plain_stage3']
    config['plain_rules_changes'] = []
    config['rule_summary_source'] = 'generated_conditions_local_summary_v1'
    return config
