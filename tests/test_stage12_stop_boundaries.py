"""Stop checkpoints preserve truthful local progress and resume on the same pairs.

All image bytes and vectors are local fakes. No model or network is contacted.
"""
import json

import numpy as np
import pytest

from selection_workflow import incremental_stage12 as engine


class Vectorizer:
    revision = "stop-boundary-local-fixture-v1"

    def __init__(self):
        self.batch_calls = 0
        self.single_calls = 0
        self.batch_hook = None
        self.single_hook = None
        self.batch_failure = False

    def encode_many(self, paths):
        self.batch_calls += 1
        if self.batch_hook:
            self.batch_hook()
        if self.batch_failure:
            raise ValueError("fixture batch inference failure")
        return [np.array([1.0, 2.0], dtype=np.float32) for _ in paths]

    def encode(self, path):
        self.single_calls += 1
        if self.single_hook:
            self.single_hook()
        return np.array([1.0, 2.0], dtype=np.float32)


@pytest.fixture
def sample(tmp_path, monkeypatch):
    model = Vectorizer()
    pipeline = engine.IncrementalStage12(tmp_path / "stage12", vectorizer=model,
                                         vector_batch_size=1, image_download_workers=1)
    stopped = [False]
    pipeline.stop_requested = lambda: stopped[0]
    source_paths = []
    for source, field, identities in (("amazon", "asin", ["A1"]),
                                      ("temu", "goods_id", ["T1", "T2"])):
        path = tmp_path / (source + ".jsonl")
        path.write_text("".join(json.dumps({field: identity,
            "title": "clear PEVA shower curtain", "main_image_url": "https://example.invalid/" + identity,
            "product_url": "https://example.invalid/product/" + identity}) + "\n"
            for identity in identities), encoding="utf-8")
        source_paths.append(path)
    pipeline.configure([
        engine.SourceSpec("amazon", "AMAZON", "amazon_generic", source_paths[0]),
        engine.SourceSpec("temu", "TEMU", "temu_frontend", source_paths[1]),
    ], [engine.PathSpec("fixture", "amazon", "temu", "Local stop fixture")])
    for source in ("amazon", "temu"):
        pipeline.ingest_source(source)
    # Exercise actual local cache and pair transactions while replacing the
    # sole byte transport boundary with deterministic per-product fake bytes.
    monkeypatch.setattr(pipeline, "_download_image_bytes",
                        lambda product: (product["product_id"] + "-fixture").encode() * 30)
    monkeypatch.setattr(pipeline._image_transport, "read",
                        lambda *args, **kwargs: pytest.fail("No network is authorized by this test"))
    revisions = dict(pipeline.con.execute("SELECT goods_id,pair_revision FROM pairs"))
    try:
        yield pipeline, model, stopped, revisions
    finally:
        pipeline.close()


def assert_pending(pipeline, *, stage1):
    pairs = list(pipeline.con.execute("SELECT status,stage1_status,vector_score,last_error,retry_count FROM pairs"))
    assert len(pairs) == 2
    assert all(row["status"] == "PENDING" and row["vector_score"] is None
               and row["last_error"] is None and row["retry_count"] == 0 for row in pairs)
    assert all(bool(row["stage1_status"]) is stage1 for row in pairs)
    assert pipeline.con.execute("SELECT count(*) FROM candidates").fetchone()[0] == 0
    counters = dict(pipeline.con.execute("SELECT name,value FROM counters"))
    assert counters.get("pairs_processed", 0) == 0
    assert counters.get("pair_failures", 0) == 0


def assert_cancelled(result, phase, *, stage1_count):
    assert result["cancelled"] is True and result["cancelled_phase"] == phase
    assert result["completed"] == result["failed"] == result["new_candidates"] == 0
    assert result["stage1_committed"] == stage1_count


def resume_same_pairs(pipeline, model, stopped, revisions):
    stopped[0] = False
    model.batch_hook = model.single_hook = None
    model.batch_failure = False
    result = pipeline.process_batch(10)
    assert not result.get("cancelled") and result["completed"] == 2 and result["failed"] == 0
    assert result["new_candidates"] == 2
    assert dict(pipeline.con.execute("SELECT goods_id,pair_revision FROM pairs")) == revisions
    assert pipeline.con.execute("SELECT count(*) FROM candidates").fetchone()[0] == 2


def test_stop_during_main_product_preparation_keeps_unprocessed_pairs_for_resume(sample, monkeypatch):
    pipeline, model, stopped, revisions = sample
    product = pipeline._product
    calls = []
    def read_product(*args):
        calls.append(args)
        value = product(*args)
        stopped[0] = True
        return value
    with monkeypatch.context() as patch:
        patch.setattr(pipeline, "_product", read_product)
        result = pipeline.process_batch(10)
    assert_cancelled(result, "text_preparation", stage1_count=0)
    assert_pending(pipeline, stage1=False)
    assert model.batch_calls == model.single_calls == 0
    resume_same_pairs(pipeline, model, stopped, revisions)


def test_stop_after_images_retains_stage1_and_real_cache_without_starting_vectors(sample, monkeypatch):
    pipeline, model, stopped, revisions = sample
    prefetch = pipeline._prefetch_images
    def stop_after_images(products):
        result = prefetch(products)
        stopped[0] = True
        return result
    with monkeypatch.context() as patch:
        patch.setattr(pipeline, "_prefetch_images", stop_after_images)
        patch.setattr(pipeline, "_prefetch_vectors", lambda products: pytest.fail("Vectors must not start after Stop"))
        result = pipeline.process_batch(10)
    assert_cancelled(result, "image_download", stage1_count=2)
    assert_pending(pipeline, stage1=True)
    assert pipeline.con.execute("SELECT count(*) FROM images WHERE status='READY'").fetchone()[0] == 3
    assert model.batch_calls == model.single_calls == 0
    resume_same_pairs(pipeline, model, stopped, revisions)


def test_stop_after_one_vector_chunk_prevents_remaining_chunks_and_false_completion(sample):
    pipeline, model, stopped, revisions = sample
    model.batch_hook = lambda: stopped.__setitem__(0, True)
    result = pipeline.process_batch(10)
    assert_cancelled(result, "vector_inference", stage1_count=2)
    assert_pending(pipeline, stage1=True)
    assert model.batch_calls == 1 and model.single_calls == 0
    assert pipeline.con.execute("SELECT count(*) FROM vectors").fetchone()[0] == 0
    resume_same_pairs(pipeline, model, stopped, revisions)


def test_stop_during_single_image_fallback_is_not_swallowed_as_model_failure(sample):
    pipeline, model, stopped, revisions = sample
    pipeline.vector_batch_size = 3
    model.batch_failure = True
    model.single_hook = lambda: stopped.__setitem__(0, True)
    result = pipeline.process_batch(10)
    assert_cancelled(result, "vector_inference", stage1_count=2)
    assert_pending(pipeline, stage1=True)
    assert model.batch_calls == model.single_calls == 1
    assert pipeline.con.execute("SELECT count(*) FROM vectors").fetchone()[0] == 0
    resume_same_pairs(pipeline, model, stopped, revisions)


def test_stop_during_pair_preparation_preserves_stage1_for_resume(sample, monkeypatch):
    pipeline, model, stopped, revisions = sample
    ensure_image = pipeline._ensure_image
    def stop_after_image(*args):
        value = ensure_image(*args)
        stopped[0] = True
        return value
    with monkeypatch.context() as patch:
        patch.setattr(pipeline, "_ensure_image", stop_after_image)
        result = pipeline.process_batch(10)
    assert_cancelled(result, "pair_preparation", stage1_count=2)
    assert_pending(pipeline, stage1=True)
    resume_same_pairs(pipeline, model, stopped, revisions)


def test_stop_at_final_pair_commit_never_publishes_computed_but_uncommitted_pairs(sample):
    pipeline, model, stopped, revisions = sample
    pipeline.stop_requested = lambda: pipeline._batch_phase == "pair_commit"
    result = pipeline.process_batch(10)
    assert_cancelled(result, "pair_commit", stage1_count=2)
    assert_pending(pipeline, stage1=True)
    cached_model_calls = model.batch_calls
    assert cached_model_calls == 3
    assert pipeline.con.execute("SELECT count(*) FROM vectors WHERE status='READY'").fetchone()[0] == 3
    pipeline.stop_requested = lambda: stopped[0]
    resume_same_pairs(pipeline, model, stopped, revisions)
    assert model.batch_calls == cached_model_calls  # Resume uses actual completed local cache.
