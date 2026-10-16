from __future__ import annotations

import base64
import json
import os
from types import SimpleNamespace
from PIL import Image

from selection_workflow.bath_towel_deepseek import FACT_SCHEMA, FIXED_RELATION_PROMPT, MODULE_CODES, extract_visual_facts, validate_facts
from selection_workflow.bath_towel_layer3_v1 import MODULES, score_facts


def test_paid_image_identity_changes_for_same_size_same_mtime_overwrite(tmp_path):
    from selection_workflow.bath_towel_deepseek import image_digest
    image = tmp_path / "same-name.jpg"
    image.write_bytes(b"original")
    before = image.stat()
    first = image_digest(image)
    image.write_bytes(b"replaced")
    os.utime(image, ns=(before.st_atime_ns, before.st_mtime_ns))
    assert image.stat().st_size == before.st_size
    assert image.stat().st_mtime_ns == before.st_mtime_ns
    assert image_digest(image) != first


def _raw_facts(relation: str = "SAME") -> dict:
    return {
        "modules": {
            name: {
                "relation": relation,
                "amazon_evidence": "Amazon可见短证据" if relation != "UNKNOWN" else "",
                "temu_evidence": "TEMU可见短证据" if relation != "UNKNOWN" else "",
                "comparison_reason": "双边可见事实一致" if relation != "UNKNOWN" else "证据不足",
                "hard_conflict_code": "NONE",
            }
            for name in MODULES
        },
        "ignored_elements": ["浴室背景"],
        "overall_confidence": 0.9 if relation != "UNKNOWN" else 0.0,
    }


def _compact_facts(relation: str = "S") -> dict:
    return {
        "m": {
            code: {"r": relation, "a": "Amazon可见短证据" if relation != "U" else "", "t": "TEMU可见短证据" if relation != "U" else "", "h": "N"}
            for code in MODULE_CODES
        },
        "c": 0.9 if relation != "U" else 0.0,
    }


def test_bath_towel_prompt_and_schema_are_category_specific():
    assert "浴巾套装" in FIXED_RELATION_PROMPT
    assert "禁止计算分数" in FIXED_RELATION_PROMPT
    assert set(FACT_SCHEMA) == {"type", "additionalProperties", "required", "properties"}
    assert set(FACT_SCHEMA["properties"]["m"]["required"]) == set(MODULE_CODES)
    assert sum(MODULES.values()) == 100


def test_validate_and_score_normalizes_none_and_keeps_python_policy():
    facts = validate_facts(_compact_facts())
    assert facts["evidence_quality"] == "HIGH"
    scored = score_facts(facts)
    assert scored["state"] == "确认同款"
    assert scored["total_score"] == 100


def test_unknown_facts_do_not_become_a_false_difference():
    facts = validate_facts(_raw_facts("UNKNOWN"))
    scored = score_facts(facts)
    assert scored["state"] == "待补充证据"
    assert scored["hard_conflicts"] == []


def test_similar_is_not_reported_as_a_variant():
    facts = validate_facts(_raw_facts("SIMILAR"))
    assert facts["variant_differences"] == []
    assert validate_facts(_raw_facts("VARIANT"))["variant_differences"] == list(MODULES)


class _FakeResponse:
    model = "deepseek-flash"
    id = "fake-response"
    output_text = json.dumps(_compact_facts(), ensure_ascii=False)
    usage = None


class _FakeResponses:
    def create(self, **kwargs):
        assert kwargs["text"]["format"]["name"] == "bath_towel_set_visual_facts"
        assert kwargs["text"]["format"]["strict"] is True
        return _FakeResponse()


class _FakeClient:
    responses = _FakeResponses()


def test_extract_visual_facts_uses_strict_bath_schema_without_network(tmp_path, monkeypatch):
    # A tiny valid JPEG is sufficient because the fake client never sends it.
    jpeg = base64.b64decode("/9j/4AAQSkZJRgABAQAAAQABAAD/2wBDAP//////////////////////////////////////////////////////////////////////////////////////2wBDAf//////////////////////////////////////////////////////////////////////////////////////wAARCAABAAEDASIAAhEBAxEB/8QAFQABAQAAAAAAAAAAAAAAAAAAAAX/xAAUEAEAAAAAAAAAAAAAAAAAAAAA/9oADAMBAAIQAxAAAAH/AP/EABQQAQAAAAAAAAAAAAAAAAAAACD/2gAIAQEAAQUCf//EABQRAQAAAAAAAAAAAAAAAAAAACD/2gAIAQMBAT8Bf//EABQRAQAAAAAAAAAAAAAAAAAAACD/2gAIAQIBAT8Bf//Z")
    amazon = tmp_path / "amazon.jpg"
    temu = tmp_path / "temu.jpg"
    Image.new('RGB',(8,8),'white').save(amazon)
    Image.new('RGB',(8,8),'white').save(temu)
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-only")
    monkeypatch.setenv("OPENAI_VISION_MODEL", "deepseek-flash")
    result = extract_visual_facts(
        amazon_image=amazon, temu_image=temu, amazon_title="a", temu_title="b", client=_FakeClient(), max_attempts=1,
    )
    assert result["status"] == "success"
    assert result["facts"]["modules"]["surface_design_weave_border"]["relation"] == "SAME"


def test_single_invalid_json_preserves_usage_before_validation(tmp_path, monkeypatch):
    image = tmp_path / "image.jpg"
    Image.new('RGB',(8,8),'white').save(image)
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-only")
    response = SimpleNamespace(output_text="{}", id="paid-invalid-json", model="deepseek-flash",
                               usage=SimpleNamespace(input_tokens=100, output_tokens=10, total_tokens=110))
    client = SimpleNamespace(responses=SimpleNamespace(create=lambda **kwargs: response))
    events = []
    result = extract_visual_facts(amazon_image=image, temu_image=image, amazon_title="unused", temu_title="unused",
                                  client=client, request_observer=events.append)
    assert result["status"] == "failed"
    assert result["usage"]["total_tokens"] == 110
    assert result["response_id"] == "paid-invalid-json"
    assert [event["status"] for event in events] == ["request_started", "response_received"]
    assert events[-1]["usage"]["total_tokens"] == 110
