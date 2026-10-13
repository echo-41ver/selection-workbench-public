"""Portable adaptations of the recovered upstream fixes; no external requests."""
import importlib.util
import json
import os
import subprocess
import threading
from pathlib import Path

import numpy as np
import pytest

from selection_workflow.incremental_stage12 import IncrementalStage12, replace_snapshot_file

ROOT = Path(__file__).resolve().parents[1]


def pipeline_script():
    spec = importlib.util.spec_from_file_location("portable_pipeline_repairs", ROOT / "scripts/pipeline.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("output", ["C:fixture", "\\fixture", "..\\fixture", "../fixture", "C:\\fixture"])
def test_stage12_windows_paths_cannot_escape_run_directory(tmp_path, monkeypatch, output):
    module = pipeline_script()
    monkeypatch.setattr(module, "ROOT", tmp_path)
    category = json.loads((ROOT / "config/categories/power_drill.json").read_text("utf-8"))
    category["stage12_output_dir"] = output
    with pytest.raises(ValueError, match="inside|outside"):
        module.write_stage12_config(category)


def test_stage3_revision_does_not_invalidate_stage12_config(tmp_path, monkeypatch):
    module = pipeline_script()
    monkeypatch.setattr(module, "ROOT", tmp_path)
    category = json.loads((ROOT / "config/categories/power_drill.json").read_text("utf-8"))
    before = json.loads(module.write_stage12_config(category).read_text("utf-8"))
    category["stage3"]["rule_version"] = "new-visual-only-rule"
    after = json.loads(module.write_stage12_config(category).read_text("utf-8"))
    assert before == after


def test_declarative_gate_contract_tracks_only_stage1_config(tmp_path):
    class Vectorizer:
        revision = "fixture-vector"
        def encode(self, _): return np.ones(2, dtype=np.float32)

    category = json.loads((ROOT / "config/categories/power_drill.json").read_text("utf-8"))
    path = tmp_path / "category.json"
    def contract():
        path.write_text(json.dumps(category), encoding="utf-8")
        pipe = IncrementalStage12(tmp_path / "stage12", vectorizer=Vectorizer(),
                                  text_gate_name=f"declarative:{path}", stage12_rule_version="text-v1")
        try: return pipe.ensure_processing_contract()
        finally: pipe.close()
    assert contract()["changed"]
    category["stage3"]["rule_version"] = "new-visual-only-rule"
    assert not contract()["changed"]
    category["stage1"]["rule_version"] = "new-text-rule"
    assert contract()["changed"]


@pytest.mark.skipif(os.name != "nt", reason="Windows open handles prevent replacement")
def test_snapshot_publication_waits_for_brief_real_windows_reader(tmp_path):
    destination = tmp_path / "candidates.jsonl"
    destination.write_text("old", encoding="utf-8")
    replacement = tmp_path / "new.tmp"
    replacement.write_text("new", encoding="utf-8")
    reader = destination.open("rb")
    timer = threading.Timer(0.1, reader.close)
    timer.start()
    try:
        replace_snapshot_file(replacement, destination, timeout_seconds=1)
    finally:
        reader.close()
        timer.join()
    assert destination.read_text("utf-8") == "new"


@pytest.mark.skipif(os.name != "nt", reason="Windows open handles prevent replacement")
def test_snapshot_persistent_lock_reports_failure_without_losing_old_data(tmp_path):
    destination = tmp_path / "candidates.jsonl"
    destination.write_text("old", encoding="utf-8")
    replacement = tmp_path / "new.tmp"
    replacement.write_text("new", encoding="utf-8")
    with destination.open("rb"):
        with pytest.raises(PermissionError):
            replace_snapshot_file(replacement, destination, timeout_seconds=0.05)
    assert destination.read_text("utf-8") == "old"
    assert replacement.read_text("utf-8") == "new"


def test_original_amazon_image_is_not_ranked_below_small_display_image():
    code = """
import assert from 'node:assert/strict';
import {selectAmazonMainImage} from './scripts/collection_checkpoint.mjs';
const display={url:'https://images.example/display.jpg',size:[679,613],source:'详情页动态主图'};
const original={url:'https://images.example/original.jpg',size:[0,0],source:'详情页原始主图'};
assert.equal(selectAmazonMainImage([display,original]).url,original.url);
assert.equal(selectAmazonMainImage([display]).url,display.url);
assert.equal(selectAmazonMainImage([{url:'javascript:bad',size:[9999,9999]},display]).url,display.url);
"""
    result = subprocess.run(["node", "--input-type=module", "-e", code], cwd=ROOT, capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr


def test_legacy_layer12_watcher_does_not_cache_failed_publication(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location("legacy_watch_repairs", ROOT / "scripts/watch_excel_output.py")
    watcher = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(watcher)
    monkeypatch.setattr(watcher, "call", lambda _: json.dumps({"输出": [{"查看副本发布": {
        "固定查看副本已更新": False, "时间戳查看快照已生成": True,
    }}]}))
    with pytest.raises(RuntimeError, match="下轮重试"):
        watcher.export("layer12", tmp_path, tmp_path / "stage12_config.json")
