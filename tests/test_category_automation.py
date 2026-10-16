import json
from pathlib import Path

from selection_automation.category_config import load_category
from selection_automation.keyword_planner import build_plan, build_product_groups, extract_queries
from selection_automation.merge_temu_snapshots import merge
from selection_workflow.bath_towel_layer3_v1 import MODULES, score_facts
from selection_workflow.bath_towel_text_gate_v1 import evaluate_text_gate

ROOT = Path(__file__).resolve().parents[1]


def test_proxy_policy_requires_new_environment_and_new_session():
    policy = json.loads((ROOT / "config" / "proxy_policy.json").read_text(encoding="utf-8"))
    assert policy["architecture"] == "ADSPOWER_TO_LOCAL_HTTP_CHAIN_TO_LOCAL_SOCKS5_TO_PROXYSHARE_TO_TEMU"
    assert policy["direct_proxyshare_in_adspower_forbidden"] is True
    assert policy["session_rotation_policy"] == "NEW_ADSPOWER_PROFILE_AND_NEW_PROXY_SESSION"
    assert policy["reuse_old_profile_on_session_change"] is False
    assert policy["delete_old_profile_first_via_adspower"] is True
    assert policy["old_profile_deletion_is_permanent"] is True
    assert policy["replacement_speed_target_seconds"] == 60
    assert policy["worker_chain_ports"] == [17891, 17892, 17893, 17894]


def test_public_session_replacement_script_enforces_full_replacement():
    script = (ROOT / "scripts" / "replace_proxyshare_worker_environment.py").read_text(encoding="utf-8")
    assert "prepare-replacement" in script
    assert "/api/v2/browser-profile/create" in script
    assert "/api/v2/browser-profile/delete" in script
    assert "new_proxy_session" in script
    assert "/api/v1/browser/stop" in script
    assert script.index("/api/v2/browser-profile/delete") < script.index("/api/v2/browser-profile/create")
    assert "delete_profile_after_release" in script
    assert "time.sleep(1)" in script
    assert "stdout=subprocess.DEVNULL" in script
    assert "subprocess.DETACHED_PROCESS" in script


def test_first_computer_bootstrap_creates_capacity_adapted_chained_profiles():
    script = (ROOT / "scripts" / "setup_proxyshare_adspower_workers.py").read_text(encoding="utf-8")
    assert "proxyshare_chain_manager.ps1" in script
    assert "range(1, target_count + 1)" in script
    assert '"proxy_host": "127.0.0.1"' in script
    assert "worker_chain_ports" in script
    assert "/api/v2/browser-profile/create" in script
    assert "session_rotation_policy" in script


def test_parallel_replacement_has_manifest_lock_and_nonfatal_speed_target():
    single = (ROOT / "scripts" / "replace_proxyshare_worker_environment.py").read_text(encoding="utf-8")
    batch = (ROOT / "scripts" / "replace_proxyshare_workers_batch.py").read_text(encoding="utf-8")
    assert "guard = ProcessLock(lock)" in single
    assert "state-worker-{args.worker}.json" in single
    assert "ThreadPoolExecutor" in batch
    assert '"speed_target_seconds": 60' in batch
    assert "elapsed < 60" in batch
    assert "raise SystemExit(3)" not in batch
    assert "timeout=58" not in batch


def test_all_three_continuous_excel_publishers_are_portable():
    watcher = (ROOT / "scripts" / "watch_excel_output.py").read_text(encoding="utf-8")
    launcher = (ROOT / "scripts" / "start_excel_watchers.ps1").read_text(encoding="utf-8")
    assert all(f'"{kind}"' in watcher for kind in ("temu", "layer12", "layer123"))
    assert "export_temu_union_excel.py" in watcher
    assert "run_incremental_stage12.py" in watcher
    assert "export_incremental_layer123_excel.py" in watcher
    assert "validate_temu_union_excel.py" in watcher
    assert "validate_incremental_layer123_excel.py" in watcher
    assert all(kind in launcher for kind in ("temu", "layer12", "layer123"))


def relation(value, left="Amazon证据", right="TEMU证据", code=None):
    return {"relation": value, "amazon_evidence": left, "temu_evidence": right, "reason": "对比", "hard_conflict_code": code}


def test_bath_towel_config_is_explicitly_uncalibrated():
    config = load_category(ROOT / "config" / "categories" / "bath_towel_set.json")
    assert config["amazon"]["target_count"] == 30
    assert config["amazon"]["bestsellers_url"].endswith("/3731761")
    assert config["stage2"]["below_threshold_action"] == "NOT_SELECTED_NOT_MANUAL_REVIEW"
    assert sum(config["stage3"]["modules"].values()) == 100


def test_keyword_plan_is_traceable_to_asins():
    rows = [
        {"asin": "A1", "title": "6 Piece 100% Turkish Cotton Waffle Bath Towel Set"},
        {"asin": "A2", "title": "8 Piece Cotton Striped Bath Towel Set"},
    ]
    plan = build_plan(rows, "bath towel set", 12, 500)
    assert plan[0]["query"] == "bath towel set"
    assert plan[0]["source_asins"] == ["A1", "A2"]
    assert any(item["query"] == "turkish cotton bath towel set" for item in plan)
    assert all(item["max_products"] == 500 for item in plan)
    assert all("source_evidence" in item for item in plan)


def test_keyword_extraction_keeps_turkish_oeko_and_piece_identity():
    row = {
        "asin": "A1",
        "rank": 1,
        "title": "American Soft Linen 100% Cotton Luxury Turkish Towels, 4 PC Bath Towel Set OEKO Tex Standard 100",
    }
    queries = extract_queries(row, "bath towel set")
    assert "turkish cotton oeko tex bath towel set" in queries
    assert "oeko tex bath towel set" in queries
    assert "4 piece turkish cotton bath towel set" in queries
    groups = build_product_groups([row], "bath towel set", 6)
    assert groups[0]["asin"] == "A1"
    assert len(groups[0]["queries"]) >= 5


def test_merge_keeps_distinct_ids_and_query_provenance(tmp_path):
    left = tmp_path / "a" / "products.jsonl"; right = tmp_path / "b" / "products.jsonl"
    left.parent.mkdir(); right.parent.mkdir()
    left.write_text(json.dumps({"goods_id": "1", "title": "x", "search_query": "cotton towel", "all_image_urls": ["a"]}) + "\n", encoding="utf-8")
    right.write_text("".join(json.dumps(row) + "\n" for row in [
        {"goods_id": "1", "title": "x2", "search_query": "waffle towel", "all_image_urls": ["b"]},
        {"goods_id": "2", "title": "x", "search_query": "waffle towel", "all_image_urls": ["c"]},
    ]), encoding="utf-8")
    rows, summary = merge([left, right])
    assert len(rows) == 2 and summary["duplicate_rows_merged"] == 1
    assert rows[0]["source_queries"] == ["cotton towel", "waffle towel"]


def test_text_gate_only_excludes_bilateral_explicit_conflicts():
    result = evaluate_text_gate("100% cotton terry bath towel set", "microfiber waffle bath towel set")
    assert result["stage1_status"] == "TEXT_HARD_EXCLUDE"
    uncertain = evaluate_text_gate("soft premium towel set", "absorbent towel set")
    assert uncertain["stage1_status"] == "TEXT_INSUFFICIENT"


def test_bath_towel_set_gate_keeps_mixed_sets_but_excludes_non_target_only():
    mixed = evaluate_text_gate(
        "6-piece cotton bath towel set with 2 hand towels and 2 washcloths",
        "6pcs bath towel set 2 bath towels 2 hand towels 2 washcloths 100% cotton",
    )
    assert mixed["stage1_status"] != "TEXT_HARD_EXCLUDE"
    assert mixed["facts"]["product_subtype"] == {
        "amazon": "BATH_TOWEL_SET",
        "temu": "BATH_TOWEL_SET",
    }

    hand_only = evaluate_text_gate(
        "100% cotton bath towel set with 2 bath towels and 2 hand towels",
        "3 pack premium hand towels for bathroom and kitchen",
    )
    assert hand_only["stage1_status"] == "TEXT_HARD_EXCLUDE"
    assert any(c["code"] == "NON_TARGET_TOWEL_PRODUCT" for c in hand_only["hard_conflicts"])

    translated = evaluate_text_gate(
        "American cotton bath towel set",
        "6pcs bathroom towel set, 2 bath towels and 4 hand towels",
    )
    assert translated["facts"]["product_subtype"] == {
        "amazon": "BATH_TOWEL_SET",
        "temu": "BATH_TOWEL_SET",
    }


def test_linen_brand_or_bath_linen_is_not_material_conflict():
    result = evaluate_text_gate(
        "LANE LINEN 100% cotton bath towel set",
        "4pcs household bath linen set soft absorbent towels",
    )
    assert result["facts"]["fiber_family"]["amazon"] == "COTTON"
    assert result["facts"]["fiber_family"]["temu"] is None
    assert not any(c["code"] == "DIFFERENT_EXPLICIT_FIBER_FAMILY" for c in result["hard_conflicts"])


def test_layer3_requires_relation_different_for_hard_conflict():
    facts = {name: relation("SAME") for name in MODULES}
    facts["surface_design_weave_border"] = relation("UNKNOWN", code="DIFFERENT_WEAVE_OR_SURFACE_DESIGN")
    result = score_facts({"modules": facts, "evidence_quality": "HIGH"})
    assert result["state"] == "待补充证据"
    assert result["hard_conflicts"] == []


def test_layer3_confirms_strong_same_with_variants():
    facts = {name: relation("SAME") for name in MODULES}
    facts["color_pattern_variant"] = relation("VARIANT")
    facts["color_pattern_variant"] = relation("VARIANT")
    result = score_facts({"modules": facts, "overall_confidence": 0.9, "evidence_quality": "HIGH", "variant_differences": ["尺寸和颜色"]})
    assert result["state"] == "同款（存在规格变体）"
    assert result["total_score"] >= 88


def test_temu_scroll_restores_learned_zone_with_live_control_fallback():
    source = (ROOT / "scripts" / "temu_flower_collect.mjs").read_text(encoding="utf-8")
    assert "let before = await visibleScrollToLearnedSeeMoreZone()" in source
    assert "let element = await findSeeMoreFromCurrentDom()" in source
    assert "element = await revealSeeMoreAboveFooter()" in source
    assert "usedLearnedOffset ?" not in source
    live_probe = source.split("async function findSeeMoreFromCurrentDom()", 1)[1].split(
        "async function", 1
    )[0]
    assert "findVisibleSeeMore" in live_probe
    assert "getBoundingClientRect" in live_probe
    assert "scrollIntoView" not in live_probe
    assert "enforceConfiguredProductLimit();" in source
    assert "ordered.slice(MAX_PRODUCTS)" in source


def test_amazon_bestsellers_collector_uses_latest_detail_standard():
    source = (ROOT / "scripts" / "collect_amazon_search_frontend.mjs").read_text(encoding="utf-8")
    assert "amazon-bestsellers-frontend-v1" in source
    assert "LATEST_DETAIL_V2" in source
    assert "best_sellers_rank" in source
    assert "image_width_hint" in source
    assert "detail_image_attempts" in source
