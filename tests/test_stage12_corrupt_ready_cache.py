"""Offline contracts for a JPEG that verifies but cannot be decoded."""
import hashlib
import io
import json
import os
from pathlib import Path
import sys

import numpy as np
import pytest
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from selection_workflow.incremental_stage12 import IncrementalStage12, PathSpec, SourceSpec
from run_incremental_stage12 import WatchInstance


def jpeg_bytes():
    stream = io.BytesIO()
    Image.new('RGB', (32, 32), (100, 80, 60)).save(stream, format='JPEG')
    return stream.getvalue()


@pytest.fixture
def broken_jpeg():
    selected = os.environ.get('SELECTION_BAD_JPEG_SAMPLE')
    data = Path(selected).read_bytes() if selected else jpeg_bytes()[:-10]
    if selected:
        assert hashlib.sha256(data).hexdigest() == '5dc1784a14ef3259c6768994e6d416c8b310e4ea95a9c265e52b140ff44157a4'
    with Image.open(io.BytesIO(data)) as image:
        image.verify()
    with pytest.raises(OSError):
        with Image.open(io.BytesIO(data)) as image:
            image.load()
    return data


class DecodingVectorizer:
    revision = 'offline-decoding-vector-v1'

    def encode(self, path):
        with Image.open(path) as image:
            image.load()
            return np.asarray(image.convert('RGB')).mean(axis=(0, 1)).astype(np.float32)


def seeded_run(tmp_path, broken_jpeg, monkeypatch):
    amazon = tmp_path / 'amazon.jsonl'
    temu = tmp_path / 'temu.jsonl'
    amazon.write_text(''.join(json.dumps({'asin': f'A{n}', 'title': 'floral shower curtain',
        'main_image_url': 'https://a.test/image', 'url': f'https://a.test/{n}'}) + '\n' for n in range(10)))
    temu.write_text(''.join(json.dumps({'goods_id': name, 'title': 'floral shower curtain',
        'main_image_url': f'https://t.test/{name}', 'product_url': f'https://t.test/product/{name}'}) + '\n' for name in ('bad', 'good')))
    engine = IncrementalStage12(tmp_path / 'run', vectorizer=DecodingVectorizer())
    engine.configure([SourceSpec('a', 'AMAZON', 'amazon_generic', amazon), SourceSpec('t', 'TEMU', 'temu_frontend', temu)],
                     [PathSpec('p', 'a', 't', 'test')])
    engine.ingest_source('a'); engine.ingest_source('t')
    monkeypatch.setattr(engine._image_transport, 'read', lambda *_a, **_k: jpeg_bytes())
    engine.process_batch(100)
    product = engine._product('t', 'bad')
    ref, key = engine._image_identity(product)
    with engine.con:
        content_hash, path = engine._persist_image(key, ref, broken_jpeg, 0)
        engine.image_retry.started(key, ref, product)
        engine.image_retry.finish(key)
        engine.con.execute('INSERT OR REPLACE INTO vectors VALUES(?,?,?,?,?,?,?)',
            (content_hash, engine.vectorizer.revision, None, None, 'FAILED', 'broken data stream when reading image file', 'old'))
        engine.con.execute("UPDATE pairs SET status='FAILED_BOUNDED',retry_count=3,last_error='broken data stream when reading image file' WHERE goods_id='bad'")
        engine.con.execute("DELETE FROM candidates WHERE goods_id='bad'")
    return engine, key, path


def migrate(engine):
    owner = WatchInstance(engine.root)
    owner.acquire()
    try:
        engine.migrate_image_retries(owner)
    finally:
        owner.release()


def test_download_requires_full_decode(tmp_path, broken_jpeg, monkeypatch):
    engine = IncrementalStage12(tmp_path / 'run', vectorizer=DecodingVectorizer())
    monkeypatch.setattr(engine._image_transport, 'read', lambda *_a, **_k: broken_jpeg)
    try:
        with pytest.raises((OSError, RuntimeError), match='broken data stream|truncated|invalid image'):
            engine._download_image_bytes({'platform': 'TEMU', 'main_image_url': 'https://t.test/image'})
    finally:
        engine.close()


def test_restart_repairs_only_decode_failed_pairs_and_keeps_history(tmp_path, broken_jpeg, monkeypatch):
    engine, key, old_path = seeded_run(tmp_path, broken_jpeg, monkeypatch)
    with engine.con:
        engine.con.execute("UPDATE pairs SET last_error='vector dimension mismatch' WHERE asin='A0' AND goods_id='bad'")
    good = [tuple(row) for row in engine.con.execute("SELECT * FROM pairs WHERE goods_id='good' ORDER BY asin")]
    revisions = [tuple(row) for row in engine.con.execute("SELECT asin,pair_revision FROM pairs WHERE goods_id='bad' ORDER BY asin")]
    engine.close()
    engine = IncrementalStage12(tmp_path / 'run', vectorizer=DecodingVectorizer())
    hits = []
    monkeypatch.setattr(engine._image_transport, 'read', lambda ref, **_k: hits.append(ref) or jpeg_bytes())
    try:
        migrate(engine)
        assert engine.image_retry.get(key)['status'] == 'RETRY'
        assert engine.con.execute("SELECT count(*) FROM pairs WHERE status='WAIT_IMAGE_RETRY' AND retry_count=3").fetchone()[0] == 9
        assert engine.con.execute('SELECT count(*) FROM image_pair_requeue_history').fetchone()[0] == 9
        engine.process_batch(100); engine.process_batch(100)
        assert hits == ['https://t.test/bad']
        assert engine.con.execute("SELECT count(*) FROM pairs WHERE goods_id='bad' AND status='CANDIDATE' AND retry_count=3").fetchone()[0] == 9
        assert engine.con.execute("SELECT status FROM pairs WHERE goods_id='bad' AND asin='A0'").fetchone()[0] == 'FAILED_BOUNDED'
        assert good == [tuple(row) for row in engine.con.execute("SELECT * FROM pairs WHERE goods_id='good' ORDER BY asin")]
        assert revisions == [tuple(row) for row in engine.con.execute("SELECT asin,pair_revision FROM pairs WHERE goods_id='bad' ORDER BY asin")]
        assert old_path.read_bytes() == broken_jpeg
        assert engine.image_retry.get(key)['attempts'] == 3
        assert engine.con.execute("SELECT count(*) FROM image_retry_history WHERE event='CORRUPT_CACHE_RETRY_SCHEDULED'").fetchone()[0] == 1
        migrate(engine); engine.process_batch(100)
        assert hits == ['https://t.test/bad']
    finally:
        engine.close()


def test_replacement_still_corrupt_stops_without_redownload_loop(tmp_path, broken_jpeg, monkeypatch):
    engine, key, old_path = seeded_run(tmp_path, broken_jpeg, monkeypatch)
    hits = []
    monkeypatch.setattr(engine._image_transport, 'read', lambda ref, **_k: hits.append(ref) or broken_jpeg)
    try:
        migrate(engine)
        engine.process_batch(100); engine.process_batch(100)
        assert engine.image_retry.get(key)['status'] == 'ACTION_REQUIRED'
        assert engine.image_retry.get(key)['last_classification'] == 'ACTION_INVALID_IMAGE'
        assert engine.con.execute("SELECT count(*) FROM pairs WHERE status='FAILED_IMAGE_ACTION'").fetchone()[0] == 10
        for _ in range(4):
            migrate(engine); engine.process_batch(100)
        assert hits == ['https://t.test/bad']
        assert old_path.read_bytes() == broken_jpeg
    finally:
        engine.close()


def test_valid_cache_and_non_decode_vector_failure_are_not_invalidated(tmp_path, broken_jpeg, monkeypatch):
    engine, key, _ = seeded_run(tmp_path, broken_jpeg, monkeypatch)
    try:
        with engine.con:
            engine.con.execute("UPDATE vectors SET last_error='vector dimension mismatch' WHERE status='FAILED'")
        before = [tuple(row) for row in engine.con.execute('SELECT * FROM pairs ORDER BY asin,goods_id')]
        migrate(engine)
        assert engine.image_retry.get(key)['status'] == 'READY'
        assert before == [tuple(row) for row in engine.con.execute('SELECT * FROM pairs ORDER BY asin,goods_id')]
        with engine.con:
            engine._persist_image(key, 'https://t.test/bad', jpeg_bytes(), 0)
            engine.con.execute("UPDATE vectors SET status='FAILED',last_error='broken data stream when reading image file'")
        migrate(engine)
        assert engine.image_retry.get(key)['status'] == 'READY'
        assert before == [tuple(row) for row in engine.con.execute('SELECT * FROM pairs ORDER BY asin,goods_id')]
    finally:
        engine.close()


def test_missing_cache_is_reported_without_reclassifying_it_as_bad_pixels(tmp_path, broken_jpeg, monkeypatch):
    engine, key, path = seeded_run(tmp_path, broken_jpeg, monkeypatch)
    path.unlink()
    try:
        with pytest.raises(FileNotFoundError):
            migrate(engine)
        assert engine.image_retry.get(key)['status'] == 'READY'
        assert engine.con.execute("SELECT count(*) FROM pairs WHERE status='FAILED_BOUNDED'").fetchone()[0] == 10
    finally:
        engine.close()


def test_old_bad_cache_without_current_failed_pairs_is_not_redownloaded(tmp_path, broken_jpeg, monkeypatch):
    engine, key, _ = seeded_run(tmp_path, broken_jpeg, monkeypatch)
    try:
        with engine.con:
            engine.con.execute("UPDATE pairs SET status='STAGE1_EXCLUDED',last_error=NULL WHERE goods_id='bad'")
        migrate(engine)
        assert engine.image_retry.get(key)['status'] == 'READY'
        assert engine.con.execute("SELECT count(*) FROM image_retry_history WHERE event='CORRUPT_CACHE_RETRY_SCHEDULED'").fetchone()[0] == 0
        assert engine.con.execute('SELECT count(*) FROM image_pair_requeue_history').fetchone()[0] == 0
    finally:
        engine.close()
