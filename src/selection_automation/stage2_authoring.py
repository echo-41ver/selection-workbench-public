"""Keep per-category, recall-first suggestions distinct from manual settings."""
from copy import deepcopy
import math
import re


def generated_threshold(category):
    result=deepcopy(category);stage=result['stage2']
    value=stage.get('candidate_threshold')
    if type(value) not in (int,float) or not math.isfinite(value) or not 0<=value<=100:
        raise ValueError('请由当前规则生成接口给出本品类的第二层候选阈值（0–100），不能使用空值或固定模板回退')
    reason=stage.get('threshold_reason_zh')
    if not isinstance(reason,str) or not reason.strip() or len(reason)>2000 or not re.search('[\u4e00-\u9fff]',reason):
        raise ValueError('第二层阈值须说明本品类的保守召回依据')
    stage.update(model_recommended_threshold=value,threshold_source='configured_rule_api',
                 threshold_preference='RECALL_FIRST',threshold_status='PROVISIONAL_MODEL_SUGGESTED_RECALL_FIRST',
                 below_threshold_action='NOT_SELECTED_NOT_MANUAL_REVIEW',included_in_final_score=False)
    return result


def manual_threshold(stage, value):
    if value!=stage.get('candidate_threshold'):
        stage.update(threshold_source='manual',threshold_status='PROVISIONAL_MANUAL_UNCALIBRATED')
    stage['candidate_threshold']=value
