import importlib.util
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("prior_import", ROOT / "scripts" / "import_prior_shower_layer3.py")
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def test_normalize_requires_all_seven_modules():
    category = {
        "stage3": {
            "modules": {name: weight for name, weight in zip(
                ("design_identity", "core_structure", "material_opacity", "function_subtype", "surface_details", "bundle_accessories", "spec_variants"),
                (32, 18, 15, 12, 10, 7, 6), strict=True)},
            "identity_hard_conflicts": ["DIFFERENT_BASE_PATTERN_LAYOUT"],
            "scoring_policy": "shower_curtain_v3_1", "confirmation_minimums": {},
            "rule_version": "test", "threshold_status": "实测", "same_threshold": 86,
            "different_score_ceiling": 71,
        }
    }
    row = {"visual_facts": {"modules": {}, "overall_confidence": .9}}
    assert MODULE.normalize_facts(category, row) is None


def test_evidence_list_becomes_auditable_text():
    assert MODULE.evidence(["证据一", "", "证据二"]) == "证据一；证据二"


def test_amazon_resize_suffix_does_not_change_source_identity():
    left = "https://m.media-amazon.com/images/I/7170px9vGUL._AC_SX679_.jpg"
    right = "https://m.media-amazon.com/images/I/7170px9vGUL._AC_US600_.jpg"
    assert MODULE.canonical_amazon_image_url(left) == MODULE.canonical_amazon_image_url(right)
