from __future__ import annotations

import importlib.util
import json
import re
import shutil
import subprocess
from argparse import Namespace
from pathlib import Path
from types import SimpleNamespace

import pytest

from selection_workflow import bath_towel_deepseek as vision
from selection_workflow.bath_towel_cache import (
    create_legacy_allowlist, normalized_visual_facts, previous_vision_key,
    refresh_candidate_identity, reusable_success,
)
from selection_workflow.bath_towel_layer3_v1 import MODULES

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def batch(monkeypatch):
    spec = importlib.util.spec_from_file_location("bath_resume_test", ROOT / "scripts/run_deepseek_bath_towel_batch12_remaining.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setenv("OPENAI_VISION_MODEL", "deepseek-flash")
    monkeypatch.setenv("OPENAI_BASE_URL", "https://api.deepseek.com")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "offline-test-only")
    return module


def facts():
    return vision.validate_facts({"m": {code: {"r": "S", "a": "visible left", "t": "visible right", "h": "N"}
                                         for code in vision.MODULE_CODES}, "c": 0.9})


def candidate(tmp_path, goods_id="G"):
    amazon, temu = tmp_path / "amazon.img", tmp_path / "temu.img"
    if not amazon.exists():
        amazon.write_bytes(b"amazon image")
        temu.write_bytes(b"temu image")
    return refresh_candidate_identity({"asin": "A", "goods_id": goods_id, "pair_revision": "pair-v1",
                                       "amazon_image_path": str(amazon), "temu_image_path": str(temu)})


def result(row):
    return {**row, "api_status": "success", "model": "deepseek-flash", "visual_facts": facts()}


def test_cache_rejects_model_images_versions_and_invalid_modules(tmp_path, batch, monkeypatch):
    row = candidate(tmp_path)
    prior = result(row)
    assert reusable_success(prior, row, manifest_path=None)
    assert not reusable_success({**prior, "model": "other"}, row, manifest_path=None)
    assert not reusable_success({**prior, "visual_facts": {"modules": dict.fromkeys(MODULES)}}, row, manifest_path=None)
    monkeypatch.setenv("OPENAI_VISION_MODEL", "gpt-5.6-terra")
    assert not reusable_success(prior, row, manifest_path=None)
    assert reusable_success(prior, row, manifest_path=None, expected_model="deepseek-flash", expected_endpoint="https://api.deepseek.com")
    monkeypatch.setenv("OPENAI_VISION_MODEL", "deepseek-flash")
    monkeypatch.setattr(vision, "PROMPT_VERSION", "future-unapproved-version")
    assert not reusable_success(prior, row, manifest_path=None)
    monkeypatch.undo()


def test_previous_paid_prompt_is_preserved_without_new_api_or_false_version_label(tmp_path, batch):
    row = candidate(tmp_path)
    prior = result(row)
    prior["vision_revision_key"] = previous_vision_key(
        row, expected_model="deepseek-flash", expected_endpoint="https://api.deepseek.com"
    )
    assert prior["vision_revision_key"] != row["vision_revision_key"]
    assert not reusable_success(prior, row, manifest_path=None,
                                expected_model="deepseek-flash", expected_endpoint="https://api.deepseek.com")
    assert reusable_success(prior, row, manifest_path=None,
                            expected_model="deepseek-flash", expected_endpoint="https://api.deepseek.com",
                            allow_previous_prompt=True)
    (tmp_path / "temu.img").write_bytes(b"changed visual input")
    assert not reusable_success(prior, row, manifest_path=None,
                                expected_model="deepseek-flash", expected_endpoint="https://api.deepseek.com",
                                allow_previous_prompt=True)


def test_image_content_and_portable_legacy_allowlist(tmp_path, batch):
    row = candidate(tmp_path)
    legacy = result(row)
    legacy["vision_revision_key"] = "known-paid-v2-key"
    legacy["visual_facts"]["modules"].update({name: {} for name in ("fiber_material", "dimension_mix", "gsm_thickness_finish")})
    manifest = tmp_path / "legacy.json"
    assert not reusable_success(legacy, row, manifest_path=manifest)
    assert create_legacy_allowlist([row], [legacy], destination=manifest)["entry_count"] == 1
    assert reusable_success(legacy, row, manifest_path=manifest, allow_previous_prompt=True)
    moved = tmp_path / "moved"
    moved.mkdir()
    for side in ("amazon", "temu"):
        shutil.copyfile(row[f"{side}_image_path"], moved / f"{side}.img")
    relocated = {**row, "amazon_image_path": str(moved / "amazon.img"), "temu_image_path": str(moved / "temu.img")}
    assert reusable_success(legacy, relocated, manifest_path=manifest, allow_previous_prompt=True)
    (moved / "temu.img").write_bytes(b"different image content")
    assert not reusable_success(legacy, relocated, manifest_path=manifest, allow_previous_prompt=True)
    assert set(normalized_visual_facts(legacy)["modules"]) == set(MODULES)
    with pytest.raises(FileExistsError):
        create_legacy_allowlist([row], [legacy], destination=manifest)


def fake_provider(batch, monkeypatch, *, malformed_first=False):
    requests = []
    def create(**kwargs):
        requests.append(kwargs)
        count = kwargs["text"]["format"]["schema"]["properties"]["candidates"]["minItems"]
        raw = {"candidates": [{"i": f"C{i:02d}", "m": {code: {"r": "S", "a": "left", "t": "right", "h": "N"}
                                                                for code in vision.MODULE_CODES}, "c": 0.9} for i in range(1, count + 1)]}
        if malformed_first and len(requests) == 1:
            raw = {}
        return SimpleNamespace(output_text=json.dumps(raw), id=f"response-{len(requests)}",
                               usage=SimpleNamespace(input_tokens=100, output_tokens=10, total_tokens=110))
    monkeypatch.setattr(batch, "client_for_thread", lambda timeout: SimpleNamespace(responses=SimpleNamespace(create=create)))
    monkeypatch.setattr(batch, "_data_url", lambda path: "data:image/jpeg;base64,OFFLINE")
    return requests


def test_malformed_paid_response_is_logged_before_splitting(tmp_path, batch, monkeypatch):
    requests = fake_provider(batch, monkeypatch, malformed_first=True)
    rows = [candidate(tmp_path, f"G{i}") for i in range(2)]
    ledger = tmp_path / "usage.jsonl"
    outcome = batch.process_batch(rows, results=tmp_path / "results.jsonl", ledger=ledger, timeout=1)
    events = list(batch.iter_jsonl(ledger))
    responses = [event for event in events if event["status"] == "response_received"]
    assert outcome["success_pairs"] == 2
    assert outcome["requests"] == len(requests) == 3
    assert sum(event["usage"]["total_tokens"] for event in responses) == 330
    assert {event["response_id"] for event in responses} == {"response-1", "response-2", "response-3"}
    assert sum(event.get("provider_requests", 0) for event in events) == 3


def test_sample_uses_shared_cache_and_plan_never_calls_provider(tmp_path, batch, monkeypatch):
    requests = fake_provider(batch, monkeypatch)
    rows = [candidate(tmp_path, f"G{i}") for i in range(14)]
    source = tmp_path / "candidates.jsonl"
    source.write_text("\n".join(json.dumps(row) for row in rows), encoding="utf-8")
    args = Namespace(candidates=source, results=tmp_path / "results.jsonl", run_dir=tmp_path / "run",
                     batch_size=12, workers=1, timeout=1, limit_pairs=13, plan_only=False)
    sample = batch.run(args)
    assert sample["success_pairs"] == 13
    args.limit_pairs = 0
    resumed = batch.run(args)
    assert resumed["pairs"] == resumed["success_pairs"] == 1
    assert resumed["cached_success"] == 13
    assert len(requests) == 3
    assert resumed["accounted_usage"]["total_tokens"] == 330
    args.plan_only = True
    monkeypatch.setattr(batch, "_api_key", lambda: pytest.fail("cache-only planning must not require an API key"))
    planned = batch.run(args)
    assert planned["would_request"] == 0
    assert planned["cached_success"] == 14
    assert len(requests) == 3


@pytest.mark.parametrize("already_running", [False, True])
def test_start_exits_before_sample_key_or_watcher_when_cached_or_running(tmp_path, already_running):
    shell = shutil.which("powershell") or shutil.which("pwsh")
    if not shell:
        pytest.skip("PowerShell is unavailable")
    script = (ROOT / "scripts/deepseek_bath_towel.ps1").read_text(encoding="utf-8-sig")
    replacements = {
        "Require-Python": "function Require-Python { }",
        "Set-DeepSeekEnvironment": "function Set-DeepSeekEnvironment { param([switch]$AllowMissingKey) if (-not $AllowMissingKey) { throw 'KEY_WOULD_BE_REQUIRED' } }",
        "Existing-RunnerProcess": "function Existing-RunnerProcess { " + ("return [pscustomobject]@{Id=42}" if already_running else "return $null") + " }",
        "Ensure-FrozenSnapshot": "function Ensure-FrozenSnapshot { }",
        "Get-RemainingPlan": "function Get-RemainingPlan { return [pscustomobject]@{remaining_pairs=0;cached_success=5853} }",
        "Ensure-Layer123Watcher": "function Ensure-Layer123Watcher { throw 'WATCHER_WOULD_START' }",
    }
    for name, replacement in replacements.items():
        script, count = re.subn(r"(?ms)^function " + name + r"\b.*?^}", lambda _: replacement, script)
        assert count == 1
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    target = scripts / "launcher.ps1"
    target.write_text(script, encoding="utf-8-sig")
    completed = subprocess.run([shell, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(target), "-Action", "Start"],
                               capture_output=True, timeout=20)
    assert completed.returncode == 0, completed.stderr.decode(errors="replace")
    output = completed.stdout.decode(errors="replace")
    assert "42" in output if already_running else '"paid_api_requests":  0' in output or '"paid_api_requests": 0' in output
