from copy import deepcopy
import pytest
from selection_automation.stage2_authoring import generated_threshold,manual_threshold


@pytest.mark.parametrize('value',[32,47.5,63])
def test_category_specific_value_is_preserved_without_universal_fallback(value):
    category={'stage2':{'candidate_threshold':value,'threshold_reason_zh':'该品类存在角度和背景变化，优先降低漏检风险。'}}
    before=deepcopy(category);out=generated_threshold(category)['stage2']
    assert out['candidate_threshold']==value
    assert out['model_recommended_threshold']==value
    assert out['threshold_source']=='configured_rule_api'
    assert out['threshold_preference']=='RECALL_FIRST'
    assert out['threshold_status'].startswith('PROVISIONAL')
    assert out['below_threshold_action']=='NOT_SELECTED_NOT_MANUAL_REVIEW'
    assert category==before


@pytest.mark.parametrize('value',[None,True,float('nan'),float('inf'),-1,101,'55'])
def test_invalid_generated_threshold_fails_instead_of_silently_using_60(value):
    with pytest.raises(ValueError,match='本品类'):
        generated_threshold({'stage2':{'candidate_threshold':value,'threshold_reason_zh':'保守召回。'}})


def test_generation_requires_category_rationale():
    with pytest.raises(ValueError,match='保守召回依据'):
        generated_threshold({'stage2':{'candidate_threshold':60}})


def test_manual_override_is_preserved_and_original_recommendation_retained():
    stage=generated_threshold({'stage2':{'candidate_threshold':45,'threshold_reason_zh':'保守召回。'}})['stage2']
    manual_threshold(stage,37)
    assert stage['candidate_threshold']==37 and stage['threshold_source']=='manual'
    assert stage['model_recommended_threshold']==45
    manual_threshold(stage,37)
    assert stage['threshold_source']=='manual'
