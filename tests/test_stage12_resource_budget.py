"""Local analysis must leave memory and CPU headroom for live browsers."""
from contextlib import nullcontext
import importlib.util
import json
from pathlib import Path
import sys
from types import SimpleNamespace

import numpy as np
from PIL import Image

from selection_workflow import incremental_stage12 as stage12


ROOT = Path(__file__).resolve().parents[1]


def test_resnet_streams_views_in_order_with_bounded_activation_batch(tmp_path, monkeypatch):
    sizes = []

    class Output:
        def __init__(self, values): self.values = values
        def cpu(self): return self
        def numpy(self): return self.values

    def model(values):
        sizes.append(len(values))
        return Output(values)

    vectorizer = stage12.TorchResNet50Vectorizer()
    vectorizer._model = model
    vectorizer._preprocess = lambda view: np.array([view.getpixel((0, 0))[0]], dtype=np.float32)
    monkeypatch.setitem(sys.modules, "torch", SimpleNamespace(inference_mode=nullcontext, stack=np.stack))
    monkeypatch.setattr(stage12, "image_views", lambda image: (image, image, image))
    paths = []
    for index in range(9):
        path = tmp_path / f"{index}.png"
        Image.new("RGB", (2, 2), (index, 0, 0)).save(path)
        paths.append(path)
    vectors = vectorizer.encode_many(paths)
    assert max(sizes) <= 8
    assert sum(sizes) == 27
    np.testing.assert_array_equal(np.asarray(vectors), np.repeat(np.arange(9)[:, None], 3, axis=1))


def test_first_layer_is_visible_to_dashboard_before_image_inference(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location("resource_dashboard", ROOT / "scripts/dashboard_progress.py")
    dashboard = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(dashboard)

    class Vectorizer:
        revision = "resource-test-v1"
        def encode(self, path): return np.array([1, 2], dtype=np.float32)

    image = tmp_path / "image.png"
    Image.new("RGB", (64, 64), "red").save(image)
    source_paths = []
    for source, identity in (("amazon", "asin"), ("temu", "goods_id")):
        source_path = tmp_path / f"{source}.jsonl"
        source_path.write_text(json.dumps({identity: source, "title": "blue floral shower curtain",
            "local_image_path": str(image)}), encoding="utf-8")
        source_paths.append(source_path)
    pipeline = stage12.IncrementalStage12(tmp_path / "stage12", vectorizer=Vectorizer(), text_gate_workers=16)
    try:
        assert pipeline.text_gate_workers == 4  # Also protect existing 16-worker configs.
        pipeline.configure([
            stage12.SourceSpec("a", "AMAZON", "amazon_generic", source_paths[0]),
            stage12.SourceSpec("t", "TEMU", "temu_frontend", source_paths[1]),
        ], [stage12.PathSpec("p", "a", "t", "test")])
        pipeline.ingest_source("a")
        pipeline.ingest_source("t")
        prefetch = pipeline._prefetch_images

        def observe_before_image_phase(products):
            metrics = dashboard._database(pipeline.root)
            assert metrics["stage1"]["completed"] == 1
            assert metrics["stage2"]["completed"] == 0
            return prefetch(products)

        monkeypatch.setattr(pipeline, "_prefetch_images", observe_before_image_phase)
        result = pipeline.process_batch(10)
        assert result["completed"] == 1, dict(pipeline.con.execute("SELECT status,last_error FROM pairs").fetchone())
        assert dashboard._database(pipeline.root)["stage2"]["completed"] == 1
    finally:
        pipeline.close()


def test_new_tasks_use_browser_friendly_resource_defaults(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location("resource_pipeline", ROOT / "scripts/pipeline.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, "ROOT", tmp_path)
    category = json.loads((ROOT / "config/categories/power_drill.json").read_text("utf-8"))
    config = json.loads(module.write_stage12_config(category).read_text("utf-8"))
    assert config["batch_size"] == 1000
    assert config["vector_batch_size"] == 16
    assert config["image_download"]["workers"] == stage12.MAX_IMAGE_CONNECTIONS
    assert 1 <= config["text_gate_workers"] <= 4
