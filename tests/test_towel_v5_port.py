from selection_workflow.bath_towel_text_gate_v1 import evaluate_text_gate

def test_bath_towel_set_gate_excludes_disjoint_explicit_total_piece_options():
    result = evaluate_text_gate(
        "100% Cotton Bath Towels Set Soft Absorbent Quick Dry 16 Piece - Grey",
        "Luxury 12/24pcs Bath Towel Combination, 4 Bath Towels, 6 Hand Towels and 2 Face Towels",
    )
    assert result["stage1_status"] == "TEXT_HARD_EXCLUDE"
    assert result["facts"]["set_piece_count"] == {"amazon": [16], "temu": [12, 24]}
    assert any(c["code"] == "DIFFERENT_EXPLICIT_SET_PIECE_COUNT" for c in result["hard_conflicts"])

    translated = evaluate_text_gate(
        "16 Piece Bath Towel Set",
        "12/24pcs Bath Cloths Set, 4 Bath Cloths, 2 Hand and 6 Washcloths",
    )
    assert translated["stage1_status"] == "TEXT_HARD_EXCLUDE"
    assert translated["facts"]["set_piece_count"] == {"amazon": [16], "temu": [12, 24]}


def test_bath_towel_set_gate_keeps_overlapping_piece_options_and_unknown_counts():
    overlapping = evaluate_text_gate(
        "16 Piece Bath Towel Set",
        "16/8pcs Luxury Soft Towel Set, 4/2pcs Bath Towels, 4/2pcs Hand Towels, 8/4pcs Face Towels",
    )
    assert overlapping["stage1_status"] != "TEXT_HARD_EXCLUDE"
    assert overlapping["facts"]["set_piece_count"]["amazon"] == [16]
    assert 16 in overlapping["facts"]["set_piece_count"]["temu"]

    unknown = evaluate_text_gate(
        "Premium Cotton Bath Towel Set",
        "6pcs Cotton Bath Towel Set",
    )
    assert unknown["stage1_status"] != "TEXT_HARD_EXCLUDE"
    assert unknown["facts"]["set_piece_count"] == {"amazon": None, "temu": [6]}


def test_bath_towel_set_gate_understands_pack_of_total():
    result = evaluate_text_gate(
        "16 Piece Bath Towel Set",
        "Bath Sheets Pack of 3, oversized cotton towels",
    )
    assert result["stage1_status"] == "TEXT_HARD_EXCLUDE"
    assert result["facts"]["set_piece_count"] == {"amazon": [16], "temu": [3]}


def test_bath_towel_set_gate_reads_bath_towel_total_before_component_breakdown():
    result = evaluate_text_gate(
        "16 Piece Bath Towel Set",
        "title:12 24pcs bath towel super absorbent fast dry including 4 8 bath towels 2 4 hand towels and 6 12 face towels | card_text:12/24pcs Bath Towel | Super Absorbent, Fast Dry | Including 4/8 Bath Towels, 2/4 Hand Towels And 6/12 Face Towels",
    )
    assert result["stage1_status"] == "TEXT_HARD_EXCLUDE"
    assert result["facts"]["set_piece_count"] == {"amazon": [16], "temu": [12, 24]}

    four_piece = evaluate_text_gate(
        "16 Piece Bath Towel Set",
        "title:item picture chakir linens quality cotton premium towels soft absorbent 4pcs bath towels gray | card_text:Chakir Turkish Linens | Hotel & Spa Quality Cotton Premium Turkish Towels | Soft & Absorbent (4pcs Bath Towels, Gray)",
    )
    assert four_piece["stage1_status"] == "TEXT_HARD_EXCLUDE"
    assert four_piece["facts"]["set_piece_count"] == {"amazon": [16], "temu": [4]}


def test_bath_towel_set_gate_reads_hyphenated_options_without_component_leakage():
    overlapping = evaluate_text_gate(
        "16 Piece Bath Towel Set",
        "16--4pcs Bath Towel Set including 4/2pcs Bath Towels and 2/2pcs Hand Towels",
    )
    assert overlapping["stage1_status"] != "TEXT_HARD_EXCLUDE"
    assert overlapping["facts"]["set_piece_count"] == {"amazon": [16], "temu": [4, 16]}

    disjoint = evaluate_text_gate(
        "16 Piece Bath Towel Set",
        "12--24pcs Bath Towel Set including 4/8pcs Bath Towels",
    )
    assert disjoint["stage1_status"] == "TEXT_HARD_EXCLUDE"
    assert disjoint["facts"]["set_piece_count"] == {"amazon": [16], "temu": [12, 24]}


def test_bath_towel_set_gate_ignores_flattened_component_quantities():
    result = evaluate_text_gate(
        "Amazon Basics 6-Piece 100% Cotton Towel Set, 2 Bath Towels, 2 Hand Towels, 2 Washcloths",
        "title:item picture 16 8pcs luxury soft set 4 2pcs bath s 4 2pcs hand s 8 4pcs face s | "
        "card_text:16/8pcs Luxury Soft Towel Set, 4/2pcs Bath Towels, "
        "4/2pcs Hand Towels, 8/4pcs Face Towels",
    )
    assert result["stage1_status"] == "TEXT_HARD_EXCLUDE"
    assert result["facts"]["set_piece_count"] == {"amazon": [6], "temu": [8, 16]}
