"""First-layer text gate for the *bath towel set* category.

This gate is deliberately identity-focused.  A product is allowed to proceed
when the text is missing or ambiguous, but an explicit product-type mismatch
is excluded before downloading images.  A mixed set that contains bath towels
plus hand towels/washcloths is a bath-towel set; a hand-towel or washcloth-only
listing is not.  Explicit total piece counts are identity facts: when both
listings state their available totals and the two sets do not overlap, the pair
is excluded.  Colour, dimensions and GSM remain variants.
"""
from __future__ import annotations

import re
from typing import Any


def _text(value: str) -> str:
    return re.sub(r"\s+", " ", str(value or "").lower()).strip()


def _one(text: str, mapping: dict[str, str]) -> str | None:
    from selection_automation.rules import uncertain_alias_context, related_product_alias_context
    hits=set()
    for pattern,label in mapping.items():
        for match in re.finditer(pattern,text,re.I):
            if related_product_alias_context(text,match.start(),match.end()):
                continue
            if uncertain_alias_context(text,match.start(),match.end()):
                return None
            hits.add(label)
    return next(iter(hits)) if len(hits) == 1 else None


FIBERS = {
    r"\b(?:100\s*%\s*)?(?:turkish |egyptian |pima |ring[ -]?spun )?cotton\b|纯棉|全棉": "COTTON",
    r"\bmicrofiber\b|超细纤维": "MICROFIBER",
    r"\bpolyester\b|涤纶": "POLYESTER",
    r"\b(?:bamboo|viscose from bamboo|bamboo viscose|rayon from bamboo)\b|竹纤维|竹浆粘纤": "BAMBOO_DERIVED",
    # Do not treat the product-class phrase ``bath linen`` or brand names
    # such as ``Lane Linen`` as a material claim.  Require a material context
    # for English ``linen``; Chinese 亚麻 remains an explicit material term.
    r"(?:\b(?:100\s*%\s*)?linen\b(?=\s+(?:fabric|fibre|fiber|weave|waffle|towel|sheet))|\b(?:made|woven|spun)\s+(?:of|from)\s+linen\b|\blinen\s+(?:fabric|fibre|fiber|weave|waffle)\b)|亚麻": "LINEN",
    r"\b(?:viscose|rayon|modal|lyocell|tencel)\b|粘胶|人造丝|莫代尔|莱赛尔": "VISCOSE_RAYON",
    r"\b(?:polyamide|nylon)\b|聚酰胺|锦纶": "POLYAMIDE",
    r"\b(?:coral\s+(?:fleece|velvet)|fleece\s+towel)\b|珊瑚绒": "SYNTHETIC_FLEECE",
}

WEAVES = {
    r"\bwaffle(?: weave)?\b|蜂窝|华夫格": "WAFFLE",
    r"\bterry(?: cloth)?\b|毛圈": "TERRY",
    r"\bjacquard\b|提花": "JACQUARD",
    r"\bvelour\b|剪绒": "VELOUR",
}

SUBTYPES = {
    r"\bhooded\b.*\btowel\b|连帽浴巾": "HOODED_TOWEL",
    r"\bbeach\s+towels?\b|沙滩巾": "BEACH_TOWEL",
    r"\bbath\s*mat\b|浴室地垫|吸水垫": "BATH_MAT",
    r"\bbathrobe\b|浴袍": "BATHROBE",
}

PATTERNS = {
    r"\bsolid(?: color)?\b|\bplain\b|纯色|素色": "SOLID",
    r"\bstrip(?:e|ed)\b|条纹": "STRIPED",
    r"\bfloral\b|花卉|印花": "FLORAL",
    r"\bgeometric\b|几何": "GEOMETRIC",
}


# Product-type terms that are useful for a bath-towel-set category gate.  The
# distinction is based on the *product being sold*, not every use case word in
# a long bullet list.  ``bath_component`` is evaluated first so a valid mixed
# set (bath towels + hand towels + washcloths) remains a set.
BATH_COMPONENT = re.compile(
    r"\bbath\s+(?:towels?|sheets?|linen(?:\s+sets?)?|wrap(?:\s+towels?)?)\b"
    r"|\b\d+\s*(?:pc|pcs|piece|pieces)?\s*bath\s+towels?\b"
    r"|浴巾|浴巾套装|浴巾组",
    re.I,
)
SET_SIGNAL = re.compile(
    r"\b(?:set|pack|bundle|assort(?:ed|ment)?|collection)\b"
    r"|\b\d+\s*(?:pc|pcs|piece|pieces|pack|pk)\b"
    r"|\b\d+\s*(?:count|ct)\b"
    r"|套装|套|组合|件",
    re.I,
)
MIXED_COMPONENT = re.compile(
    r"\b(?:hand\s+towels?|washcloths?|wash\s+cloths?|face\s+towels?|fingertip\s+towels?)\b"
    r"|手巾|方巾|面巾|洗脸巾",
    re.I,
)
EXCLUSIVE_NON_TARGET = (
    ("BATH_SKIRT_OR_WRAP", re.compile(r"\bbath\s+(?:skirt|wrap)\b|\bdry\s+hair\s+(?:cap|towel)\b|浴裙|干发帽", re.I)),
    ("HAIR_TOWEL", re.compile(r"\bhair\s+towel\b|\bhair\s+turban\b|干发巾", re.I)),
    ("CLEANING_CLOTH", re.compile(r"\b(?:cleaning|dish|scrub|dusting)\s+cloths?\b|清洁布|洗碗布|抹布", re.I)),
    ("KITCHEN_TOWEL", re.compile(r"\b(?:kitchen|tea)\s+towels?\b|厨房巾|茶巾", re.I)),
)
NON_TARGET_ONLY = (
    ("HAND_TOWEL_ONLY", re.compile(r"\bhand\s+towels?\b|手巾", re.I)),
    ("WASHCLOTH_ONLY", re.compile(r"\bwash\s*cloths?\b|\bface\s+towels?\b|方巾|洗脸巾|面巾", re.I)),
    ("BEACH_TOWEL", re.compile(r"\bbeach\s+towels?\b|沙滩巾", re.I)),
    ("BATH_MAT", re.compile(r"\bbath\s+mat\b|浴室地垫|吸水垫", re.I)),
    ("BATHROBE", re.compile(r"\bbathrobe\b|浴袍", re.I)),
)


TOTAL_PIECE_SIGNAL = re.compile(
    r"(?<!\d)(\d{1,3}(?:\s*(?:/|--?|–|—|or)\s*\d{1,3})*)\s*[- ]?"
    r"(?:pc|pcs|piece|pieces|count|ct|pack)\b",
    re.I,
)
SPACE_SEPARATED_PIECE_OPTIONS = re.compile(
    r"(?<!\d)(\d{1,3})\s+(\d{1,3})\s*(?:pc|pcs|piece|pieces)\b",
    re.I,
)
PACK_OF_SIGNAL = re.compile(r"\b(?:set|pack|bundle)\s+of\s+(\d{1,3})\b", re.I)
CHINESE_PIECE_SIGNAL = re.compile(r"(?<!\d)(\d{1,3})\s*件(?:套|装)?")
COMPONENT_AFTER_COUNT = re.compile(
    # TEMU's visible-card title cleaning can turn ``Bath Towels`` into
    # ``bath s``.  The preceding advertised total is still authoritative.
    r"^\s*(?:bath|hand|face|fingertip)\s+(?:towels?|cloths?|s)\b"
    r"|^\s*wash\s*cloths?\b",
    re.I,
)
SET_AFTER_COUNT = re.compile(
    r"^\s*(?:bath(?:room)?\s+)?(?:towels?|cloths?)\s+(?:set|combination|bundle|collection)\b",
    re.I,
)
COMPOSITION_LEAD_IN = re.compile(r"\b(?:include|includes|including|contains|comprises|with)\b", re.I)


def _extract_total_piece_counts(text: str) -> list[int] | None:
    """Return explicitly advertised total set sizes, never inferred counts.

    Marketplace cards commonly expose option families such as ``12/24pcs``
    or flattened text such as ``16 8pcs``.  Component quantities such as
    ``4/2pcs bath towels`` are not total set sizes and are ignored here.  The
    gate intentionally does not add component quantities because partial or
    truncated marketplace text could turn that inference into a false hard
    exclusion.
    """
    value = _identity_text(text)
    counts: set[int] = set()

    def add(raw: str) -> None:
        for token in re.findall(r"\d{1,3}", raw):
            number = int(token)
            if 1 <= number <= 200:
                counts.add(number)

    for match in TOTAL_PIECE_SIGNAL.finditer(value):
        tail = value[match.end():match.end() + 48]
        if COMPONENT_AFTER_COUNT.search(tail) and not SET_AFTER_COUNT.search(tail):
            # ``12/24pcs Bath Towel`` at the start of a listing advertises
            # total options. ``including 4/8pcs Bath Towels`` lists one
            # component and must not add 4 or 8 as total options.
            segment = value[max(value.rfind(" | ", 0, match.start()) + 3, 0):match.start()]
            if COMPOSITION_LEAD_IN.search(segment) or TOTAL_PIECE_SIGNAL.search(segment):
                continue
        add(match.group(1))
    for match in SPACE_SEPARATED_PIECE_OPTIONS.finditer(value):
        tail = value[match.end():match.end() + 48]
        if COMPONENT_AFTER_COUNT.search(tail) and not SET_AFTER_COUNT.search(tail):
            segment = value[max(value.rfind(" | ", 0, match.start()) + 3, 0):match.start()]
            if COMPOSITION_LEAD_IN.search(segment) or TOTAL_PIECE_SIGNAL.search(segment):
                continue
        add(match.group(1))
        add(match.group(2))
    for pattern in (PACK_OF_SIGNAL, CHINESE_PIECE_SIGNAL):
        for match in pattern.finditer(value):
            add(match.group(1))
    return sorted(counts) or None


def _has_bath_component(text: str) -> bool:
    if BATH_COMPONENT.search(text):
        return True
    # Marketplace translations often shorten ``2 bath towels`` to ``2 bath``
    # or ``2 bathroom towels``.  Accept those forms only when the same record
    # also looks like a set/mixed composition; ``bathroom`` by itself is not a
    # bath-towel identity signal.
    if re.search(r"\b\d+\s+bath\b", text, re.I) and (
        SET_SIGNAL.search(text) or MIXED_COMPONENT.search(text)
    ):
        return True
    if re.search(r"\bbathroom\s+towels?\b", text, re.I) and SET_SIGNAL.search(text) and not re.search(
        r"\b(?:holder|rack|warmer|hook)s?\b", text, re.I
    ):
        return True
    return False


def _identity_text(text: str) -> str:
    """Return title/card identity text, excluding long use-case bullets.

    A bullet can mention ``hair`` or ``kitchen`` as an occasional use for a
    real bath-towel set.  Product-type exclusion terms therefore come from the
    title and visible card text first, while composition/material evidence can
    still use the full gate text.
    """
    value = _text(text)
    segments = re.split(r"\s+\|\s+(?=[a-z_]+:)", value)
    identity = [
        segment.split(":", 1)[1]
        for segment in segments
        if segment.startswith("title:") or segment.startswith("card_text:")
    ]
    return " | ".join(identity) if identity else value[:1200]


def _extract_subtype(text: str) -> str | None:
    """Extract a product identity, keeping ambiguous text as unknown.

    Explicit bath-skirt/hair/cleaning products take precedence over incidental
    wording such as ``wrapped bath towel``.  A listing with a bath-towel
    component and a set/pack signal (or multiple towel components) is treated
    as ``BATH_TOWEL_SET``.  Missing pack wording does not prove that the listing
    sells a single towel; leave that identity dimension unknown.
    """
    value = _text(text)
    identity = _identity_text(value)
    for label, pattern in EXCLUSIVE_NON_TARGET:
        if pattern.search(identity):
            return label
    if _has_bath_component(value):
        if SET_SIGNAL.search(identity) or MIXED_COMPONENT.search(identity):
            return "BATH_TOWEL_SET"
        return None
    for label, pattern in NON_TARGET_ONLY:
        if pattern.search(identity):
            return label
    if re.search(r"\bhooded\b.*\btowel\b|连帽浴巾", identity, re.I):
        return "HOODED_TOWEL"
    if re.search(r"\bbeach\s+towels?\b|沙滩巾", identity, re.I):
        return "BEACH_TOWEL"
    if re.search(r"\btowels?\s+set\b|\bset\s+of\s+\d+\s+towels?\b|毛巾套装", identity, re.I):
        return "GENERIC_TOWEL_SET"
    return None


def _is_target_set(value: str | None) -> bool:
    return value == "BATH_TOWEL_SET"


def _is_explicit_non_target(value: str | None) -> bool:
    return bool(value and value not in {"BATH_TOWEL_SET", "BATH_TOWEL_SINGLE", "GENERIC_TOWEL_SET"})


def evaluate_text_gate(amazon_text: str, temu_text: str) -> dict[str, Any]:
    left, right = _text(amazon_text), _text(temu_text)
    conflicts: list[dict[str, str]] = []
    facts: dict[str, dict[str, str | None]] = {}

    def add_conflict(code: str, dimension: str, reason: str) -> None:
        conflicts.append({"code": code, "dimension": dimension, "reason": reason})

    def compare(name: str, mapping: dict[str, str], code: str) -> None:
        a, t = _one(left, mapping), _one(right, mapping)
        facts[name] = {"amazon": a, "temu": t}
        if a and t and a != t:
            add_conflict(code, name, f"双边文字明确显示不同：Amazon={a}，TEMU={t}")

    amazon_subtype, temu_subtype = _extract_subtype(left), _extract_subtype(right)
    facts["product_subtype"] = {"amazon": amazon_subtype, "temu": temu_subtype}
    if _is_target_set(amazon_subtype) and _is_explicit_non_target(temu_subtype):
        add_conflict(
            "NON_TARGET_TOWEL_PRODUCT",
            "product_subtype",
            f"TEMU明确为非浴巾套装商品（{temu_subtype}），Amazon为浴巾套装",
        )
    elif _is_target_set(temu_subtype) and _is_explicit_non_target(amazon_subtype):
        add_conflict(
            "NON_TARGET_TOWEL_PRODUCT",
            "product_subtype",
            f"Amazon明确为非浴巾套装商品（{amazon_subtype}），TEMU为浴巾套装",
        )
    elif (
        amazon_subtype
        and temu_subtype
        and amazon_subtype != temu_subtype
        # A bare "towel set" is not an affirmative non-target claim.  Keep
        # it as an image-layer candidate instead of treating the missing
        # bath-towel qualifier as proof that it is a different product.
        and "GENERIC_TOWEL_SET" not in {amazon_subtype, temu_subtype}
    ):
        add_conflict(
            "DIFFERENT_TOWEL_PRODUCT_SUBTYPE",
            "product_subtype",
            f"双边文字明确显示不同：Amazon={amazon_subtype}，TEMU={temu_subtype}",
        )

    compare("fiber_family", FIBERS, "DIFFERENT_EXPLICIT_FIBER_FAMILY")
    compare("weave_structure", WEAVES, "DIFFERENT_EXPLICIT_WEAVE")
    compare("pattern_family", PATTERNS, "DIFFERENT_EXPLICIT_PATTERN")

    amazon_piece_counts = _extract_total_piece_counts(left)
    temu_piece_counts = _extract_total_piece_counts(right)
    facts["set_piece_count"] = {
        "amazon": amazon_piece_counts,
        "temu": temu_piece_counts,
    }
    if (
        amazon_piece_counts
        and temu_piece_counts
        and set(amazon_piece_counts).isdisjoint(temu_piece_counts)
    ):
        add_conflict(
            "DIFFERENT_EXPLICIT_SET_PIECE_COUNT",
            "set_piece_count",
            "双边文字明确显示套装总件数无交集："
            f"Amazon={amazon_piece_counts}，TEMU={temu_piece_counts}",
        )

    # ``TEXT_CLEAR`` means that at least two dimensions are explicitly known
    # on both sides.  A pair with one-sided facts is not clear; it is merely
    # insufficient and must rely on the image layer.
    bilateral_dimensions = sum(
        pair.get("amazon") is not None and pair.get("temu") is not None
        for pair in facts.values()
    )
    status = "TEXT_HARD_EXCLUDE" if conflicts else (
        "TEXT_CLEAR" if bilateral_dimensions >= 2 else "TEXT_INSUFFICIENT"
    )
    return {
        "stage1_status": status,
        "advance_to_image_vector": status != "TEXT_HARD_EXCLUDE",
        "hard_conflicts": conflicts,
        "facts": facts,
        "bilateral_fact_dimensions": bilateral_dimensions,
        "allowed_variants": ["color", "dimensions", "gsm", "brand", "packaging"],
        "reason": "存在双边明确的浴巾套装身份冲突" if conflicts else (
            "未发现双边文字硬冲突" if status == "TEXT_CLEAR" else "文字事实不足，进入图片向量层"
        ),
    }
