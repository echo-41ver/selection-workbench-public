import pytest

from selection_workflow.bath_towel_layer3_v1 import MODULES, score_facts


def facts(relation="SAME"):
    return {"overall_confidence": .9, "modules": {
        key: {"relation": relation, "amazon_evidence": "边框可见", "temu_evidence": "边框可见"}
        for key in MODULES
    }}


def test_removed_material_and_dimension_cannot_create_variant():
    value = facts()
    value["variant_differences"] = ["fiber_material", "dimension_mix"]
    value["uncertainties"] = ["fiber_material"]
    result = score_facts(value)
    assert result["state"] == "确认同款"
    assert result["variant_differences"] == result["uncertainties"] == []


def test_similarity_alone_is_not_an_exclusion():
    result = score_facts(facts("SIMILAR"))
    assert result["total_score"] < 69
    assert result["state"] == "待复核"


def test_visual_variant_is_derived_without_legacy_labels():
    value = facts()
    value["modules"]["color_pattern_variant"]["relation"] = "VARIANT"
    assert score_facts(value)["state"] == "同款（存在规格变体）"


@pytest.mark.parametrize("empty", [" ", [], [" "], {}, None])
def test_missing_bilateral_evidence_is_not_scored(empty):
    value = facts()
    value["modules"]["set_composition"]["amazon_evidence"] = empty
    result = score_facts(value)
    assert result["state"] == "待补充证据"
    assert result["total_score"] is None


def test_affirmative_difference_still_excludes():
    value = facts()
    value["modules"]["surface_design_weave_border"]["relation"] = "DIFFERENT"
    assert score_facts(value)["state"] == "不同款"
