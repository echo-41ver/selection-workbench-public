import json
from pathlib import Path

from selection_automation.category_config import load_category
from selection_automation.rules import score
from selection_automation.vision import protocol
from identity_fixtures import identity

ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / "config" / "categories" / "shower_curtain_local_union_20260922.json"


def facts(relation="SAME", confidence=.9):
    category = json.loads(CONFIG.read_text(encoding="utf-8"))
    return {
        "identity": identity(),
        "modules": {name: {
            "relation": relation, "amazon_evidence": "Amazon可见事实",
            "temu_evidence": "TEMU可见事实", "hard_conflict_code": "NONE",
        } for name in category["stage3"]["modules"]},
        "overall_confidence": confidence,
    }


def test_shower_config_and_batch_protocol_disable_similar():
    category = load_category(CONFIG)
    _, schema = protocol(category, 12)
    relation_enum = schema["properties"]["candidates"]["items"]["properties"]["m"]["properties"]["0"]["properties"]["r"]["enum"]
    assert relation_enum == ["S", "V", "D", "U"]
    assert schema["properties"]["candidates"]["minItems"] == 12


def test_shower_exact_scoring_and_hard_conflict():
    category = load_category(CONFIG)
    same = score(category, facts())
    assert same["total_score"] == 100
    assert same["state"] == "确认同款"
    conflict = facts()
    conflict["modules"]["design_identity"].update(
        relation="DIFFERENT", hard_conflict_code="DIFFERENT_BASE_PATTERN_LAYOUT"
    )
    different = score(category, conflict)
    assert different["state"] == "不同款"
    assert "DIFFERENT_BASE_PATTERN_LAYOUT" in different["hard_conflicts"]


def test_shower_contradictory_marketing_evidence_is_not_automatically_rewritten():
    category = load_category(CONFIG)
    value = facts()
    value["modules"]["design_identity"].update(
        relation="DIFFERENT",
        amazon_evidence="8G Heavy Duty Clear Shower Liner Soap Scum Resistant",
        temu_evidence="5G Heavy Duty Clear Shower Curtain Liner Soap Scum Resistant",
        hard_conflict_code="NONE",
    )
    value["modules"]["spec_variants"]["relation"] = "VARIANT"
    result = score(category, value)
    assert result["state"] == "待复核"
    assert result["modules"]["design_identity"]["relation"] == "DIFFERENT"
    assert result["hard_conflicts"] == []
    assert result["normalization_notes"] == []


def test_shower_zero_visibility_cannot_be_raised_by_counting_module_assertions():
    category = load_category(CONFIG)
    value = facts(confidence=0)
    value["modules"]["bundle_accessories"]["relation"] = "UNKNOWN"
    value["modules"]["spec_variants"]["relation"] = "UNKNOWN"
    result = score(category, value)
    assert result["state"] == "待补充证据"
    assert result["effective_confidence"] == 0
    assert result["known_module_count"] == 5
    assert result["total_score"] is None
    assert result["normalization_notes"] == []


def test_shower_reversed_evidence_requires_review_without_automatic_swap():
    category = load_category(CONFIG)
    value = facts()
    for module in value["modules"].values():
        module["amazon_evidence"] = "候选商品可见事实"
        module["temu_evidence"] = "Amazon基准图可见事实"
    result = score(category, value)
    assert result["state"] == "待复核"
    assert result["modules"]["design_identity"]["amazon_evidence"] == "候选商品可见事实"
    assert result["modules"]["design_identity"]["temu_evidence"] == "Amazon基准图可见事实"
    assert result["normalization_notes"] == []
