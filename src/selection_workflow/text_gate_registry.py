from __future__ import annotations

from typing import Any

from .bath_towel_text_gate_v1 import evaluate_text_gate as bath_towel_set_v5
from .shower_text_gate_v2 import evaluate_text_gate as shower_curtain_v2

GATES = {
    # The module filename remains v1 for backwards-compatible imports, while
    # the registered production contract is v5 (set-only subtype gate,
    # contextual material parsing and OCR-flattened composition exclusion).
    "bath_towel_set_v5": bath_towel_set_v5,
    "bath_towel_set_v4": bath_towel_set_v5,
    "bath_towel_set_v3": bath_towel_set_v5,
    "bath_towel_set_v2": bath_towel_set_v5,
    "bath_towel_set_v1": bath_towel_set_v5,
    "shower_curtain_v2": shower_curtain_v2,
}


def evaluate(name: str, amazon_text: str, temu_text: str) -> dict[str, Any]:
    if name.startswith("declarative:"):
        from selection_automation.rules import read_frozen, text_gate
        return text_gate(read_frozen(name.split(":", 1)[1]), amazon_text, temu_text)
    try:
        gate = GATES[name]
    except KeyError as exc:
        raise ValueError(f"unknown text gate: {name}") from exc
    return gate(amazon_text, temu_text)
