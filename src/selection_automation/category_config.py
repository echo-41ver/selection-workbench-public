from __future__ import annotations

import json
from pathlib import Path
from typing import Any


REQUIRED_STAGE3_MODULE_TOTAL = 100


def load_category(path: Path, *, compile_authored_policy: bool = False) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if value.get('visual_decision') not in (None, 'visible_core_binary_v1'):
        raise ValueError('unsupported visual decision mode')
    from .collection_contract import collection_only, validate_collection
    if collection_only(value):
        validate_collection(value)
        return value
    required = ("category_key", "category_name_zh", "amazon", "temu", "stage1", "stage2", "stage3")
    missing = [name for name in required if name not in value]
    if missing:
        raise ValueError(f"category config missing fields: {', '.join(missing)}")
    modules = value["stage3"].get("modules") or {}
    if sum(int(score) for score in modules.values()) != REQUIRED_STAGE3_MODULE_TOTAL:
        raise ValueError("stage3 module weights must total 100")
    if value["temu"].get("merge_key") != "goods_id":
        raise ValueError("TEMU snapshots must merge by goods_id")
    from .workers import category_worker_count
    category_worker_count(value)
    action = value["stage2"].get("below_threshold_action")
    allowed_provisional_actions = {"REVIEW_NOT_FINAL_DIFFERENT", "NOT_SELECTED_NOT_MANUAL_REVIEW"}
    if value["stage2"].get("threshold_status", "").startswith("PROVISIONAL") and action not in allowed_provisional_actions:
        raise ValueError("uncalibrated stage2 threshold cannot create a final different-product decision")
    # Authoring entry points accept the same raw policy as MCP. Frozen readers
    # remain strict: silently recompiling an existing run could change its rules.
    if compile_authored_policy and value["stage3"].get("identity_policy"):
        from .identity_policy import compile_policy
        value = compile_policy(value)
        value.setdefault('visual_decision', 'visible_core_binary_v1')
    if compile_authored_policy:
        from .stage1_authoring import compile_stage1_rules
        value=compile_stage1_rules(value)
    from .rules import validate
    validate(value)
    from .commercial import settings
    settings(value.get('commercial'))
    if isinstance(value['stage3'].get('provider'),dict):
        from .stage3_provider import settings as provider_settings
        provider_settings(value['stage3']['provider'])
    return value
