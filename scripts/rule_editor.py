"""Compile reviewed text rules into the existing declarative contract."""
import argparse
from copy import deepcopy
import json
import math
import hashlib
from pathlib import Path
import sys
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'src'))
from selection_automation.rules import validate_rules
from selection_automation.vision import protocol
from generate_category import model_json
from selection_automation.commercial import settings as commercial_settings
from selection_automation.workers import worker_count, category_worker_count

MODULE_FIELDS=('modules','module_descriptions','confirmation_minimums','identity_hard_conflicts','variant_fields')


def draft_view(category, draft_id):
    validate_rules(category)
    stage1=category.get('human_stage1_text') or '\n'.join(
        f"属性：{a['name']}；双边明确不同{'排除' if a.get('hard_conflict') else '不硬排除'}；"+
        '取值及文字证据：'+json.dumps(a['values'],ensure_ascii=False)+'；歧义短语：'+json.dumps(a.get('ambiguous_phrases',[]),ensure_ascii=False)
        for a in category['stage1']['attributes'])
    return {'id':draft_id,'stage1_text':stage1,'stage2_threshold':category['stage2']['candidate_threshold'],
            'stage3_text':protocol(category,1)[0],
            'stage3_modules_json':json.dumps({k:category['stage3'].get(k,{ } if k in MODULE_FIELDS[:3] else []) for k in MODULE_FIELDS},ensure_ascii=False,indent=2),
            'amazon_url':category['amazon']['bestsellers_url'],'category_name':category['category_name_zh'],
            'commercial': commercial_settings(category.get('commercial'))}


def edit_rules(category, edits, *, compile_model=model_json):
    result=deepcopy(category); original=draft_view(category,'')
    result['temu']['worker_count']=worker_count(edits.get('worker_count',category_worker_count(category)))
    result['commercial'] = commercial_settings(edits.get('commercial', category.get('commercial')))
    threshold=edits['stage2_threshold']
    if type(threshold) not in (int,float) or not math.isfinite(threshold) or not 0<=threshold<=100:
        raise ValueError('第二层阈值必须为0至100有限数值')
    for field in ('stage1_text','stage3_text'):
        if not isinstance(edits.get(field),str) or not edits[field].strip() or len(edits[field])>30000:
            raise ValueError('规则文本不能为空或超过30000字')
    if edits['stage1_text']!=original['stage1_text']:
        compiled=compile_model('Compile the user-reviewed first-layer rules below into declarative literal phrase attributes. '
            'Return {"attributes":[{"name":"...","hard_conflict":true,"values":{"value":["literal aliases"]},"ambiguous_phrases":[]}]}. '
            'No executable code. Only bilateral unambiguous identity conflicts can hard-exclude. Missing evidence stays unknown. '
            'Preserve the reviewed requirements; do not invent category restrictions. Existing attributes: '+json.dumps(category['stage1']['attributes'],ensure_ascii=False)+
            '\nReviewed rules:\n'+edits['stage1_text'],max_tokens=8000,retry_truncated=False)
        attrs=compiled.get('attributes')
        if not isinstance(attrs,list) or not 1<=len(attrs)<=32: raise ValueError('文字规则编译结果缺少有效属性')
        for attr in attrs:
            if not isinstance(attr,dict) or not isinstance(attr.get('name'),str) or not attr['name'].strip() or type(attr.get('hard_conflict')) is not bool:
                raise ValueError('文字规则属性格式无效')
            if not isinstance(attr.get('values'),dict) or not attr['values']: raise ValueError('文字规则缺少取值')
            for value,phrases in attr['values'].items():
                if not isinstance(value,str) or not isinstance(phrases,list) or not phrases or any(not isinstance(p,str) or not p.strip() for p in phrases): raise ValueError('文字规则必须为字面短语列表')
            if not isinstance(attr.get('ambiguous_phrases',[]),list) or any(not isinstance(p,str) for p in attr.get('ambiguous_phrases',[])): raise ValueError('歧义短语格式无效')
        result['stage1']['attributes']=attrs
        from selection_automation.stage1_authoring import compile_stage1_rules
        result=compile_stage1_rules(result)
    if 'stage3_modules_json' in edits:
        modules=json.loads(edits['stage3_modules_json'])
        if not isinstance(modules,dict) or set(modules)!=set(MODULE_FIELDS): raise ValueError('视觉模块JSON字段不完整')
        if list(modules['modules'])!=list(category['stage3']['modules']):
            raise ValueError('本次编辑仅允许调整现有模块权重，不能改变模块键或顺序')
        for field in ('module_descriptions','identity_hard_conflicts','variant_fields'):
            if modules[field]!=category['stage3'].get(field,{} if field=='module_descriptions' else []):
                raise ValueError('模块说明、冲突代码和变体字段本次只读；请编辑完整视觉提示词')
        result['stage3'].update(modules)
    result['human_stage1_text']=edits['stage1_text']
    from selection_automation.stage2_authoring import manual_threshold
    manual_threshold(result['stage2'],threshold)
    result['stage3']['prompt_override']=edits['stage3_text']
    version=hashlib.sha256(json.dumps({k:v for k,v in edits.items() if k not in ('commercial','draft_id','worker_count')},sort_keys=True,ensure_ascii=False).encode()).hexdigest()[:16]
    result['stage1']['rule_version']='human-reviewed-'+version
    result['stage3']['prompt_version']='human-reviewed-'+version
    result['stage1']['text_gate']='declarative'
    result['execution_mode']='full'
    if result['stage3'].get('identity_policy') is not None:
        from selection_automation.identity_policy import compile_policy, checked_policy
        policy = checked_policy(result, required=True)
        allowed = {f['id']: f['role'] for f in policy['features']}
        if any(a.get('hard_conflict') and allowed[a['name']] != 'identity' for a in result['stage1']['attributes']):
            raise ValueError('该属性在统一同款边界中允许变化或仅供说明，不能单独改成文字硬排除；请先调整品类边界')
        # Weights remain auxiliary; changes cannot make a context/variant module
        # become a required identity module via an edited score minimum.
        result = compile_policy(result)
    validate_rules(result)
    if edits['stage1_text']!=original['stage1_text']:
        from selection_automation.match_criteria import include_manual_attributes
        include_manual_attributes(result)
    return result


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--input',required=True);parser.add_argument('--edits',required=True);parser.add_argument('--output',required=True)
    args=parser.parse_args()
    try:
        result=edit_rules(json.loads(Path(args.input).read_text('utf-8')),json.loads(Path(args.edits).read_text('utf-8')))
        Path(args.output).write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    except Exception:
        print('规则编译或校验失败，请检查文本、视觉模块JSON与API授权后重试',file=sys.stderr);sys.exit(1)
