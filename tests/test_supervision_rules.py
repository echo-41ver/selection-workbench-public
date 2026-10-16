import pytest

from selection_automation.rules import score


@pytest.fixture
def category():
    return {"stage3": {
        "modules": {"identity": 60, "construction": 40},
        "identity_hard_conflicts": ["DIFFERENT_DESIGN"],
        "confirmation_minimums": {"identity": 50},
        "same_threshold": 88,
        "different_score_ceiling": 79,
        "rule_version": "evidence-test-v1",
    }}


def facts(relation="DIFFERENT"):
    return {"overall_confidence": 0.95, "modules": {
        "identity": {
            "relation": relation,
            "amazon_evidence": "Amazon: striped design",
            "temu_evidence": "TEMU: floral design",
            "hard_conflict_code": "DIFFERENT_DESIGN",
        },
        "construction": {
            "relation": "SAME",
            "amazon_evidence": "Amazon: hemmed edge",
            "temu_evidence": "TEMU: hemmed edge",
        },
    }}


def assert_unknown_identity(result):
    assert result["modules"]["identity"]["relation"] == "UNKNOWN"
    assert result["modules"]["identity"]["score"] is None
    assert result["modules"]["construction"]["score"] == 40
    assert result["known_module_count"] == 1
    assert result["total_score"] is None
    assert result["hard_conflicts"] == []
    assert result["state"] == "待补充证据"


@pytest.mark.parametrize("side", ["amazon_evidence", "temu_evidence"])
@pytest.mark.parametrize("relation", ["SAME", "DIFFERENT"])
@pytest.mark.parametrize("invalid", ["", "   ", "\t\r\n", None, 1, True, ["evidence"], {"text": "evidence"}])
def test_invalid_evidence_is_unknown_and_cannot_contribute_or_exclude(category, side, relation, invalid):
    value = facts(relation)
    value["modules"]["identity"][side] = invalid

    assert_unknown_identity(score(category, value))


@pytest.mark.parametrize("side", ["amazon_evidence", "temu_evidence"])
def test_missing_evidence_is_unknown_and_cannot_exclude(category, side):
    value = facts()
    del value["modules"]["identity"][side]

    assert_unknown_identity(score(category, value))


@pytest.mark.parametrize("other_module_unknown", [False, True])
def test_valid_bilateral_hard_conflict_still_excludes(category, other_module_unknown):
    value = facts()
    value["modules"]["identity"]["amazon_evidence"] = "  Amazon: striped design\t"
    if other_module_unknown:
        del value["modules"]["construction"]

    result = score(category, value)

    assert result["modules"]["identity"]["relation"] == "DIFFERENT"
    assert result["modules"]["identity"]["score"] == 0
    assert result["hard_conflicts"] == ["DIFFERENT_DESIGN"]
    assert result["state"] == "不同款"
    assert result["known_module_count"] == (1 if other_module_unknown else 2)
    assert result["total_score"] == (None if other_module_unknown else 40)
