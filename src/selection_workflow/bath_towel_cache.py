"""Shared content-addressed cache and explicit paid-result migration for towels."""
from __future__ import annotations

import hashlib
import json
from functools import lru_cache
from pathlib import Path
from typing import Any, Mapping

from .bath_towel_deepseek import PREVIOUS_PROMPT_VERSION, model_name, validate_facts, vision_revision_key
from .bath_towel_layer3_v1 import MODULES

DEFAULT_MANIFEST = Path(__file__).resolve().parents[2] / "runs/bath_towel_set/stage123/legacy_paid_cache_v3.json"
MIGRATION_TARGET = "bath-towel-set-image-only-four-module-v3.0-compact"


def _digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def result_identity(row: Mapping[str, Any]) -> str:
    return _digest({name: row.get(name) for name in (
        "asin", "goods_id", "pair_revision", "vision_revision_key", "model", "visual_facts",
    )})


def normalized_visual_facts(row: Mapping[str, Any]) -> dict[str, Any]:
    facts = row.get("visual_facts") or {}
    modules = facts.get("modules") or {}
    return validate_facts({
        "modules": {name: modules[name] for name in MODULES},
        "ignored_elements": facts.get("ignored_elements", []),
        "overall_confidence": facts.get("overall_confidence"),
    })


def current_vision_key(row: Mapping[str, Any], *, expected_model: str | None = None, expected_endpoint: str | None = None) -> str:
    return vision_revision_key(amazon_image=row["amazon_image_path"], temu_image=row["temu_image_path"], pair=row,
                               model=expected_model, endpoint=expected_endpoint)


def previous_vision_key(row: Mapping[str, Any], *, expected_model: str | None = None, expected_endpoint: str | None = None) -> str:
    """Identify paid v3 facts without pretending they used the new prompt."""
    return vision_revision_key(amazon_image=row["amazon_image_path"], temu_image=row["temu_image_path"], pair=row,
                               model=expected_model, endpoint=expected_endpoint,
                               prompt_version=PREVIOUS_PROMPT_VERSION)


def refresh_candidate_identity(row: Mapping[str, Any], *, expected_model: str | None = None, expected_endpoint: str | None = None) -> dict[str, Any]:
    result = dict(row)
    raw = "|".join(str(row.get(name) or "") for name in ("asin", "goods_id", "pair_revision"))
    result["pair_id"] = hashlib.sha256(raw.encode()).hexdigest()[:32]
    result["vision_revision_key"] = current_vision_key(row, expected_model=expected_model, expected_endpoint=expected_endpoint)
    return result


@lru_cache(maxsize=8)
def _manifest(path: str, mtime_ns: int, size: int) -> dict[str, Any]:
    value = json.loads(Path(path).read_text(encoding="utf-8"))
    if value.get("schema_version") != "bath-towel-paid-legacy-allowlist-v1" or value.get("target_version") != MIGRATION_TARGET:
        return {}
    return value.get("entries") or {}


def reusable_success(prior: Mapping[str, Any], current: Mapping[str, Any], *, manifest_path: Path | None = DEFAULT_MANIFEST,
                     expected_model: str | None = None, expected_endpoint: str | None = None,
                     allow_previous_prompt: bool = False) -> bool:
    """Normal reruns use the new prompt; exports may retain already-paid facts."""
    if prior.get("api_status") != "success" or not current.get("pair_revision"):
        return False
    if any(prior.get(name) != current.get(name) for name in ("asin", "goods_id", "pair_revision")):
        return False
    if prior.get("model") != (expected_model if expected_model is not None else model_name()):
        return False
    try:
        normalized_visual_facts(prior)
        key = current_vision_key(current, expected_model=expected_model, expected_endpoint=expected_endpoint)
        if prior.get("vision_revision_key") == key:
            return True
        if not allow_previous_prompt:
            return False
        old_key = previous_vision_key(current, expected_model=expected_model, expected_endpoint=expected_endpoint)
        if prior.get("vision_revision_key") == old_key:
            return True
        if manifest_path is None or not manifest_path.is_file():
            return False
        stat = manifest_path.stat()
        entry = _manifest(str(manifest_path.resolve()), stat.st_mtime_ns, stat.st_size).get(result_identity(prior), {})
        return entry.get("target_vision_key") == old_key
    except (OSError, ValueError, TypeError, KeyError, AttributeError):
        return False


def create_legacy_allowlist(candidates: list[dict[str, Any]], results: list[dict[str, Any]], *, destination: Path = DEFAULT_MANIFEST) -> dict[str, Any]:
    """Explicit one-time offline migration; normal start/resume never enrolls results."""
    if destination.exists():
        raise FileExistsError(f"Refusing to replace paid-result migration: {destination}")
    lookup = {(row.get("asin"), row.get("goods_id"), row.get("pair_revision")): row for row in candidates}
    legacy_modules = set(MODULES) | {"fiber_material", "dimension_mix", "gsm_thickness_finish"}
    entries = {}
    for row in results:
        current = lookup.get((row.get("asin"), row.get("goods_id"), row.get("pair_revision")))
        if current is None or row.get("api_status") != "success" or row.get("model") != "deepseek-flash":
            continue
        if set((row.get("visual_facts") or {}).get("modules") or {}) != legacy_modules or not row.get("vision_revision_key"):
            continue
        normalized_visual_facts(row)
        entries[result_identity(row)] = {"pair_id": current.get("pair_id"), "legacy_vision_key": row["vision_revision_key"],
                                        "target_vision_key": previous_vision_key(current, expected_model="deepseek-flash", expected_endpoint="https://api.deepseek.com")}
    value = {"schema_version": "bath-towel-paid-legacy-allowlist-v1", "target_version": MIGRATION_TARGET, "entry_count": len(entries), "entries": entries}
    destination.parent.mkdir(parents=True, exist_ok=True)
    with destination.open("x", encoding="utf-8") as target:
        json.dump(value, target, ensure_ascii=False, indent=2)
    return {"entry_count": len(entries), "manifest": str(destination)}
