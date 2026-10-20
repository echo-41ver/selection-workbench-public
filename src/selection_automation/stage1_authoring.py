"""Compile attribute-bound ambiguity before new task rules are frozen.

Frozen task readers deliberately do not call this authoring pass.
"""
from copy import deepcopy
import re

COMPATIBILITY_MARKERS={'for','compatible with','compatible','fits','fit','replacement for',
    'designed for','works with','use with','suitable for','适用于','適用於','适配','適配','兼容','用于','用於'}


def normalize_attribute_aliases(values):
    """Normalize literal list framing without inventing attribute meanings."""
    if not isinstance(values,dict) or not values:
        raise ValueError('第一层属性取值须为非空对象')
    result={}
    for value,raw in values.items():
        if not isinstance(value,str) or not value.strip():
            raise ValueError('第一层属性取值名称必须是非空文字')
        if isinstance(raw,str):
            phrases=[phrase.strip() for phrase in re.split(r'[|｜]',raw) if phrase.strip()]
        elif isinstance(raw,list) and all(isinstance(phrase,str) and phrase.strip() for phrase in raw):
            phrases=[phrase.strip() for phrase in raw]
        else:
            raise ValueError('第一层属性同义词须为文字数组或竖线分隔文字')
        if not phrases:raise ValueError('第一层属性同义词必须包含非空文字')
        result[value]=list(dict.fromkeys(phrases))
    return result


def compile_stage1_rules(category):
    result=deepcopy(category)
    if result.get('stage1',{}).get('text_gate')!='declarative':return result
    for attr in result['stage1'].get('attributes',[]):
        attr['values']=normalize_attribute_aliases(attr.get('values'))
        if 'ambiguous_phrases' not in attr:continue
        if not isinstance(attr['ambiguous_phrases'],list):
            raise ValueError('第一层歧义短语须为文字数组')
        aliases=[phrase for phrases in attr.get('values',{}).values() for phrase in phrases]
        phrases=[]
        for raw in attr.get('ambiguous_phrases',[]):
            if not isinstance(raw,str) or not raw.strip():raise ValueError('第一层歧义短语必须是非空文字')
            phrase=raw.strip()
            if phrase.casefold() in COMPATIBILITY_MARKERS:
                # "for PC" must not erase an explicit wired/wireless fact.
                # An actual reference to "for wireless keyboard" remains
                # uncertain for the attribute containing that exact alias.
                for alias in aliases:
                    for separator in ((' ',) if phrase.isascii() else ('',' ')):
                        phrases.append(phrase+separator+alias)
            else:phrases.append(phrase)
        attr['ambiguous_phrases']=list(dict.fromkeys(phrases))
    validate_stage1_authoring(result)
    return result


def validate_stage1_authoring(category):
    """Reject rules that cannot exclude any of their own explicit conflicts."""
    from .rules import text_gate
    variants=set(category['stage3'].get('variant_fields',[]))-set(category['stage3']['modules'])
    for attr in category['stage1'].get('attributes',[]):
        if not attr.get('hard_conflict') or attr['name'] in variants:continue
        values=list(attr['values'].values())
        if len(values)<2:continue
        probe=deepcopy(category);probe['stage1']['attributes']=[attr]
        # This validates the authored dictionary itself. A task's independent
        # selected/observed parameters can legitimately use different values.
        probe.pop('match_criteria',None)
        tested=False
        for i,left in enumerate(values):
            for right in values[i+1:]:
                for a in left[:3]:
                    for b in right[:3]:
                        row=text_gate(probe,a,b)
                        if row['excluded']:tested=True;break
                    if tested:break
                if tested:break
            if tested:break
        if not tested:
            raise ValueError('第一层属性 '+attr['name']+' 的明确冲突也被规则判成不足；请修正规则的同义词或歧义短语')
