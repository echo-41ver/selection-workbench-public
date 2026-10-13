from __future__ import annotations

import json
import os
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path

import numpy as np
from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from selection_workflow.incremental_stage12 import (
    IncrementalStage12, PathSpec, SourceSpec, normalize_record,
    unique_temporary_path,
    same_snapshot_bytes,
)
from selection_workflow.shower_text_gate_v2 import evaluate_text_gate


class FakeVectorizer:
    revision = "fake-vector-v1"
    def __init__(self): self.calls = 0; self.batch_calls = 0; self.batch_sizes = []
    def encode(self, image_path: Path) -> np.ndarray:
        self.calls += 1
        with Image.open(image_path) as image:
            rgb = np.asarray(image.convert("RGB").resize((1, 1)), dtype=np.float32).reshape(-1)
        return np.concatenate((rgb, np.array([1.0], dtype=np.float32)))
    def encode_many(self, image_paths: list[Path]) -> list[np.ndarray]:
        self.batch_calls += 1; self.batch_sizes.append(len(image_paths))
        return [self.encode(path) for path in image_paths]


class SnapshotTemporaryPathTests(unittest.TestCase):
    def test_parallel_snapshot_writers_use_distinct_temporary_names(self):
        destination = Path("layer12_rows_live_frontend.jsonl")
        first = unique_temporary_path(destination)
        second = unique_temporary_path(destination)
        self.assertNotEqual(first, second)
        self.assertEqual(first.parent, destination.parent)


def write_jsonl(path: Path, rows: list[dict]) -> None:
    path.write_text("".join(json.dumps(x, ensure_ascii=False) + "\n" for x in rows), encoding="utf-8")


class IncrementalStage12Tests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.inputs = self.root / "inputs"; self.inputs.mkdir()
        self.images = self.root / "images"; self.images.mkdir()
        self.a1 = self.images / "a1.png"; self.a2 = self.images / "a2.png"
        self.t1 = self.images / "t1.png"; self.t2 = self.images / "t2.png"; self.t3 = self.images / "t3.png"
        for path, color in ((self.a1,(255,0,0)),(self.a2,(0,255,0)),(self.t1,(255,0,0)),(self.t2,(0,0,255)),(self.t3,(0,255,0))):
            Image.new("RGB", (8,8), color).save(path)
        self.amazon_path = self.inputs / "amazon.jsonl"
        self.geek_path = self.inputs / "geek.jsonl"
        self.front_path = self.inputs / "front.jsonl"
        self.vectorizer = FakeVectorizer()
        self.pipeline = IncrementalStage12(
            self.root / "run", vectorizer=self.vectorizer, threshold=50,
            image_download_workers=4, vector_batch_size=3,
        )
        self.pipeline.configure(
            [SourceSpec("amazon", "AMAZON", "amazon_generic", self.amazon_path), SourceSpec("geek", "TEMU", "temu_geekbi", self.geek_path), SourceSpec("front", "TEMU", "temu_frontend", self.front_path)],
            [PathSpec("A", "amazon", "geek", "A"), PathSpec("B", "amazon", "front", "B")],
        )

    def tearDown(self):
        self.pipeline.close(); self.temp.cleanup()

    def amazon(self, asin: str, title: str, image: Path) -> dict:
        return {"asin":asin,"title":title,"product_url":f"https://amazon.example/{asin}","main_image_url":"https://images.example/a.png","local_image_path":str(image),"best_seller_evidence":{"qualified":True}}

    def geek(self, gid: str, title: str, image: Path) -> dict:
        return {"goodsId":gid,"goodsNameEn":title,"linkUrl":f"https://geek.example/{gid}","thumbnail":"https://images.example/t.png","local_image_path":str(image),"sku":"[]","observed_at":"volatile"}

    def front(self, gid: str, title: str, image: Path) -> dict:
        return {"goods_id":gid,"title":title,"product_url":f"https://temu.example/{gid}","main_image_url":"https://images.example/t.png","local_image_path":str(image),"observed_at":"volatile"}

    def ingest_all(self):
        return [self.pipeline.ingest_source(x) for x in ("amazon","geek","front")]

    def drain(self):
        for _ in range(20):
            result = self.pipeline.process_batch(10)
            if not result["selected"]: break

    def test_repeat_input_does_not_recompute(self):
        write_jsonl(self.amazon_path,[self.amazon("A1","red floral shower curtain",self.a1)])
        write_jsonl(self.geek_path,[self.geek("T1","red floral shower curtain",self.t1)])
        write_jsonl(self.front_path,[self.front("T1","red floral shower curtain",self.t1)])
        self.ingest_all(); self.drain(); calls = self.vectorizer.calls
        results = self.ingest_all(); self.drain()
        self.assertTrue(all(x["reused_snapshot"] == 1 for x in results))
        self.assertEqual(calls, self.vectorizer.calls)
        self.assertEqual([g["actual_pairs"] for g in self.pipeline.status()["groups"]], [1,1])

    def test_same_snapshot_recovers_from_temporary_invalid_json(self):
        write_jsonl(self.amazon_path,[self.amazon('A1','curtain',self.a1)])
        original=self.amazon_path.read_bytes()
        self.pipeline.ingest_source('amazon')
        self.amazon_path.write_text('{broken',encoding='utf-8')
        with self.assertRaises(Exception): self.pipeline.ingest_source('amazon')
        self.amazon_path.write_bytes(original)
        result=self.pipeline.ingest_source('amazon')
        self.assertEqual(result['reused_snapshot'],1)
        self.assertIsNone(self.pipeline.con.execute("SELECT last_error FROM sources WHERE source_key='amazon'").fetchone()[0])

    def test_replace_snapshot_removes_stale_products_and_pairs(self):
        # A complete source snapshot must not retain products from a previous
        # input file that reused the same source key.
        self.pipeline.close()
        self.pipeline = IncrementalStage12(self.root / "run_replace", vectorizer=self.vectorizer, threshold=50)
        self.pipeline.configure(
            [SourceSpec("amazon", "AMAZON", "amazon_generic", self.amazon_path, "replace"),
             SourceSpec("front", "TEMU", "temu_frontend", self.front_path, "replace")],
            [PathSpec("B", "amazon", "front", "B")],
        )
        write_jsonl(self.amazon_path, [self.amazon("A1", "curtain", self.a1), self.amazon("STALE", "curtain", self.a2)])
        write_jsonl(self.front_path, [self.front("T1", "curtain", self.t1)])
        self.pipeline.ingest_source("amazon"); self.pipeline.ingest_source("front"); self.drain()
        self.assertEqual(self.pipeline.con.execute("SELECT count(*) FROM pairs").fetchone()[0], 2)
        write_jsonl(self.amazon_path, [self.amazon("A1", "curtain", self.a1)])
        result = self.pipeline.ingest_source("amazon")
        self.assertEqual(result["removed"], 1)
        self.assertEqual(result["pairs_removed"], 1)
        self.assertEqual(self.pipeline.con.execute("SELECT count(*) FROM products WHERE source_key='amazon'").fetchone()[0], 1)
        self.assertEqual(self.pipeline.con.execute("SELECT count(*) FROM pairs").fetchone()[0], 1)

    def test_cache_seed_reuses_only_images_and_vectors_not_products_or_pairs(self):
        vector = np.array([1.0, 2.0], dtype=np.float32)
        with self.pipeline.con:
            self.pipeline.con.execute(
                "INSERT INTO images VALUES(?,?,?,?,?,?,?,?)",
                ("image-key", "https://images.example/seed.png", "content-hash", str(self.a1), "READY", 0, None, "now"),
            )
            self.pipeline.con.execute(
                "INSERT INTO vectors VALUES(?,?,?,?,?,?,?)",
                ("content-hash", self.vectorizer.revision, 2, vector.tobytes(), "READY", None, "now"),
            )
        target = IncrementalStage12(self.root / "seeded", vectorizer=FakeVectorizer(), threshold=50)
        try:
            result = target.seed_cache_from_database(self.pipeline.db_path)
            self.assertGreater(result["images"], 0)
            self.assertGreater(result["vectors"], 0)
            self.assertEqual(target.con.execute("SELECT count(*) FROM products").fetchone()[0], 0)
            self.assertEqual(target.con.execute("SELECT count(*) FROM pairs").fetchone()[0], 0)
        finally:
            target.close()

    def test_new_temu_only_adds_corresponding_pairs(self):
        write_jsonl(self.amazon_path,[self.amazon("A1","curtain",self.a1),self.amazon("A2","curtain",self.a2)])
        write_jsonl(self.geek_path,[self.geek("T1","curtain",self.t1)])
        write_jsonl(self.front_path,[self.front("T1","curtain",self.t1)])
        self.ingest_all(); self.drain()
        before = self.pipeline.status()["counters"]["pairs_scheduled"]
        write_jsonl(self.front_path,[self.front("T1","curtain",self.t1),self.front("T2","curtain",self.t2)])
        self.pipeline.ingest_source("front"); self.drain()
        after = self.pipeline.status()["counters"]["pairs_scheduled"]
        self.assertEqual(after-before, 2)
        groups = {x["path_key"]:x for x in self.pipeline.status()["groups"]}
        self.assertEqual(groups["A"]["actual_pairs"],2); self.assertEqual(groups["B"]["actual_pairs"],4)

    def test_change_one_product_updates_only_affected_source_path(self):
        write_jsonl(self.amazon_path,[self.amazon("A1","curtain",self.a1),self.amazon("A2","curtain",self.a2)])
        write_jsonl(self.geek_path,[self.geek("T1","curtain",self.t1)])
        write_jsonl(self.front_path,[self.front("T1","curtain",self.t1)])
        self.ingest_all(); self.drain(); before=self.pipeline.status()["counters"]["pairs_scheduled"]
        write_jsonl(self.front_path,[self.front("T1","changed title",self.t2)])
        self.pipeline.ingest_source("front"); self.drain()
        self.assertEqual(self.pipeline.status()["counters"]["pairs_scheduled"]-before,2)
        a = self.pipeline.con.execute("SELECT temu_version FROM pairs WHERE path_key='A' AND goods_id='T1'").fetchone()[0]
        b = self.pipeline.con.execute("SELECT temu_version FROM pairs WHERE path_key='B' AND goods_id='T1'").fetchone()[0]
        self.assertNotEqual(a,b)

    def test_restart_matches_uninterrupted_and_malformed_snapshot_is_retained(self):
        write_jsonl(self.amazon_path,[self.amazon("A1","curtain",self.a1)])
        write_jsonl(self.geek_path,[self.geek("T1","curtain",self.t1),self.geek("T2","curtain",self.t2)])
        write_jsonl(self.front_path,[self.front("T1","curtain",self.t1)])
        self.ingest_all(); self.pipeline.process_batch(1)
        self.pipeline.close()
        self.pipeline = IncrementalStage12(self.root/"run",vectorizer=self.vectorizer,threshold=50)
        self.pipeline.configure([SourceSpec("amazon","AMAZON","amazon_generic",self.amazon_path),SourceSpec("geek","TEMU","temu_geekbi",self.geek_path),SourceSpec("front","TEMU","temu_frontend",self.front_path)],[PathSpec("A","amazon","geek","A"),PathSpec("B","amazon","front","B")])
        self.drain(); self.assertEqual(sum(g["actual_pairs"] for g in self.pipeline.status()["groups"]),3)
        self.front_path.write_text('{"goods_id":"BROKEN"',encoding="utf-8")
        with self.assertRaises(RuntimeError): self.pipeline.ingest_source("front")
        self.assertEqual(self.pipeline.con.execute("SELECT count(*) FROM products WHERE source_key='front'").fetchone()[0],1)

    def test_missing_fields_are_unknown_not_hard_conflict_and_sources_stay_separate(self):
        record = normalize_record({"asin":"SIM1","title":"minimal","product_url":"https://x","main_image_url":"https://y"},platform="AMAZON",source_key="amazon_front",adapter="amazon_generic")
        self.assertIsNone(record["best_seller_evidence"]["qualified"])
        write_jsonl(self.amazon_path,[self.amazon("A1","shower curtain",self.a1)])
        write_jsonl(self.geek_path,[self.geek("T1","polyester floral shower curtain",self.t1)])
        write_jsonl(self.front_path,[self.front("T1","item",self.t1)])
        self.ingest_all(); self.drain()
        rows=self.pipeline.con.execute("SELECT path_key,stage1_status FROM pairs ORDER BY path_key").fetchall()
        self.assertEqual(rows[1]["stage1_status"],"TEXT_INSUFFICIENT")
        self.assertEqual(len(rows),2)

    def test_cartesian_reconciliation_and_candidate_materialization(self):
        write_jsonl(self.amazon_path,[self.amazon("A1","curtain",self.a1),self.amazon("A2","curtain",self.a2)])
        write_jsonl(self.geek_path,[self.geek("T1","curtain",self.t1),self.geek("T2","curtain",self.t2)])
        write_jsonl(self.front_path,[self.front("T1","curtain",self.t1),self.front("T3","curtain",self.t3)])
        self.ingest_all(); self.drain(); status=self.pipeline.status()
        for group in status["groups"]:
            self.assertEqual(group["expected_pairs"],group["actual_pairs"])
            self.assertEqual(sum(group["states"].values()),group["actual_pairs"])
        outputs=self.pipeline.materialize_candidates()
        self.assertTrue(Path(outputs["A"]).exists()); self.assertTrue(Path(outputs["B"]).exists())
        comparison=self.pipeline.comparison_rows()
        self.assertEqual({x["goods_id"] for x in comparison["only"]},{"T2","T3"})
        self.assertEqual(len(comparison["common"]),2)
        export = self.pipeline.materialize_layer12_export("B")
        exported_rows = [json.loads(x) for x in Path(export["rows"]).read_text(encoding="utf-8").splitlines()]
        exported_summary = json.loads(Path(export["summary"]).read_text(encoding="utf-8"))
        self.assertEqual(len(exported_rows), 4)
        self.assertEqual(exported_summary["expected_pairs"], 4)
        self.assertEqual(exported_summary["api_requests"], 0)

    def test_identical_candidate_export_preserves_timestamp_but_real_bytes_update(self):
        payload=json.dumps({'asin':'A1','goods_id':'T1','title':'curtain'})
        self.pipeline.con.execute('INSERT INTO candidates VALUES(?,?,?,?,?,?)',
                                  ('A','A1','T1','test-revision',payload,'test-time'))
        self.pipeline.con.commit()
        outputs=self.pipeline.materialize_candidates()
        destination=Path(outputs['A'])
        self.assertEqual(outputs['A_count'],1)
        before=destination.read_bytes()
        os.utime(destination,ns=(1600000000000000000,1600000000000000000))
        previous=destination.stat().st_mtime_ns
        self.pipeline.materialize_candidates()
        self.assertEqual(destination.stat().st_mtime_ns,previous)
        payload=self.pipeline.con.execute("SELECT payload_json FROM candidates WHERE path_key='A'").fetchone()[0]
        updated=payload.replace('curtain','curtaiX')
        self.assertNotEqual(payload,updated)
        self.assertEqual(len(payload.encode('utf-8')),len(updated.encode('utf-8')))
        self.pipeline.con.execute("UPDATE candidates SET payload_json=? WHERE path_key='A'",(updated,))
        self.pipeline.con.commit()
        self.pipeline.materialize_candidates()
        self.assertNotEqual(destination.read_bytes(),before)
        self.assertNotEqual(destination.stat().st_mtime_ns,previous)
        self.assertEqual(destination.read_bytes(),(updated+'\n').encode('utf-8'))

    def test_snapshot_byte_comparison_ignores_equal_size_and_equal_mtime(self):
        first=self.root/'left.jsonl';second=self.root/'right.jsonl'
        first.write_bytes(b'original');second.write_bytes(b'original')
        stamp=1600000000000000000
        for path in [first,second]:os.utime(path,ns=(stamp,stamp))
        self.assertTrue(same_snapshot_bytes(first,second))
        second.write_bytes(b'modified');os.utime(second,ns=(stamp,stamp))
        self.assertFalse(same_snapshot_bytes(first,second))

    def test_layer12_export_preserves_join_values_order_and_orphans(self):
        write_jsonl(self.amazon_path, [self.amazon("A2", "second", self.a2), self.amazon("A1", "first", self.a1)])
        write_jsonl(self.front_path, [self.front("T2", "blue", self.t2), self.front("T1", "red", self.t1)])
        self.pipeline.ingest_source("amazon"); self.pipeline.ingest_source("front")
        states = ["TEXT_HARD_EXCLUDE", "TEXT_INSUFFICIENT", "TEXT_PASS", None]
        with self.pipeline.con:
            for i, (asin, goods_id) in enumerate((("A1", "T1"), ("A1", "T2"), ("A2", "T1"), ("A2", "T2"))):
                self.pipeline.con.execute("""UPDATE pairs SET stage1_status=?,stage1_json=?,
                    vector_score=?,status=?,last_error=?,pair_revision=?
                    WHERE path_key='B' AND asin=? AND goods_id=?""",
                    (states[i], json.dumps({"hard_conflicts": [{"reason": "数量冲突"}]}),
                     12.5 + i, "DONE", "diagnostic" if i == 2 else None, f"rev-{i}", asin, goods_id))
            # A saved orphan must remain excluded exactly as by the old JOIN.
            self.pipeline.con.execute("""INSERT INTO pairs SELECT path_key,asin,'MISSING',
                amazon_version,temu_version,pair_revision,stage1_status,stage1_json,vector_score,
                status,amazon_content_hash,temu_content_hash,retry_count,last_error,
                processing_seconds,updated_at FROM pairs WHERE path_key='B' AND asin='A1' AND goods_id='T1'""")
        old_rows = self.pipeline.con.execute("""SELECT p.*,a.record_json amazon_json,t.record_json temu_json
            FROM pairs p JOIN products a ON a.source_key='amazon' AND a.product_id=p.asin
            JOIN products t ON t.source_key='front' AND t.product_id=p.goods_id
            WHERE p.path_key='B' ORDER BY p.asin,p.goods_id""").fetchall()
        expected = []
        facts = ["数量冲突", "文字证据不足，未发现可确认的硬冲突，继续图片向量层",
                 "未发现文字或规格硬冲突，普通规格差异不作为排除", "第一层待处理"]
        for i, row in enumerate(old_rows):
            a = json.loads(row["amazon_json"]); t = json.loads(row["temu_json"])
            expected.append({"序号": i + 1, "asin": row["asin"], "goods_id": row["goods_id"],
                "amazon_title": a.get("title"), "temu_title": t.get("title"),
                "amazon_image_url": a.get("main_image_url"), "temu_image_url": t.get("main_image_url"),
                "amazon_product_url": a.get("product_url"), "temu_product_url": t.get("product_url"),
                "stage1_text_fact": facts[i], "stage1_status": row["stage1_status"],
                "vector_score": row["vector_score"], "pair_status": row["status"],
                "last_error": row["last_error"], "pair_revision": row["pair_revision"]})
        real_loads = json.loads
        with patch("selection_workflow.incremental_stage12.json.loads", wraps=real_loads) as loads:
            export = self.pipeline.materialize_layer12_export("B")
        product_parses = [call for call in loads.call_args_list if '"platform"' in call.args[0]]
        self.assertEqual(len(product_parses), 4)  # Two products per source, once each.
        actual = [real_loads(line) for line in Path(export["rows"]).read_text(encoding="utf-8").splitlines()]
        self.assertEqual(actual, expected)
        self.assertEqual(export["row_count"], 4)
        summary = real_loads(Path(export["summary"]).read_text(encoding="utf-8"))
        self.assertEqual(summary["rows"], 4)
        self.assertEqual(summary["actual_pairs"], 5)

    def test_layer12_product_cache_and_pair_values_share_read_snapshot(self):
        write_jsonl(self.amazon_path, [self.amazon("A1", "original", self.a1)])
        write_jsonl(self.front_path, [self.front("T1", "original-temu", self.t1)])
        self.pipeline.ingest_source("amazon"); self.pipeline.ingest_source("front")
        writer = sqlite3.connect(self.pipeline.db_path)
        real_loads = json.loads
        changed = False
        def concurrent_update(value, *args, **kwargs):
            nonlocal changed
            record = real_loads(value, *args, **kwargs)
            if not changed:
                changed = True
                with writer:
                    writer.execute("UPDATE products SET record_json=? WHERE source_key='amazon'",
                                   (json.dumps({"title": "new-title"}),))
                    writer.execute("UPDATE pairs SET vector_score=99,status='NEW' WHERE path_key='B'")
            return record
        try:
            with patch("selection_workflow.incremental_stage12.json.loads", side_effect=concurrent_update):
                export = self.pipeline.materialize_layer12_export("B")
        finally:
            writer.close()
        row = real_loads(Path(export["rows"]).read_text(encoding="utf-8"))
        self.assertEqual(row["amazon_title"], "original")
        self.assertIsNone(row["vector_score"])
        self.assertEqual(row["pair_status"], "PENDING")
        self.assertFalse(self.pipeline.con.in_transaction)
        self.assertEqual(self.pipeline.con.execute("SELECT status FROM pairs WHERE path_key='B'").fetchone()[0], "NEW")
        # Caller-owned snapshot stays open; materialization must not commit it.
        self.pipeline.con.execute("BEGIN")
        self.pipeline.materialize_layer12_export("B", self.root / "caller-owned")
        self.assertTrue(self.pipeline.con.in_transaction)
        self.pipeline.con.rollback()

    def test_required_temu_proxy_waits_without_counting_fake_download_failure(self):
        self.pipeline.close()
        self.pipeline = IncrementalStage12(
            self.root / "run_proxy", vectorizer=self.vectorizer, threshold=50,
            require_temu_image_proxy=True,
        )
        self.pipeline.configure(
            [SourceSpec("amazon", "AMAZON", "amazon_generic", self.amazon_path), SourceSpec("front", "TEMU", "temu_frontend", self.front_path)],
            [PathSpec("B", "amazon", "front", "B")],
        )
        Image.new("RGB", (64, 64), (255, 0, 0)).save(self.a1)
        amazon = self.amazon("A1", "curtain", self.a1)
        temu = self.front("T1", "curtain", self.t1)
        temu.pop("local_image_path")
        write_jsonl(self.amazon_path, [amazon]); write_jsonl(self.front_path, [temu])
        self.pipeline.ingest_source("amazon"); self.pipeline.ingest_source("front")
        result = self.pipeline.process_batch(10)
        row = self.pipeline.con.execute("SELECT status,retry_count,last_error FROM pairs").fetchone()
        self.assertEqual(result["failed"], 1)
        self.assertEqual(row["status"], "WAIT_IMAGE_PROXY", row["last_error"])
        self.assertEqual(row["retry_count"], 0)
        self.assertEqual(self.pipeline.status()["image_failures"], 0)

    def test_images_are_prefetched_concurrently_and_vectors_are_batched(self):
        for path, color in ((self.a1, (255, 0, 0)), (self.t1, (255, 0, 0)),
                            (self.t2, (0, 0, 255)), (self.t3, (0, 255, 0))):
            Image.new("RGB", (64, 64), color).save(path)
        write_jsonl(self.amazon_path, [self.amazon("A1", "curtain", self.a1)])
        write_jsonl(self.geek_path, [self.geek("T1", "curtain", self.t1)])
        write_jsonl(self.front_path, [
            self.front("T1", "curtain", self.t1),
            self.front("T2", "curtain", self.t2),
            self.front("T3", "curtain", self.t3),
        ])
        self.ingest_all()
        result = self.pipeline.process_batch(10)
        self.assertEqual(result["selected"], 4)
        self.assertGreater(self.vectorizer.batch_calls, 0)
        self.assertTrue(all(size <= 3 for size in self.vectorizer.batch_sizes))
        self.assertIn("download_seconds", result)
        self.assertIn("vector_seconds", result)
        status = self.pipeline.status()
        self.assertEqual(status["image_download_workers"], 4)
        self.assertEqual(status["vector_batch_size"], 3)

    def test_parallel_text_gate_matches_serial_results(self):
        parallel = IncrementalStage12(
            self.root / "parallel_text", vectorizer=FakeVectorizer(), threshold=50,
            text_gate_workers=2,
        )
        try:
            inputs = [
                ("clear peva shower curtain", "frosted polyester shower curtain"),
                ("blue floral shower curtain", "blue floral shower curtain"),
            ] * 150
            expected = [evaluate_text_gate(left, right) for left, right in inputs]
            self.assertEqual(parallel._evaluate_text_gates(inputs), expected)
            self.assertEqual(parallel.status()["text_gate_workers"], 2)
        finally:
            parallel.close()


if __name__ == "__main__": unittest.main()
