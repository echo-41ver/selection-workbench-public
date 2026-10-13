"""Offline regressions for local rule refresh and continuous delivery."""
from __future__ import annotations

import importlib.util
import json
import os
import sqlite3
import shutil
import subprocess
import sys
from pathlib import Path

import numpy as np
import pytest
from PIL import Image

from selection_workflow.bath_towel_text_gate_v1 import evaluate_text_gate
from selection_workflow.incremental_stage12 import IncrementalStage12, PathSpec, SourceSpec

ROOT = Path(__file__).resolve().parents[1]


def script(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.skipif(os.name != "nt", reason="Windows retains exited process objects while handles remain open")
@pytest.mark.parametrize("module_name, checker_name", [
    ("watch_excel_output", "running"),
    ("run_incremental_stage12", "process_is_running"),
])
def test_exited_process_with_retained_popen_handle_is_not_running(module_name, checker_name):
    checker = getattr(script(module_name), checker_name)
    assert checker(os.getpid()) is True
    child = subprocess.Popen([sys.executable, "-c", "pass"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        assert child.wait(timeout=15) == 0
        # Popen still owns its Windows process handle after wait(); existence
        # of that kernel object must not be mistaken for a live watcher.
        assert checker(child.pid) is False
    finally:
        if child.poll() is None:
            child.terminate()
            child.wait(timeout=15)


class FakeVectorizer:
    def __init__(self, revision="local-vector-v1"):
        self.revision = revision
        self.calls = 0

    def encode(self, path):
        self.calls += 1
        # Deterministic non-identical vectors yield a score between 50 and 90.
        return np.array([1.0, 0.0] if path.read_bytes()[-1] == 1 else [0.8, 0.6], dtype=np.float32)


def sources(tmp_path):
    return [SourceSpec("a", "AMAZON", "amazon_generic", tmp_path / "a.jsonl", "replace"),
            SourceSpec("t", "TEMU", "temu_frontend", tmp_path / "t.jsonl", "replace")]


def create_inputs(tmp_path):
    for name, platform_id, color in [("a", "asin", "red"), ("t", "goods_id", "blue")]:
        image = tmp_path / f"{name}.png"
        Image.new("RGB", (20, 20), color).save(image)
        image.write_bytes(image.read_bytes() + (b"\x01" if name == "a" else b"\x02"))
        row = {platform_id: name, "title": "cotton bath towel set", "local_image_path": str(image),
               "main_image_url": f"https://images.example/{name}.png", "product_url": f"https://product.example/{name}"}
        (tmp_path / f"{name}.jsonl").write_text(json.dumps(row) + "\n", encoding="utf-8")


def pipeline(tmp_path, *, threshold=50, rule="text-v1", vector="local-vector-v1", overall="vision-v1"):
    instance = IncrementalStage12(tmp_path / "run", vectorizer=FakeVectorizer(vector), threshold=threshold,
                                  text_gate_name="bath_towel_set_v2", rule_version=overall, stage12_rule_version=rule)
    instance.configure(sources(tmp_path), [PathSpec("pair", "a", "t", "test")])
    return instance


@pytest.mark.parametrize("temu", ["cotton bath towels 4 count", "cotton bath towels 4 ct", "cotton bath towel"])
def test_pack_wording_does_not_manufacture_single_towel_conflict(temu):
    result = evaluate_text_gate("cotton bath towel set of 4", temu)
    assert result["advance_to_image_vector"] is True
    if temu == "cotton bath towel":
        assert result["facts"]["product_subtype"]["temu"] is None


@pytest.mark.parametrize("change", [{"threshold": 90}, {"rule": "text-v2"}, {"vector": "local-vector-v2"}])
def test_processing_contract_refreshes_identical_snapshots_without_rebilling(tmp_path, change):
    create_inputs(tmp_path)
    first = pipeline(tmp_path)
    for source in sources(tmp_path):
        first.ingest_source(source.key)
    first.process_batch(10)
    original = dict(first.con.execute("SELECT * FROM pairs").fetchone())
    assert original["status"] == "CANDIDATE"
    first.close()

    updated = pipeline(tmp_path, **change)
    try:
        assert updated.ingest_source("a")["reused_snapshot"] == 1
        row = dict(updated.con.execute("SELECT * FROM pairs").fetchone())
        assert row["status"] == "PENDING"
        assert row["pair_revision"] == original["pair_revision"]
        assert updated.con.execute("SELECT count(*) FROM candidates").fetchone()[0] == 0
        updated.process_batch(10)
        row = dict(updated.con.execute("SELECT * FROM pairs").fetchone())
        assert row["pair_revision"] == original["pair_revision"]
        assert row["status"] == ("BELOW_VECTOR_THRESHOLD" if "threshold" in change else "CANDIDATE")
        if "vector" not in change:
            assert updated.vectorizer.calls == 0
        else:
            assert updated.vectorizer.calls == 2
        assert updated.ensure_processing_contract()["rescheduled"] == 0
    finally:
        updated.close()


def test_third_layer_version_does_not_invalidate_local_contract_or_paid_identity(tmp_path):
    create_inputs(tmp_path)
    first = pipeline(tmp_path)
    for source in sources(tmp_path):
        first.ingest_source(source.key)
    first.process_batch(10)
    revision = first.con.execute("SELECT pair_revision FROM pairs").fetchone()[0]
    first.close()
    second = pipeline(tmp_path, overall="vision-v2")
    try:
        assert second.ingest_source("a")["reused_snapshot"] == 1
        assert second.process_batch(10)["selected"] == 0
        assert second.con.execute("SELECT pair_revision FROM pairs").fetchone()[0] == revision
    finally:
        second.close()


def test_legacy_contract_and_gate_implementation_refresh_preserve_pair_revision(tmp_path):
    create_inputs(tmp_path)
    current = pipeline(tmp_path)
    try:
        for source in sources(tmp_path):
            current.ingest_source(source.key)
        current.process_batch(10)
        revision = current.con.execute("SELECT pair_revision FROM pairs").fetchone()[0]
        with current.con:
            current.con.execute("DELETE FROM processing_metadata")
        current._checked_processing_contract = None
        assert current.ensure_processing_contract()["rescheduled"] == 1
        current.process_batch(10)
        current._text_gate_revision = "new-gate-implementation"
        assert current.ensure_processing_contract()["rescheduled"] == 1
        assert current.con.execute("SELECT pair_revision FROM pairs").fetchone()[0] == revision
    finally:
        current.close()


def test_watcher_notices_wal_config_scorer_and_cache_manifest(tmp_path, monkeypatch):
    watcher = script("watch_excel_output")
    monkeypatch.setattr(watcher, "ROOT", tmp_path)
    base = tmp_path / "runs" / "bath_towel_set"
    base.mkdir(parents=True)
    config = base / "stage12_config.json"
    config.write_text(json.dumps({"output_dir": str(base)}), encoding="utf-8")
    database = base / "stage12.sqlite3"
    connection = sqlite3.connect(database)
    try:
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA wal_autocheckpoint=0")
        connection.execute("CREATE TABLE test(value INTEGER)")
        connection.commit()
        previous = watcher.source_signature("layer123", base, config)
        main_mtime = database.stat().st_mtime_ns
        connection.execute("INSERT INTO test VALUES(1)")
        connection.commit()
        assert database.stat().st_mtime_ns == main_mtime
        current = watcher.source_signature("layer123", base, config)
        assert current != previous
        for path in [config, tmp_path / "src" / "selection_workflow" / "bath_towel_layer3_v1.py",
                     tmp_path / "src" / "selection_workflow" / "bath_towel_cache.py",
                     base / "stage123" / "legacy_paid_cache_v3.json"]:
            path.parent.mkdir(parents=True, exist_ok=True)
            text = path.read_text(encoding="utf-8") if path.exists() else ""
            path.write_text(text + " ", encoding="utf-8")
            updated = watcher.source_signature("layer123", base, config)
            assert updated != current
            current = updated
    finally:
        connection.close()


def test_failed_fixed_publication_is_retried_without_source_change(tmp_path, monkeypatch):
    watcher = script("watch_excel_output")
    monkeypatch.setattr(watcher, "ROOT", tmp_path)
    monkeypatch.setattr(sys, "argv", ["watch_excel_output.py", "--kind", "layer123"])
    monkeypatch.setattr(watcher, "source_signature", lambda *args: "unchanged-source")
    published = []
    states = []

    def export(*args):
        published.append(True)
        watcher.require_fixed_publication({"固定查看副本已更新": len(published) > 1})
        return {"published": True}

    class EndWatch(Exception):
        pass

    def sleep(_):
        if len(states) == 3:
            raise EndWatch

    monkeypatch.setattr(watcher, "export", export)
    monkeypatch.setattr(watcher, "atomic_json", lambda path, state: states.append(state))
    monkeypatch.setattr(watcher.time, "sleep", sleep)
    with pytest.raises(EndWatch):
        watcher.main()
    assert len(published) == 2
    assert [state["status"] for state in states] == ["EXPORT_FAILED_PREVIOUS_WORKBOOK_PRESERVED", "PUBLISHED", "WATCHING_NO_CHANGE"]


def test_watch_instance_is_exclusive_and_recoverable(tmp_path):
    runner = script("run_incremental_stage12")
    first, second = runner.WatchInstance(tmp_path), runner.WatchInstance(tmp_path)
    first.acquire()
    try:
        with pytest.raises(SystemExit) as error:
            second.acquire()
        assert error.value.code == 75
    finally:
        first.release()
    second.acquire()
    second.release()


def test_excel_existing_watcher_returns_busy_code(tmp_path, monkeypatch):
    watcher = script("watch_excel_output")
    monkeypatch.setattr(watcher, "ROOT", tmp_path)
    monkeypatch.setattr(sys, "argv", ["watch_excel_output.py", "--kind", "temu"])
    lock = tmp_path / "runs" / "bath_towel_set" / "excel_watch" / "temu.pid"
    lock.parent.mkdir(parents=True)
    lock.write_text("123", encoding="ascii")
    monkeypatch.setattr(watcher, "running", lambda pid: pid == 123)
    with pytest.raises(SystemExit) as error:
        watcher.main()
    assert error.value.code == 75
    assert lock.read_text(encoding="ascii") == "123"


def test_temu_data_only_export_cannot_publish_before_validation(tmp_path, monkeypatch):
    exporter = script("export_temu_union_excel")
    source = tmp_path / "input"
    source.mkdir()
    (source / "products.jsonl").write_text(json.dumps({"goods_id": "1", "title": "towel"}) + "\n", encoding="utf-8")
    (source / "summary.json").write_text('{"unique_goods_id": 1}', encoding="utf-8")
    monkeypatch.setattr(exporter, "publish_view_copies", lambda *a, **kw: pytest.fail("data-only export published a view"))
    output = tmp_path / "towels_data.xlsx"
    result = exporter.build(source, output, data_only=True)
    assert output.exists()
    assert result["发布"] is None
    assert result["Excel查看副本"] is None


@pytest.mark.parametrize("valid", [True, False])
def test_temu_watcher_validates_data_before_publishing(tmp_path, monkeypatch, valid):
    watcher = script("watch_excel_output")
    calls = []

    def call(command):
        name = Path(command[1]).name
        calls.append(name)
        if name == "export_temu_union_excel.py":
            assert "--data-only" in command
            assert command[command.index("--output") + 1].endswith("_data.xlsx")
        if name == "validate_temu_union_excel.py":
            assert command[command.index("--book") + 1].endswith("_data.xlsx")
            if not valid:
                raise RuntimeError("validation failed")
        return "{}"

    def publish(source, destination, **kwargs):
        calls.append("publish")
        assert source.name.endswith("_data.xlsx")
        assert not destination.name.endswith("_data.xlsx")
        return {"固定查看副本已更新": True}

    monkeypatch.setattr(watcher, "call", call)
    monkeypatch.setattr(watcher, "publish_view_copies", publish)
    if valid:
        watcher.export("temu", tmp_path, tmp_path / "config.json")
        assert calls == ["export_temu_union_excel.py", "validate_temu_union_excel.py", "publish"]
    else:
        with pytest.raises(RuntimeError, match="validation failed"):
            watcher.export("temu", tmp_path, tmp_path / "config.json")
        assert "publish" not in calls


def test_launcher_skips_live_watchers_without_touching_logs(tmp_path):
    shell = shutil.which("pwsh")
    if not shell:
        pytest.skip("PowerShell not available")
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    launcher = scripts / "start_excel_watchers.ps1"
    launcher.write_text((ROOT / "scripts" / launcher.name).read_text(encoding="utf-8"), encoding="utf-8")
    python = tmp_path / ".venv" / "Scripts" / "python.exe"
    python.parent.mkdir(parents=True)
    python.write_bytes(b"fixture, never executed")
    state = tmp_path / "runs" / "bath_towel_set" / "excel_watch"
    state.mkdir(parents=True)
    for kind in ("temu", "layer12", "layer123"):
        (state / f"{kind}.log").write_text("preserve previous output", encoding="utf-8")
    # Mock the OS process inventory and launch command in a child shell. No
    # real watcher or browser is launched by this regression.
    command = r'''
$global:watcherScript = Join-Path $env:PIPELINE_TEST_ROOT 'scripts\watch_excel_output.py'
function Get-CimInstance {
    foreach ($kind in @('temu', 'layer12', 'layer123')) {
        [pscustomobject]@{ProcessId=123; Name='python.exe'; CommandLine=('python "' + $global:watcherScript + '" --kind ' + $kind + ' --category-key bath_towel_set')}
    }
}
function Start-Process { throw 'Duplicate watcher launch attempted' }
function Start-Sleep {}
& (Join-Path $env:PIPELINE_TEST_ROOT 'scripts\start_excel_watchers.ps1')
'''
    import os
    env = {**os.environ, "PIPELINE_TEST_ROOT": str(tmp_path)}
    result = subprocess.run([shell, "-NoProfile", "-Command", command], env=env, capture_output=True, text=True, encoding="utf-8")
    assert result.returncode == 0, result.stderr
    for kind in ("temu", "layer12", "layer123"):
        assert (state / f"{kind}.log").read_text(encoding="utf-8") == "preserve previous output"
