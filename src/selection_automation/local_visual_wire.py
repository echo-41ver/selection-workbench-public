"""Local-only transport constraints; visual evidence and scoring stay unchanged."""
from copy import deepcopy

VERSION = 'local-keyed-candidates-v1'
ERROR_CODE = 'LOCAL_MODEL_INVALID_OUTPUT'
ERROR_MESSAGE = '本地模型连续返回不合格结果，已暂停重复重跑；原返回和已完成结果已保留，请更换可用模型后继续。'


class LocalVisualFormatError(ValueError):
    pass


def prepare(prompt, schema, ids):
    if not ids or len(ids) > 12 or len(set(ids)) != len(ids):
        raise ValueError('invalid local candidate bindings')
    result = deepcopy(schema)
    item = result['properties']['candidates']['items']
    item['properties'].pop('i')
    item['required'].remove('i')
    differences = item['properties'].get('v',{}).get('properties',{}).get('differences')
    if differences is not None:
        _constrain_differences(differences)
    result['properties']['candidates'] = {'type': 'object',
        'properties': {identity: deepcopy(item) for identity in ids},
        'required': list(ids), 'additionalProperties': False}
    prompt += ('\n本机输出格式：candidates 是按固定编号取值的对象，键必须逐字为：'
               + ','.join(ids) + '。每个编号恰好一个候选对象，项内不再写 i；'
               + ('保留原来的全部 m/f/v/c 证据及顶层 amazon（如果要求）。' if differences is not None else '保留原协议的其余字段。') +
               '编号绑定图片格，禁止改成 ASIN、商品ID、从1起的编号或另编编号。'
               + ('IDENTITY/UNRESOLVED 差异的 variant_field 必须为 NONE。' if differences is not None else ''))
    return prompt, result


def _constrain_differences(differences):
    fields = differences['items']['properties']
    # Enforce the decoder's existing invariant during generation as well.
    alternatives = []
    for kinds, variants in [(['VARIANT'], fields['variant_field']['enum']),
                            (['IDENTITY', 'UNRESOLVED'], ['NONE'])]:
        props = {'kind': {'type': 'string', 'enum': kinds},
                 **{k: deepcopy(v) for k, v in fields.items() if k != 'kind'}}
        props['variant_field'] = {'type': 'string', 'enum': list(variants)}
        alternatives.append({'type': 'object', 'properties': props,
                             'required': list(props), 'additionalProperties': False})
    differences['items'] = {'anyOf': alternatives}


def unpack(raw, ids):
    if not isinstance(raw, dict) or set(raw) not in ({'candidates'}, {'amazon', 'candidates'}):
        raise LocalVisualFormatError('invalid local visual envelope')
    candidates = raw['candidates']
    if not isinstance(candidates, dict) or set(candidates) != set(ids):
        raise LocalVisualFormatError('local candidate IDs do not match bound images')
    if any(not isinstance(candidates[i], dict) or 'i' in candidates[i] for i in ids):
        raise LocalVisualFormatError('local candidate contains conflicting ID')
    return {**raw, 'candidates': [{'i': identity, **candidates[identity]} for identity in ids]}
