"""One-shot plain Chinese explanation or constrained draft rule modification."""
from copy import deepcopy
import argparse
import hashlib
import json
from pathlib import Path
import re
import sys
from generate_category import model_json, CategoryGenerationError
from rule_editor import draft_view, edit_rules


class UnsupportedRules(ValueError): pass


def chinese_text(value):
    return (isinstance(value,str) and 0<len(value.strip())<=12000
            and bool(re.search('[\u4e00-\u9fff]',value))
            and not value.lstrip().startswith(('{','[','```')))


def apply_plain_rules(category, instruction, *, model=model_json, scope='all'):
    if scope not in ('all','stage1','stage3'): raise ValueError('规则层无效')
    if not isinstance(instruction,str) or len(instruction)>12000: raise ValueError('要求文本无效')
    view=draft_view(category,'')
    if scope=='stage3' and instruction.strip():
        # Visual prose is already executable input to the fixed visual protocol.
        # Store it directly instead of paying a model to echo unrelated layers.
        result=deepcopy(category)
        result['stage3']['prompt_override']=instruction.strip()
        result['stage3']['prompt_version']='human-reviewed-'+hashlib.sha256(instruction.strip().encode()).hexdigest()[:16]
        result['plain_stage3']=instruction.strip()
        result['plain_rules_summary']='第三层要求已保存并加入视觉分析提示词；固定同款边界和评分规则继续生效。'
        result['plain_rules_changes']=['第三层补充要求已更新，第一层、第二层和视觉模块保持原配置。']
        from selection_automation.rules import validate_rules
        validate_rules(result)
        return result
    current={'stage1_attributes':category['stage1']['attributes'],'stage2_threshold':view['stage2_threshold'],
             'stage3_prompt':view['stage3_text']}
    fields=('supported(boolean), stage1_attributes, plain_summary(中文人话), changes(中文句子数组)' if instruction.strip() and scope=='stage1' else
            'supported(boolean), stage1_attributes, stage2_threshold, stage3_prompt, plain_summary(中文人话), changes(中文句子数组)' if instruction.strip()
            else 'supported(boolean), plain_summary(中文人话), changes(空数组)')
    prompt=('用通俗中文解释或修改当前三层商品同款筛选。只返回JSON对象，字段严格为 '+fields+'。'
            '用户要求为空时仅解释，不返回或修改执行规则，changes=[]。非空时只修改能用现有声明属性/阈值/完整视觉提示词表达的要求。'
            '第一层属性name/hard_conflict/values/ambiguous_phrases；values是属性值到字面证据短语数组，仅双边单一明确不同可排除，不能实现数量解析、任意算法或额外联网。'
            '不能变更视觉模块键、序号映射、分数权重、硬冲突代码、变体字段或输出schema；如要求超出能力supported=false并中文说明，不假装已应用。'
            '第三层先判断整体基础款并记录具体双边共同细节、身份差异和未知项；模块分数仅作辅助，不能用高分抵消明确差异。'
            '通用整体身份协议固定保留，用户视觉规则作为品类补充；缺失证据UNKNOWN，本地检查事实与矛盾后产生最终状态。'
            'plain_summary必须以中文普通句子解释第一层规则、第二层阈值和第三层判断重点，不能是英文JSON或属性清单。'
            '不要输出API密钥、路径、公司配置、代码。当前规则：'+json.dumps(current,ensure_ascii=False)+
            '\n固定视觉映射：'+view['stage3_modules_json']+'\n用户要求：'+instruction)
    prompt+='\n本次范围：'+scope+'。stage1只解释/修改文字筛选，stage3只解释/修改图片判断。'
    if instruction.strip(): prompt+='其他层必须原样返回，不得联动修改。'
    else: prompt+='仅解释：plain_summary不超过600字；不逐条复述属性、词库或固定映射，不返回执行规则字段。'
    if scope=='all': prompt+='额外返回plain_stage1和plain_stage3两个中文字符串，分别独立解释第一层文字判断和第三层图片判断，避免英文术语；不混入另一层。'
    if not instruction.strip(): prompt+='plain_stage1和plain_stage3各不超过300字。'
    # Thinking shares the output budget. Retry explanations only on an explicit
    # length error, once, with a bounded increase; all other failures stop.
    budgets=(16000,) if instruction.strip() else (8000,16000)
    for attempt,budget in enumerate(budgets):
        try:
            response=model(prompt,max_tokens=budget,retry_truncated=False)
            break
        except CategoryGenerationError as exc:
            if exc.code=='MODEL_OUTPUT_TRUNCATED' and attempt+1<len(budgets):
                continue
            operation='规则修改生成' if instruction.strip() else '中文说明生成'
            reasons={'MODEL_OUTPUT_TRUNCATED':'模型输出达到长度上限',
                     'MODEL_NETWORK_ERROR':'模型连接超时或网络失败',
                     'MODEL_INVALID_JSON':'模型返回格式无效'}
            reason=reasons.get(exc.code,'模型请求失败')
            if exc.code.startswith('MODEL_HTTP_'): reason='模型请求返回 HTTP '+exc.code.removeprefix('MODEL_HTTP_')
            raise CategoryGenerationError(operation+'失败：'+reason+'；原执行规则已保留。',exc.code) from None
    summaries={}
    if isinstance(response,dict):
        response=dict(response)
        if instruction.strip() and scope=='stage1':
            # Unselected layers need not be echoed. Explicit changes still fail
            # the protected-layer comparison below.
            for key in ('stage2_threshold','stage3_prompt'):
                response.setdefault(key,current[key])
        for field in ('plain_stage1','plain_stage3'):
            value=response.pop(field,None)
            if value is not None:
                if not chinese_text(value): raise ValueError('分层说明必须为中文')
                summaries[field]=value
    # An explanation is read-only: accept harmless provider metadata rather
    # than rejecting valid Chinese prose. Never apply returned rule fields.
    if not instruction.strip() and isinstance(response,dict) and {'supported','plain_summary','changes'}<=set(response):
        for key,value in current.items():
            if key in response and response[key]!=value:
                raise ValueError('仅解释请求不得修改规则')
        response={key:response[key] for key in ('supported','plain_summary','changes')}
    if not instruction.strip() and isinstance(response,dict) and set(response)=={'supported','plain_summary','changes'}:
        response={**response,**current}
    allowed={'supported','stage1_attributes','stage2_threshold','stage3_prompt','plain_summary','changes'}
    if not isinstance(response,dict) or set(response)!=allowed or type(response['supported']) is not bool:
        raise ValueError('规则说明返回结构无效')
    if not chinese_text(response['plain_summary']): raise ValueError('规则说明必须是通俗中文')
    if response['supported'] is False: raise UnsupportedRules(response['plain_summary'])
    if not isinstance(response['changes'],list) or any(not chinese_text(x) for x in response['changes']): raise ValueError('修改说明格式无效')
    result=deepcopy(category)
    if not instruction.strip():
        if any(response[k]!=v for k,v in current.items()) or response['changes']:
            raise ValueError('仅解释请求不得修改规则')
    else:
        protected=('stage2_threshold','stage3_prompt') if scope=='stage1' else ('stage1_attributes','stage2_threshold') if scope=='stage3' else ()
        if any(response[key]!=current[key] for key in protected):
            raise ValueError('本次修改越过所选规则层，原规则已保留')
        edits={'stage1_text':'人工要求：'+instruction,'stage2_threshold':response['stage2_threshold'],
               'stage3_text':response['stage3_prompt']}
        # Reuse strict attribute validation without another model call.
        result=edit_rules(category,edits,compile_model=lambda *a,**k:{'attributes':response['stage1_attributes']})
        result.pop('human_stage1_text',None)
        if all(response[k]==v for k,v in current.items()): raise UnsupportedRules('没有产生可执行规则修改，请具体说明需要改变哪条判断')
    if scope=='stage1':
        result['stage2']=deepcopy(category['stage2']);result['stage3']=deepcopy(category['stage3'])
    elif scope=='stage3':
        result['stage1']=deepcopy(category['stage1']);result['stage2']=deepcopy(category['stage2'])
        if 'human_stage1_text' in category: result['human_stage1_text']=category['human_stage1_text']
    result.update(summaries)
    if scope!='all': result['plain_'+scope]=response['plain_summary']
    result['plain_rules_summary']=response['plain_summary']
    result['plain_rules_changes']=response['changes']
    return result


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--input',required=True);parser.add_argument('--instruction-file',required=True);parser.add_argument('--output',required=True);parser.add_argument('--error-file',required=True)
    parser.add_argument('--scope',choices=['all','stage1','stage3'],default='all')
    args=parser.parse_args()
    try:
        result=apply_plain_rules(json.loads(Path(args.input).read_text('utf-8')),Path(args.instruction_file).read_text('utf-8'),scope=args.scope)
        target=Path(args.output);temp=target.with_suffix('.tmp');temp.write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8');temp.replace(target)
    except Exception as exc:
        if isinstance(exc,CategoryGenerationError):
            code,message=exc.code,str(exc)
        elif isinstance(exc,UnsupportedRules):
            code,message='UNSUPPORTED',str(exc)
        elif isinstance(exc,ValueError):
            code,message='PLAIN_RULES_FORMAT','应用失败：'+str(exc)[:180]+'。原执行规则已保留，请修改要求后重试。'
        else:
            code,message='PLAIN_RULES_FAILED','中文说明处理失败，原执行规则已保留。'
        Path(args.error_file).write_text(json.dumps({'code':code,'message':message},ensure_ascii=False),encoding='utf-8')
        sys.exit(1)
