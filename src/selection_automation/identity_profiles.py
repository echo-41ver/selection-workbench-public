"""Reviewed category boundaries, keyed by category, never by product pair.

Profiles constrain generated policies. They supply no image observations and
cannot override an actual structural conflict. New categories use the generic
effect contract until a reviewed profile exists.
"""

PROFILES = {
    'bath_towel_set': {
        'version': 'towel-reviewed-regions-v1',
        'aliases': ['bath_towel_set', 'bath_towel', '浴巾', '浴巾套装', '浴巾套组'],
        'basis': '用户人工确认：对应阴刻/装饰带位置相同，允许带内花纹及颜色不同；包装不同允许。',
        'required': [
            {'region': 'body', 'effect': 'body_weave', 'role': 'identity'},
            {'region': 'decoration', 'effect': 'layout', 'role': 'identity'},
            {'region': 'decoration', 'effect': 'surface_pattern', 'role': 'variant'},
            {'region': 'decoration', 'effect': 'surface_texture', 'role': 'variant'},
        ],
        'guidance': '先定位毛圈主体与阴刻/装饰区域，再比较条带相对毛巾真实边缘的位置、组数和间隔。'
                    '平织带内线纹或花卉纹样允许不同，但不允许条带区域布局或主体织法改变。'
                    '不得将装饰带内织纹混入主体织法，也不得把折叠外缘或画面左右当作真实边缘。'
                    '大面积花纹必须先确认是否位于对应装饰区域，不能仅因花纹面积大便改变身份。'
                    '套内设计按条带区域结构分组，不按允许的配色和带内图案分组。'
                    '装饰区域布局无法对应时不能仅凭都是毛巾确认。',
    },
    'shower_curtain': {
        'version': 'curtain-reviewed-regions-v1',
        'aliases': ['shower_curtain', 'shower_curtain_local_union_20260922', '浴帘'],
        'basis': '项目已冻结严格浴帘标准：主体印花和版式不同为不同款；明确透明度冲突为身份冲突。',
        'required': [
            {'region': 'body', 'effect': 'surface_pattern', 'role': 'identity'},
            {'region': 'body', 'effect': 'transparency', 'role': 'identity'},
        ],
        'guidance': '主体印花须比较实际元素及排列，都是花卉或同一主题不足以确认。'
                    '明确透明、半透明、不透明属于身份边界；只看光线和背景不能推断透明度。'
                    '透光不等于透明；无法核实则记未知，不能将透明度改为允许变体。',
    },
}


def reviewed_profile(category):
    candidates = {str(category.get(k) or '').strip().casefold() for k in ('category_key', 'category_name_zh')}
    matches = [p for p in PROFILES.values() if candidates & {a.casefold() for a in p['aliases']}]
    if len(matches) > 1:
        raise ValueError('品类名称与标识指向不同的已确认标准')
    return matches[0] if matches else None


def validate_profile(category, features):
    profile = reviewed_profile(category)
    if profile is None:
        return
    for boundary in profile['required']:
        matching = [f for f in features if f['region'] == boundary['region'] and boundary['effect'] in f['effects']]
        if not matching or any(f['role'] != boundary['role'] for f in matching):
            raise ValueError('生成标准违背已确认品类边界：' + boundary['region'] + '/' +
                             boundary['effect'] + ' 必须为 ' + boundary['role'])
    # A decoration variant must preserve both the main weave and the band layout.
    for f in features:
        if f['role'] != 'variant' or f['region'] != 'decoration':
            continue
        for boundary in profile['required']:
            if boundary['role'] == 'identity' and not any(
                other['id'] in f['preserves'] and other['region'] == boundary['region'] and
                boundary['effect'] in other['effects'] for other in features
            ):
                raise ValueError('装饰变体必须保留主体织法和装饰区域布局，不能只保留品类名称')
