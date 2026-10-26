"""Incremental, source-isolated stage 1/2 pipeline for category matching.

The collector remains the owner of its snapshots.  This module only reads a
complete snapshot, normalises records locally and commits them to SQLite after
the entire input has parsed successfully.  Product identity and evidence
version are intentionally separate: ASIN/goods_id identifies the product while
the evidence hash controls selective recomputation.
"""
from __future__ import annotations

import hashlib
import inspect
import io
import json
import os
import sqlite3
import struct
import time
import urllib.request
import uuid
from concurrent.futures import CancelledError, ProcessPoolExecutor, ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from functools import lru_cache
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Protocol

import numpy as np
from PIL import Image

from .text_gate_registry import GATES, evaluate as evaluate_text_gate
from .image_transport import ImageTransport, MAX_IMAGE_CONNECTIONS
from .image_retry import ImageRetryPending,ImageNeedsAction


from .matching_views import VECTOR_REVISION, image_views, similarity_scores, validate_features
DEFAULT_THRESHOLD = 50.0
VOLATILE_FIELDS = {
    "observed_at", "updated_at", "retrievedAt", "first_seen_ordinal",
    "first_seen_round", "_sourcePage", "page", "export_time", "capture_time",
}


def same_snapshot_bytes(first: Path, second: Path) -> bool:
    """Compare actual bytes, never a size/mtime or process-wide cached result."""
    try:
        if first.stat().st_size != second.stat().st_size:
            return False
        with first.open('rb') as left, second.open('rb') as right:
            while True:
                chunk = left.read(1024 * 1024)
                if chunk != right.read(1024 * 1024):
                    return False
                if not chunk:
                    return True
    except OSError:
        return False


@lru_cache(maxsize=32)
def _category_snapshot(content: str) -> dict[str, Any]:
    """Validate one immutable serialized contract, independent of live files."""
    from selection_automation import rules
    category = json.loads(content)
    rules.validate(category)
    return category


def _evaluate_text_gate_args(values: tuple[str, ...]) -> dict[str, Any]:
    """Pickle-safe worker entry; declarative batches carry immutable content."""
    if len(values) == 2:  # Compatibility for direct callers of the old private batch API.
        return evaluate_text_gate("shower_curtain_v2", values[0], values[1])
    if len(values) == 4:
        from .text_attribute_cache import install_attribute_cache
        install_attribute_cache()
        from selection_automation.rules import text_gate
        return text_gate(_category_snapshot(values[3]), values[1], values[2])
    return evaluate_text_gate(values[0], values[1], values[2])


def unique_temporary_path(destination: Path) -> Path:
    """Avoid cross-process collisions between watch, manual export and layer 3."""
    return destination.with_name(
        f".{destination.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp"
    )


class ImageProxyNotConfigured(RuntimeError):
    """A TEMU remote image is ready, but the required download proxy is absent."""


class ImageSourceMissing(RuntimeError):
    """The source has not supplied an image URL or local image yet."""


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def stable_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def replace_snapshot_file(source: Path, destination: Path, *, timeout_seconds: float = 2.0) -> None:
    """Allow brief Windows readers to close without hiding a persistent lock."""
    deadline = time.monotonic() + max(0.0, timeout_seconds)
    while True:
        try:
            os.replace(source, destination)
            return
        except PermissionError:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise
            time.sleep(min(0.05, remaining))


def digest(value: bytes | str) -> str:
    if isinstance(value, str):
        value = value.encode("utf-8")
    return hashlib.sha256(value).hexdigest()


def declarative_text_revision(category: dict) -> str:
    """The local cache must include the frozen checkboxes actually executed."""
    from selection_automation import rules,match_criteria
    payload={"stage1":category["stage1"],"snapshot_protocol":"declarative-content-v1",
        "ordinary_variants":sorted(set(category['stage3'].get('variant_fields',[]))-set(category['stage3']['modules'])),
        "engine":digest(Path(inspect.getsourcefile(rules.text_gate)).read_bytes())}
    if match_criteria.enabled(category):
        payload['selected_text']=[{key:item.get(key) for key in
            ('id','label','feature_id','source_labels','observations','comparison')}
            for item in match_criteria.selected(category,'text')]
        payload['selected_text_engine']=digest(Path(inspect.getsourcefile(match_criteria.text_gate)).read_bytes())
    return digest(stable_json(payload))


def local_category_contract(category: dict) -> str:
    """Freeze local decisions without treating visual-provider setup as a rule edit.

    The immutable batch snapshot is still used for every text evaluation. Only
    category identity, local rules and the visual fields actually consulted by
    the text gate participate in this running-pipeline drift guard. Provider,
    concurrency and paid-result checkpoint changes cannot alter those decisions.
    """
    return digest(stable_json({
        "category_key": category["category_key"],
        "category_name_zh": category["category_name_zh"],
        "stage12_output_dir": category.get("stage12_output_dir", "stage12"),
        "stage1": category["stage1"],
        "stage2": category["stage2"],
        "text_revision": declarative_text_revision(category),
    }))


def without_volatile(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: without_volatile(v) for k, v in sorted(value.items()) if k not in VOLATILE_FIELDS}
    if isinstance(value, list):
        return [without_volatile(v) for v in value]
    return value


def unknown(value: Any) -> Any:
    return None if value in (None, "", [], {}) else value


def first(record: dict[str, Any], *names: str) -> Any:
    for name in names:
        if name in record and record[name] not in (None, ""):
            return record[name]
    return None


def _parse_sku(value: Any) -> tuple[Any, str, str | None]:
    if value in (None, ""):
        return None, "UNKNOWN", None
    if isinstance(value, (list, dict)):
        return value, "PARSED", None
    try:
        return json.loads(str(value)), "PARSED", None
    except Exception as exc:
        return value, "FAILED", str(exc)


def _field_map(adapter: str) -> dict[str, list[str]]:
    maps = {
        "amazon_facts": {
            "product_id": ["asin"], "title": ["title"], "url": ["product_url"],
            "image": ["main_image_url"], "specs": ["fields"],
        },
        "amazon_generic": {
            "product_id": ["asin", "ASIN", "商品ID", "product_id"],
            "title": ["title", "标题", "product_title"],
            "url": ["product_url", "url", "链接"],
            "image": ["main_image_url", "image_url", "主图", "thumbnail_url"],
            "specs": ["specifications", "specs", "规格", "fields"],
        },
        "temu_geekbi": {
            "product_id": ["goodsId", "goods_id", "商品ID"],
            "title": ["goodsNameEn", "goodsName", "goodsNameCn", "title"],
            "url": ["productUrl", "product_url", "linkUrl", "url"],
            "image": ["thumbnail", "main_image_url", "image_url"],
            "specs": ["sku"],
        },
        "temu_frontend": {
            "product_id": ["goods_id", "goodsId", "商品ID"],
            "title": ["title", "goodsName", "card_text"],
            "url": ["product_url", "url"],
            "image": ["main_image_url", "thumbnail_url", "image_url"],
            "specs": ["specifications", "specs", "sku"],
        },
    }
    return maps[adapter]


def normalize_record(
    raw: dict[str, Any], *, platform: str, source_key: str, adapter: str,
    source_path: Path | None = None,
) -> dict[str, Any]:
    mapping = _field_map(adapter)
    product_id = str(first(raw, *mapping["product_id"]) or "").strip()
    if not product_id:
        raise ValueError(f"{source_key} record missing product identity")
    title = str(first(raw, *mapping["title"]) or "").strip()
    url = str(first(raw, *mapping["url"]) or "").strip() or None
    image_url = str(first(raw, *mapping["image"]) or "").strip() or None
    local_image = unknown(raw.get("local_image_path"))
    if not local_image and source_path and adapter == "amazon_facts":
        candidate = source_path.parent / "image_cache" / f"amazon_{product_id}.jpg"
        if candidate.exists():
            local_image = str(candidate)
    specs_value = first(raw, *mapping["specs"])
    sku_value, sku_status, sku_error = _parse_sku(raw.get("sku") if adapter == "temu_geekbi" else specs_value)
    if adapter == "amazon_facts":
        fields = raw.get("fields") or {}
        gate_text = raw.get("gate_text") or " | ".join(f"{k}:{stable_json(v) if isinstance(v,(dict,list)) else v}" for k, v in fields.items() if v not in (None,"",[],{}))
        best_seller = raw.get("best_seller_evidence") or {"qualified": None, "status": "PENDING"}
    elif adapter == "temu_geekbi":
        fields = {
            "title_original": unknown(raw.get("goodsName")), "title_chinese": unknown(raw.get("goodsNameCn")),
            "title_english": unknown(raw.get("goodsNameEn")), "category_path": unknown(raw.get("catItems")),
            "sku": sku_value, "status": unknown(raw.get("status")),
        }
        gate_text = " | ".join(f"{k}:{stable_json(v) if isinstance(v,(dict,list)) else v}" for k, v in fields.items() if v not in (None,"",[],{}))
        best_seller = None
    else:
        fields = {
            "title": title or None, "specifications": unknown(specs_value),
            "card_text": unknown(raw.get("card_text")), "material": unknown(raw.get("material")),
            "size": unknown(raw.get("size")), "sku": sku_value,
        }
        gate_text = " | ".join(f"{k}:{stable_json(v) if isinstance(v,(dict,list)) else v}" for k, v in fields.items() if v not in (None,"",[],{}))
        best_seller = raw.get("best_seller_evidence") if platform == "AMAZON" else None
        if platform == "AMAZON" and best_seller is None:
            best_seller = {"qualified": None, "status": "PENDING", "reason": "source has no Best Seller evidence"}
    observed_at = first(raw, "observed_at", "retrievedAt", "updateTime", "createTime")
    normalized = {
        "schema_version": "incremental-product-facts-v1",
        "platform": platform, "source_key": source_key, "product_id": product_id,
        "title": title or None, "product_url": url, "main_image_url": image_url,
        "local_image_path": local_image, "fields": fields, "gate_text": gate_text,
        "sku_parse_status": sku_status, "sku_parse_error": sku_error,
        "best_seller_evidence": best_seller,
        "market": first(raw, "market", "site", "siteId") or ("US" if platform == "AMAZON" else "TEMU_US"),
        "observed_at": observed_at, "source_record_id": product_id,
        "field_mapping": mapping, "adapter": adapter,
        "raw_record": raw,
    }
    evidence = {
        "platform": platform, "source_key": source_key, "product_id": product_id,
        "title": normalized["title"], "product_url": url, "main_image_url": image_url,
        "local_image_path": local_image, "fields": without_volatile(fields),
        "gate_text": gate_text, "best_seller_evidence": best_seller,
        "sku_parse_status": sku_status,
    }
    normalized["evidence_hash"] = digest(stable_json(evidence))
    normalized["raw_record_hash"] = digest(stable_json(without_volatile(raw)))
    return normalized


def read_complete_jsonl(path: Path, retries: int = 3, delay: float = 0.15) -> tuple[list[dict[str, Any]], str]:
    last: Exception | None = None
    for _ in range(max(1, retries)):
        try:
            data = path.read_bytes()
            if not data and path.exists():
                return [], digest(data)
            rows = []
            for number, line in enumerate(data.decode("utf-8-sig").splitlines(), 1):
                if not line.strip():
                    continue
                value = json.loads(line)
                if not isinstance(value, dict):
                    raise ValueError(f"line {number} is not an object")
                if value.get("_recordType") in {"metadata", "category_validation"}:
                    continue
                rows.append(value)
            return rows, digest(data)
        except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
            last = exc
            time.sleep(delay)
    raise RuntimeError(f"snapshot read failed; previous committed data retained: {path}: {last}")


class Vectorizer(Protocol):
    revision: str
    def encode(self, image_path: Path) -> np.ndarray: ...


class TorchResNet50Vectorizer:
    revision = VECTOR_REVISION
    def __init__(self) -> None:
        self._model = None
        self._preprocess = None

    def _load(self) -> None:
        if self._model is not None:
            return
        import torch
        from torchvision.models import ResNet50_Weights, resnet50
        # Leave CPU capacity for the collection browsers, local drivers and UI.
        # An explicit deployment setting still takes precedence.
        requested_threads = int(os.environ.get("STAGE12_TORCH_THREADS") or min(4, max(1, (os.cpu_count() or 2) // 2)))
        torch.set_num_threads(max(1, requested_threads))
        weights = ResNet50_Weights.IMAGENET1K_V2
        self._preprocess = weights.transforms()
        self._model = resnet50(weights=weights)
        self._model.fc = torch.nn.Identity()
        self._model.eval()

    def encode(self, image_path: Path) -> np.ndarray:
        return self.encode_many([image_path])[0]

    def encode_many(self, image_paths: list[Path]) -> list[np.ndarray]:
        if not image_paths:
            return []
        self._load()
        import torch
        values = []
        tensors = []
        with torch.inference_mode():
            # Stream the three views instead of retaining a whole image batch.
            # Eight views also bound ResNet activation memory while browsers run.
            for image_path in image_paths:
                with Image.open(image_path) as image:
                    for view in image_views(image):
                        tensors.append(self._preprocess(view))
                        if len(tensors) == 8:
                            values.append(self._model(torch.stack(tensors)).cpu().numpy().astype(np.float32))
                            tensors.clear()
            if tensors:
                values.append(self._model(torch.stack(tensors)).cpu().numpy().astype(np.float32))
        joined = np.concatenate(values).reshape(len(image_paths),-1)
        return [row for row in joined]


@dataclass
class SourceSpec:
    key: str
    platform: str
    adapter: str
    path: Path
    # ``replace`` means the input is a complete snapshot.  IDs that are no
    # longer present must be removed from the active product/pair set.  Keep
    # ``upsert`` available for append-only feeds.
    snapshot_mode: str = "upsert"


@dataclass
class PathSpec:
    key: str
    amazon_source: str
    temu_source: str
    label: str


class IncrementalStage12:
    def __init__(
        self, root: Path, *, vectorizer: Vectorizer | None = None,
        threshold: float = DEFAULT_THRESHOLD, max_image_retries: int = 3,
        temu_image_proxy_url: str | None = None,
        require_temu_image_proxy: bool = False,
        image_download_timeout: float = 30.0,
        image_download_workers: int = 1,
        vector_batch_size: int = 1,
        text_gate_workers: int = 1,
        text_gate_name: str = "shower_curtain_v2",
        rule_version: str = "shower-curtain-three-layer-v3.1-module-facts",
        stage12_rule_version: str | None = None,
    ) -> None:
        if isinstance(threshold, bool) or not np.isfinite(float(threshold)) or not 0 <= float(threshold) <= 100:
            raise ValueError("candidate threshold must be finite and 0..100")
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)
        self.cache = self.root / "image_cache"
        self.cache.mkdir(exist_ok=True)
        self.db_path = self.root / "stage12.sqlite3"
        self.con = sqlite3.connect(self.db_path, timeout=30)
        self.con.row_factory = sqlite3.Row
        self.con.execute("PRAGMA busy_timeout=30000")
        self.con.execute("PRAGMA journal_mode=WAL")
        self.con.execute("PRAGMA foreign_keys=ON")
        self.vectorizer = vectorizer or TorchResNet50Vectorizer()
        self.threshold = float(threshold)
        self.max_image_retries = max_image_retries
        self.temu_image_proxy_url = str(temu_image_proxy_url or "").strip() or None
        self.require_temu_image_proxy = bool(require_temu_image_proxy)
        self.image_download_timeout = float(image_download_timeout)
        self.image_download_workers = min(MAX_IMAGE_CONNECTIONS,max(1, int(image_download_workers)))
        self._image_transport = ImageTransport(self.image_download_timeout)
        self.vector_batch_size = max(1, int(vector_batch_size))
        # Older saved configs requested one process for every logical CPU.
        # Keep those configs usable without recreating that memory pressure.
        self.text_gate_workers = min(4, max(1, int(text_gate_workers)))
        self.text_gate_name = str(text_gate_name)
        self.rule_version = str(rule_version)
        # Local processing versions are separate from the identity carried by
        # already-paid visual results. Reclassifying unchanged inputs must not
        # change their historical pair_revision.
        self.stage12_rule_version = str(stage12_rule_version or rule_version)
        self._frozen_category_digest = None
        self._declarative_snapshot: str | None = None
        if self.text_gate_name.startswith("declarative:"):
            from selection_automation import rules
            gate_file = inspect.getsourcefile(rules.text_gate)
            category_bytes = Path(self.text_gate_name.split(":", 1)[1]).read_bytes()
            self._declarative_snapshot = category_bytes.decode("utf-8")
            category = _category_snapshot(self._declarative_snapshot)
            self._frozen_category_digest = local_category_contract(category)
            self._text_gate_revision = declarative_text_revision(category)
        else:
            gate_file = inspect.getsourcefile(GATES[self.text_gate_name])
            from selection_automation import rules
            context_file=inspect.getsourcefile(rules.uncertain_alias_context)
            self._text_gate_revision = digest(stable_json({'gate':digest(Path(gate_file).read_bytes()) if gate_file else self.text_gate_name,
                'context':digest(Path(context_file).read_bytes()) if context_file else 'context-v1'}))
        self._checked_processing_contract: str | None = None
        self._text_gate_pool: ProcessPoolExecutor | None = None
        self._init_schema()
        from .image_retry import ImageRetryQueue
        self.image_retry=ImageRetryQueue(self.con)
        self._corrupt_cache_assets={row[0] for row in self.con.execute(
            "SELECT DISTINCT image_key FROM image_retry_history WHERE event='CORRUPT_CACHE_RETRY_SCHEDULED'")}
        self.stop_requested=lambda:False
        self.image_retry_scope=None
        self.image_pair_scope=None

    def close(self) -> None:
        self._image_transport.close()
        if self._text_gate_pool is not None:
            if self.stop_requested():
                self._cancel_text_gate_work()
            else:
                self._text_gate_pool.shutdown(wait=True, cancel_futures=True)
                self._text_gate_pool = None
        self.con.close()

    def _cancel_text_gate_work(self, futures=()) -> None:
        """Cancel only this executor's queued work; do not wait for a whole batch."""
        for future in futures:
            future.cancel()
        pool, self._text_gate_pool = self._text_gate_pool, None
        if pool is not None:
            # Python 3.12 has no public terminate_workers API. Already-running
            # single gate calls can finish; bounded dispatch prevents the rest
            # of the batch from having been queued behind them.
            pool.shutdown(wait=False, cancel_futures=True)

    def _evaluate_text_gates(self, inputs: list[tuple[str, ...]]) -> list[dict[str, Any]]:
        from concurrent.futures import CancelledError, FIRST_COMPLETED, wait

        def check_stop(futures=()):
            if self.stop_requested():
                self._cancel_text_gate_work(futures)
                raise CancelledError("Task stopped during text classification")

        check_stop()
        if self.text_gate_workers <= 1 or len(inputs) < 256:
            results = []
            for values in inputs:
                check_stop()
                results.append(_evaluate_text_gate_args(values))
            check_stop()
            return results
        if self._text_gate_pool is None:
            self._text_gate_pool = ProcessPoolExecutor(max_workers=self.text_gate_workers)
        pool = self._text_gate_pool
        results = [None] * len(inputs)
        pending = {}
        next_index = 0

        def fill_available():
            nonlocal next_index
            # Submit at most one single-item call per worker. Executor.map
            # eagerly queues every chunk, which cannot be revoked on Stop.
            while next_index < len(inputs) and len(pending) < self.text_gate_workers:
                check_stop(pending)
                pending[pool.submit(_evaluate_text_gate_args, inputs[next_index])] = next_index
                next_index += 1

        try:
            fill_available()
            while pending:
                check_stop(pending)
                done, _ = wait(tuple(pending), timeout=0.05, return_when=FIRST_COMPLETED)
                check_stop(pending)
                for future in done:
                    results[pending.pop(future)] = future.result()
                fill_available()
            check_stop()
            return results
        except BaseException:
            self._cancel_text_gate_work(pending)
            raise

    def _init_schema(self) -> None:
        self.con.executescript("""
        CREATE TABLE IF NOT EXISTS sources(
          source_key TEXT PRIMARY KEY, platform TEXT NOT NULL, adapter TEXT NOT NULL,
          input_path TEXT NOT NULL, snapshot_mode TEXT NOT NULL DEFAULT 'upsert',
          last_snapshot_hash TEXT, last_success_at TEXT,
          last_error TEXT, received_records INTEGER NOT NULL DEFAULT 0);
        CREATE TABLE IF NOT EXISTS paths(
          path_key TEXT PRIMARY KEY, amazon_source TEXT NOT NULL, temu_source TEXT NOT NULL,
          label TEXT NOT NULL, FOREIGN KEY(amazon_source) REFERENCES sources(source_key),
          FOREIGN KEY(temu_source) REFERENCES sources(source_key));
        CREATE TABLE IF NOT EXISTS products(
          source_key TEXT NOT NULL, product_id TEXT NOT NULL, platform TEXT NOT NULL,
          evidence_hash TEXT NOT NULL, record_json TEXT NOT NULL, first_seen TEXT NOT NULL,
          last_seen TEXT NOT NULL, update_count INTEGER NOT NULL DEFAULT 0,
          PRIMARY KEY(source_key,product_id));
        CREATE TABLE IF NOT EXISTS product_versions(
          source_key TEXT NOT NULL, product_id TEXT NOT NULL, evidence_hash TEXT NOT NULL,
          record_json TEXT NOT NULL, created_at TEXT NOT NULL,
          PRIMARY KEY(source_key,product_id,evidence_hash));
        CREATE TABLE IF NOT EXISTS observations(
          source_key TEXT NOT NULL, product_id TEXT NOT NULL, raw_record_hash TEXT NOT NULL,
          observed_at TEXT, snapshot_hash TEXT NOT NULL, first_received_at TEXT NOT NULL,
          last_received_at TEXT NOT NULL, receive_count INTEGER NOT NULL DEFAULT 1,
          PRIMARY KEY(source_key,product_id,raw_record_hash));
        CREATE TABLE IF NOT EXISTS images(
          image_key TEXT PRIMARY KEY, source_ref TEXT NOT NULL, content_hash TEXT,
          local_path TEXT, status TEXT NOT NULL, retry_count INTEGER NOT NULL DEFAULT 0,
          last_error TEXT, updated_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS vectors(
          content_hash TEXT NOT NULL, vector_revision TEXT NOT NULL, dimension INTEGER,
          vector_blob BLOB, status TEXT NOT NULL, last_error TEXT, updated_at TEXT NOT NULL,
          PRIMARY KEY(content_hash,vector_revision));
        CREATE TABLE IF NOT EXISTS pairs(
          path_key TEXT NOT NULL, asin TEXT NOT NULL, goods_id TEXT NOT NULL,
          amazon_version TEXT NOT NULL, temu_version TEXT NOT NULL, pair_revision TEXT NOT NULL,
          stage1_status TEXT, stage1_json TEXT, vector_score REAL, status TEXT NOT NULL,
          amazon_content_hash TEXT, temu_content_hash TEXT, retry_count INTEGER NOT NULL DEFAULT 0,
          last_error TEXT, processing_seconds REAL NOT NULL DEFAULT 0, updated_at TEXT NOT NULL,
          PRIMARY KEY(path_key,asin,goods_id));
        CREATE TABLE IF NOT EXISTS candidates(
          path_key TEXT NOT NULL, asin TEXT NOT NULL, goods_id TEXT NOT NULL,
          pair_revision TEXT NOT NULL, payload_json TEXT NOT NULL, queued_at TEXT NOT NULL,
          PRIMARY KEY(path_key,asin,goods_id));
        CREATE TABLE IF NOT EXISTS counters(name TEXT PRIMARY KEY, value INTEGER NOT NULL DEFAULT 0);
        CREATE TABLE IF NOT EXISTS processing_metadata(key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE INDEX IF NOT EXISTS idx_pairs_pending ON pairs(status,updated_at);
        CREATE INDEX IF NOT EXISTS idx_pairs_status_gate ON pairs(status,stage1_status);
        CREATE INDEX IF NOT EXISTS idx_pairs_path_status_updated ON pairs(path_key,status,updated_at);
        """)
        # Existing databases predate snapshot replacement.  Migrate them in
        # place instead of silently continuing the old append-only behavior.
        source_columns = {row[1] for row in self.con.execute("PRAGMA table_info(sources)")}
        if "snapshot_mode" not in source_columns:
            self.con.execute("ALTER TABLE sources ADD COLUMN snapshot_mode TEXT NOT NULL DEFAULT 'upsert'")
        if self.con.execute("SELECT 1 FROM pairs WHERE status='WAIT_IMAGE' LIMIT 1").fetchone():
            self.con.execute("UPDATE pairs SET status='FAILED_RETRYABLE' WHERE status='WAIT_IMAGE'")
        self.con.commit()

    def configure(self, sources: Iterable[SourceSpec], paths: Iterable[PathSpec]) -> None:
        with self.con:
            for source in sources:
                self.con.execute("""INSERT INTO sources(source_key,platform,adapter,input_path,snapshot_mode)
                    VALUES(?,?,?,?,?) ON CONFLICT(source_key) DO UPDATE SET platform=excluded.platform,
                    adapter=excluded.adapter,input_path=excluded.input_path,snapshot_mode=excluded.snapshot_mode""",
                    (source.key, source.platform, source.adapter, str(source.path), source.snapshot_mode))
            for path in paths:
                self.con.execute("""INSERT INTO paths(path_key,amazon_source,temu_source,label)
                    VALUES(?,?,?,?) ON CONFLICT(path_key) DO UPDATE SET amazon_source=excluded.amazon_source,
                    temu_source=excluded.temu_source,label=excluded.label""",
                    (path.key, path.amazon_source, path.temu_source, path.label))

    def ensure_processing_contract(self) -> dict[str, Any]:
        """Requeue local decisions when rules change, even with identical inputs.

        Called only by ingestion/processing, never by snapshot or Excel reads.
        Legacy databases have no recorded contract, so their local decisions
        are checked once. Image/vector caches and historical pair revisions
        remain available; current candidate eligibility is rebuilt locally.
        """
        if self._frozen_category_digest is not None:
            current_category = _category_snapshot(Path(self.text_gate_name.split(":", 1)[1]).read_text("utf-8"))
            current = local_category_contract(current_category)
            if current != self._frozen_category_digest:
                raise ValueError("category rule content changed during this pipeline; stop and reopen before importing or processing new evidence")
        contract = stable_json({
            "schema": "stage12-processing-contract-v1",
            "text_gate": self.text_gate_name,
            "text_gate_revision": self._text_gate_revision,
            "rule_version": self.stage12_rule_version,
            "vector_revision": self.vectorizer.revision,
            "candidate_threshold": self.threshold,
            "cosine_policy": "float64-normalized-unrounded-threshold-v2",
        })
        if contract == self._checked_processing_contract:
            return {"changed": False, "rescheduled": 0}
        with self.con:
            self.con.execute("BEGIN IMMEDIATE")
            row = self.con.execute("SELECT value FROM processing_metadata WHERE key='stage12_contract'").fetchone()
            changed = row is None or row[0] != contract
            count = 0
            if changed:
                count = self.con.execute("SELECT count(*) FROM pairs").fetchone()[0]
                self.con.execute("DELETE FROM candidates")
                self.con.execute("""UPDATE pairs SET status='PENDING',stage1_status=NULL,
                    stage1_json=NULL,vector_score=NULL,retry_count=0,last_error=NULL,
                    processing_seconds=0,updated_at=?""", (utcnow(),))
                self.con.execute("INSERT OR REPLACE INTO processing_metadata VALUES('stage12_contract',?)", (contract,))
                self._inc("pairs_contract_rescheduled", count)
        self._checked_processing_contract = contract
        return {"changed": changed, "rescheduled": count, "pair_revisions_preserved": True}

    def seed_cache_from_database(self, source_db: Path) -> dict[str, int | str]:
        """Reuse ready image/vector records from another local stage-1/2 run.

        Product evidence and pair conclusions are deliberately not copied.  The
        only shared objects are immutable image bytes (by URL/content hash) and
        vectors (by content hash/model revision), so source-specific decisions
        remain isolated.
        """
        source_db = source_db.resolve()
        if source_db == self.db_path.resolve() or not source_db.exists():
            return {"source": str(source_db), "images": 0, "vectors": 0}
        source = sqlite3.connect(f"file:{source_db.as_posix()}?mode=ro", uri=True, timeout=30)
        source.row_factory = sqlite3.Row
        images_added = vectors_added = 0
        try:
            valid_images = []
            valid_hashes: set[str] = set()
            for row in source.execute("SELECT * FROM images WHERE status='READY' AND content_hash IS NOT NULL"):
                if row["local_path"] and Path(row["local_path"]).exists():
                    valid_images.append(tuple(row))
                    valid_hashes.add(row["content_hash"])
            with self.con:
                before = self.con.total_changes
                self.con.executemany("INSERT OR IGNORE INTO images VALUES(?,?,?,?,?,?,?,?)", valid_images)
                images_added = self.con.total_changes - before
                before = self.con.total_changes
                vector_rows = (
                    tuple(row) for row in source.execute(
                        "SELECT * FROM vectors WHERE status='READY' AND vector_revision=?",
                        (self.vectorizer.revision,),
                    ) if row["content_hash"] in valid_hashes
                )
                self.con.executemany("INSERT OR IGNORE INTO vectors VALUES(?,?,?,?,?,?,?)", vector_rows)
                vectors_added = self.con.total_changes - before
                self._inc("cache_seed_images", images_added)
                self._inc("cache_seed_vectors", vectors_added)
        finally:
            source.close()
        return {"source": str(source_db), "images": images_added, "vectors": vectors_added}

    def _inc(self, name: str, amount: int = 1) -> None:
        self.con.execute("INSERT INTO counters(name,value) VALUES(?,?) ON CONFLICT(name) DO UPDATE SET value=value+excluded.value", (name, amount))

    def ingest_source(self, source_key: str) -> dict[str, int | str]:
        self._raise_if_stopped()
        self.ensure_processing_contract()
        source = self.con.execute("SELECT * FROM sources WHERE source_key=?", (source_key,)).fetchone()
        if not source:
            raise KeyError(source_key)
        path = Path(source["input_path"])
        try:
            raws, snapshot_hash = read_complete_jsonl(path)
            if not raws and int(source["received_records"] or 0) > 0:
                raise RuntimeError("empty snapshot rejected because committed products already exist")
            by_id: dict[str, dict[str, Any]] = {}
            for raw in raws:
                self._raise_if_stopped()
                row = normalize_record(raw, platform=source["platform"], source_key=source_key,
                                       adapter=source["adapter"], source_path=path)
                by_id[row["product_id"]] = row
            current_ids = {
                row[0] for row in self.con.execute(
                    "SELECT product_id FROM products WHERE source_key=?", (source_key,)
                )
            }
            # A replacement snapshot must still reconcile if the bytes/hash
            # are unchanged but an older run left stale IDs in the database.
            # This is the repair path for the historical 113-vs-100 incident.
            if source["last_snapshot_hash"] == snapshot_hash and (
                str(source["snapshot_mode"] or "upsert").lower() != "replace"
                or current_ids == set(by_id)
            ):
                if source['last_error']:
                    with self.con:
                        self.con.execute('UPDATE sources SET last_error=NULL,last_success_at=? WHERE source_key=?', (utcnow(),source_key))
                return {"source": source_key, "received": len(by_id), "new": 0, "changed": 0,
                        "removed": 0, "reused_snapshot": 1}
        except CancelledError:
            raise
        except Exception as exc:
            with self.con:
                self.con.execute("UPDATE sources SET last_error=? WHERE source_key=?", (str(exc), source_key))
            raise
        new = changed = removed = pairs_removed = 0
        changed_ids: list[str] = []
        now = utcnow()
        self._raise_if_stopped()
        with self.con:
            snapshot_mode = str(source["snapshot_mode"] or "upsert").lower()
            existing_ids = {
                row[0] for row in self.con.execute(
                    "SELECT product_id FROM products WHERE source_key=?", (source_key,)
                )
            }
            removed_ids = (existing_ids - set(by_id)) if snapshot_mode == "replace" else set()
            if removed_ids:
                # Remove active pair/candidate rows first.  Product facts and
                # historical versions are then pruned for exact current
                # snapshot accounting.  Immutable image/vector cache entries
                # remain reusable and are intentionally not deleted.
                related_paths = [
                    row[0] for row in self.con.execute(
                        "SELECT path_key FROM paths WHERE amazon_source=? OR temu_source=?",
                        (source_key, source_key),
                    )
                ]
                for path_key in related_paths:
                    self._raise_if_stopped()
                    if source["platform"].upper() == "AMAZON":
                        placeholders = ",".join("?" for _ in removed_ids)
                        args = [path_key, *sorted(removed_ids)]
                        self.con.execute(
                            f"DELETE FROM candidates WHERE path_key=? AND asin IN ({placeholders})", args
                        )
                        cur = self.con.execute(
                            f"DELETE FROM pairs WHERE path_key=? AND asin IN ({placeholders})", args
                        )
                    else:
                        placeholders = ",".join("?" for _ in removed_ids)
                        args = [path_key, *sorted(removed_ids)]
                        self.con.execute(
                            f"DELETE FROM candidates WHERE path_key=? AND goods_id IN ({placeholders})", args
                        )
                        cur = self.con.execute(
                            f"DELETE FROM pairs WHERE path_key=? AND goods_id IN ({placeholders})", args
                        )
                    pairs_removed += cur.rowcount
                placeholders = ",".join("?" for _ in removed_ids)
                args = [source_key, *sorted(removed_ids)]
                self.con.execute(f"DELETE FROM observations WHERE source_key=? AND product_id IN ({placeholders})", args)
                self.con.execute(f"DELETE FROM product_versions WHERE source_key=? AND product_id IN ({placeholders})", args)
                self.con.execute(f"DELETE FROM products WHERE source_key=? AND product_id IN ({placeholders})", args)
                removed = len(removed_ids)
            for product_id, row in by_id.items():
                self._raise_if_stopped()
                current = self.con.execute("SELECT evidence_hash FROM products WHERE source_key=? AND product_id=?", (source_key, product_id)).fetchone()
                record_json = stable_json(row)
                if current is None:
                    new += 1; changed_ids.append(product_id)
                    self.con.execute("""INSERT INTO products(
                        source_key,product_id,platform,evidence_hash,record_json,first_seen,last_seen,update_count
                    ) VALUES(?,?,?,?,?,?,?,0)""", (source_key, product_id, source["platform"], row["evidence_hash"], record_json, now, now))
                elif current["evidence_hash"] != row["evidence_hash"]:
                    changed += 1; changed_ids.append(product_id)
                    self.con.execute("UPDATE products SET evidence_hash=?,record_json=?,last_seen=?,update_count=update_count+1 WHERE source_key=? AND product_id=?", (row["evidence_hash"], record_json, now, source_key, product_id))
                else:
                    self.con.execute("UPDATE products SET last_seen=? WHERE source_key=? AND product_id=?", (now, source_key, product_id))
                self.con.execute("INSERT OR IGNORE INTO product_versions VALUES(?,?,?,?,?)", (source_key, product_id, row["evidence_hash"], record_json, now))
                obs = (source_key, product_id, row["raw_record_hash"], row.get("observed_at"), snapshot_hash, now, now)
                self.con.execute("""INSERT INTO observations(source_key,product_id,raw_record_hash,observed_at,snapshot_hash,first_received_at,last_received_at)
                    VALUES(?,?,?,?,?,?,?) ON CONFLICT(source_key,product_id,raw_record_hash) DO UPDATE SET
                    last_received_at=excluded.last_received_at,receive_count=receive_count+1""", obs)
            self.con.execute("UPDATE sources SET last_snapshot_hash=?,last_success_at=?,last_error=NULL,received_records=? WHERE source_key=?", (snapshot_hash, now, len(by_id), source_key))
            for product_id in changed_ids:
                self._raise_if_stopped()
                self._schedule_product(source_key, source["platform"], product_id)
            self._raise_if_stopped()
            self._inc("products_new", new); self._inc("products_changed", changed)
            self._inc("products_removed", removed)
            self._inc("pairs_removed", pairs_removed)
        return {"source": source_key, "received": len(by_id), "new": new, "changed": changed,
                "removed": removed, "pairs_removed": pairs_removed, "reused_snapshot": 0}

    def _schedule_product(self, source_key: str, platform: str, product_id: str) -> None:
        if platform == "AMAZON":
            related = self.con.execute("SELECT * FROM paths WHERE amazon_source=?", (source_key,)).fetchall()
            for path in related:
                opposite = self.con.execute("SELECT product_id,evidence_hash FROM products WHERE source_key=?", (path["temu_source"],)).fetchall()
                amazon_version = self.con.execute("SELECT evidence_hash FROM products WHERE source_key=? AND product_id=?", (source_key, product_id)).fetchone()[0]
                for item in opposite:
                    self._raise_if_stopped()
                    self._upsert_pair(path["path_key"], product_id, item["product_id"], amazon_version, item["evidence_hash"])
        else:
            related = self.con.execute("SELECT * FROM paths WHERE temu_source=?", (source_key,)).fetchall()
            for path in related:
                opposite = self.con.execute("SELECT product_id,evidence_hash FROM products WHERE source_key=?", (path["amazon_source"],)).fetchall()
                temu_version = self.con.execute("SELECT evidence_hash FROM products WHERE source_key=? AND product_id=?", (source_key, product_id)).fetchone()[0]
                for item in opposite:
                    self._raise_if_stopped()
                    self._upsert_pair(path["path_key"], item["product_id"], product_id, item["evidence_hash"], temu_version)

    def _upsert_pair(self, path_key: str, asin: str, goods_id: str, amazon_version: str, temu_version: str) -> None:
        revision = digest("|".join((amazon_version, temu_version, self.rule_version, self.vectorizer.revision, str(self.threshold))))
        current = self.con.execute("SELECT pair_revision FROM pairs WHERE path_key=? AND asin=? AND goods_id=?", (path_key, asin, goods_id)).fetchone()
        if current and current["pair_revision"] == revision:
            self._inc("pairs_reused")
            return
        self.con.execute("DELETE FROM candidates WHERE path_key=? AND asin=? AND goods_id=?", (path_key, asin, goods_id))
        self.con.execute("""INSERT INTO pairs(path_key,asin,goods_id,amazon_version,temu_version,pair_revision,status,updated_at)
            VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(path_key,asin,goods_id) DO UPDATE SET
            amazon_version=excluded.amazon_version,temu_version=excluded.temu_version,pair_revision=excluded.pair_revision,
            stage1_status=NULL,stage1_json=NULL,vector_score=NULL,status='PENDING',amazon_content_hash=NULL,
            temu_content_hash=NULL,retry_count=0,last_error=NULL,processing_seconds=0,updated_at=excluded.updated_at""",
            (path_key, asin, goods_id, amazon_version, temu_version, revision, "PENDING", utcnow()))
        self._inc("pairs_scheduled")

    def _product(self, source_key: str, product_id: str) -> dict[str, Any]:
        row = self.con.execute("SELECT record_json FROM products WHERE source_key=? AND product_id=?", (source_key, product_id)).fetchone()
        return json.loads(row[0])

    @staticmethod
    def _image_identity(product: dict[str, Any]) -> tuple[str, str]:
        ref = product.get("local_image_path") or product.get("main_image_url")
        if not ref:
            raise ImageSourceMissing("source has not supplied an image URL or local image")
        ref = str(ref)
        return ref, digest(ref)

    def _download_image_bytes(self, product: dict[str, Any]) -> bytes:
        ref, image_key = self._image_identity(product)
        if product.get("local_image_path"):
            data = Path(ref).read_bytes()
        else:
            if product.get("platform") == "TEMU":
                if self.require_temu_image_proxy and not self.temu_image_proxy_url:
                    raise ImageProxyNotConfigured("TEMU image proxy is required but not configured")
                proxy = self.temu_image_proxy_url
            else:
                proxy = None
            # trust_env=False bypasses HTTP_PROXY/HTTPS_PROXY and therefore
            # cannot consume the collector browser's residential proxy.
            data = self._image_transport.read(ref,proxy=proxy)
        if len(data) < 100:
            raise RuntimeError(f"image too small: {len(data)} bytes")
        try:
            self._validate_image_bytes(data)
        except RuntimeError as error:
            # Observed CDN JPEGs have a complete pixel stream but end in FF65
            # instead of FFD9. Correct only that trailer, then fully decode it.
            # Do not enable truncated-image loading or repair arbitrary damage.
            if not (data.startswith(b'\xff\xd8') and data.endswith(b'\xff\x65')):
                raise
            corrected = data[:-1] + b'\xd9'
            self._validate_image_bytes(corrected)
            original = self.cache / f'{image_key}.original-jpeg'
            original.write_bytes(data)
            original.with_suffix('.recovery.json').write_text(json.dumps({
                'original': str(original), 'original_decode_error': str(error),
                'correction': 'JPEG_TERMINAL_FF65_TO_FFD9', 'changed_bytes': 1,
                'full_decode_passed': True, 'updated_at': utcnow(),
            }, ensure_ascii=False, indent=2), encoding='utf-8')
            data = corrected
        return data

    @staticmethod
    def _validate_image_bytes(data: bytes) -> None:
        # JPEG verify() can accept an intact header with a broken pixel stream.
        # Decode the same bytes before marking an asset READY.
        try:
            with Image.open(io.BytesIO(data)) as image:
                image.verify()
            with Image.open(io.BytesIO(data)) as image:
                image.load()
        except (OSError, SyntaxError, ValueError) as exc:
            raise RuntimeError(f'invalid image: {exc}') from exc

    def _persist_image(self, image_key: str, ref: str, data: bytes, retry_count: int) -> tuple[str, Path]:
        content_hash = digest(data)
        destination = self.cache / f"{content_hash}.img"
        if not destination.exists():
            temporary = destination.with_suffix(".tmp")
            temporary.write_bytes(data)
            os.replace(temporary, destination)
        self.con.execute("""INSERT INTO images VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(image_key) DO UPDATE SET
            content_hash=excluded.content_hash,local_path=excluded.local_path,status='READY',last_error=NULL,updated_at=excluded.updated_at""",
            (image_key, ref, content_hash, str(destination), "READY", retry_count, None, utcnow()))
        return content_hash, destination

    def migrate_image_retries(self,writer_lease,allowed_assets=None,allowed_pairs=None):
        """Import active legacy failures without erasing their counts or errors."""
        stream=getattr(writer_lease,'stream',None)
        if stream is None or stream.closed or Path(writer_lease.path).resolve()!=(self.root/'stage12_watch.pid').resolve():
            raise RuntimeError('image retry migration requires this task single-writer lease')
        self.image_retry_scope=set(allowed_assets) if allowed_assets is not None else None
        self.image_pair_scope=set(allowed_pairs) if allowed_pairs is not None else None
        active={}
        for row in self.con.execute('SELECT record_json FROM products'):
            product=json.loads(row[0])
            try:ref,key=self._image_identity(product)
            except ImageSourceMissing:continue
            active[key]=product
        with self.con:
            self._repair_cached_decode_failures(active)
            for image in self.con.execute("SELECT * FROM images WHERE status='FAILED'").fetchall():
                if self.image_retry_scope is not None and image['image_key'] not in self.image_retry_scope:continue
                if image['image_key'] in active:
                    self.image_retry.legacy(image['image_key'],image['source_ref'],active[image['image_key']],
                        image['retry_count'],image['last_error'] or 'unknown historical image failure')
                    queued = self.image_retry.get(image['image_key'])
                    if (queued['status']=='ACTION_REQUIRED'
                            and 'broken data stream' in str(queued['last_error'])
                            and not self.con.execute("SELECT 1 FROM image_retry_history WHERE image_key=? AND event='JPEG_TRAILER_COMPATIBILITY_RECHECK'",
                                (image['image_key'],)).fetchone()):
                        self.con.execute("UPDATE image_retry_queue SET status='RETRY',next_due=?,lease_until=NULL WHERE image_key=?",
                            (self.image_retry.clock(),image['image_key']))
                        self.image_retry._event(image['image_key'],queued['attempts'],
                            'JPEG_TRAILER_COMPATIBILITY_RECHECK',queued['last_classification'],queued['last_error'])
                    if self.image_retry.get(image['image_key'])['status']=='RETRY':
                        self._wake_image_pairs(image['image_key'],target_status='WAIT_IMAGE_RETRY')

    def _repair_cached_decode_failures(self, active):
        """One download recovery for a proven bad READY cache, under the writer lease."""
        from .image_retry import classify_failure
        images=self.con.execute("""SELECT i.*,v.last_error AS vector_error FROM images i
          JOIN vectors v ON v.content_hash=i.content_hash
          WHERE i.status='READY' AND v.status='FAILED' AND v.vector_revision=?""",
          (self.vectorizer.revision,)).fetchall()
        for image in images:
            self._raise_if_stopped()
            key=image['image_key']
            if key not in active or (self.image_retry_scope is not None and key not in self.image_retry_scope):continue
            if classify_failure(image['vector_error'])!='ACTION_INVALID_IMAGE':continue
            scheduled=self.image_retry.get(key)
            if scheduled and scheduled['status']!='READY':continue
            # A filesystem read failure is not evidence of corrupt image bytes.
            data=Path(image['local_path']).read_bytes()
            try:self._validate_image_bytes(data)
            except RuntimeError as exc:
                if not self._wake_image_pairs(key,target_status='WAIT_IMAGE_RETRY',decode_failures_only=True):continue
                if scheduled is None:
                    self.image_retry.legacy(key,image['source_ref'],active[key],image['retry_count'],exc)
                    scheduled=self.image_retry.get(key)
                now=self.image_retry.clock()
                self.con.execute("UPDATE images SET status='FAILED',last_error=?,updated_at=? WHERE image_key=?",
                    (str(exc),utcnow(),key))
                self.con.execute("""UPDATE image_retry_queue SET status='RETRY',next_due=?,lease_until=NULL,
                  last_classification='ACTION_INVALID_IMAGE',last_error=?,updated_at=? WHERE image_key=?""",
                  (now,str(exc),now,key))
                self.image_retry._event(key,scheduled['attempts'],'CORRUPT_CACHE_RETRY_SCHEDULED','ACTION_INVALID_IMAGE',str(exc))
                self._corrupt_cache_assets.add(key)

    def _wake_image_pairs(self,image_key,target_status='PENDING',*,decode_failures_only=False):
        import uuid
        awakened=0
        for product in self.con.execute('SELECT source_key,product_id,evidence_hash,record_json FROM products').fetchall():
            try:_,key=self._image_identity(json.loads(product['record_json']))
            except ImageSourceMissing:continue
            if key!=image_key:continue
            rows=self.con.execute("""SELECT p.* FROM pairs p JOIN paths x ON x.path_key=p.path_key
              WHERE ((x.amazon_source=? AND p.asin=? AND p.amazon_version=?) OR
                     (x.temu_source=? AND p.goods_id=? AND p.temu_version=?))
                AND p.status IN ('WAIT_IMAGE_RETRY','FAILED_BOUNDED','FAILED_RETRYABLE','FAILED_IMAGE_ACTION')""",
              (product['source_key'],product['product_id'],product['evidence_hash'])*2).fetchall()
            for row in rows:
                if row['status']==target_status:continue
                if (decode_failures_only or image_key in self._corrupt_cache_assets) and row['status'] in ('FAILED_BOUNDED','FAILED_RETRYABLE'):
                    from .image_retry import classify_failure
                    if classify_failure(row['last_error'])!='ACTION_INVALID_IMAGE':continue
                identity=(row['path_key'],row['asin'],row['goods_id'],row['pair_revision'])
                if self.image_pair_scope is not None and identity not in self.image_pair_scope:continue
                self.con.execute('INSERT INTO image_pair_requeue_history VALUES(?,?,?,?,?,?,?,?,?,?)',
                    (uuid.uuid4().hex,image_key,row['path_key'],row['asin'],row['goods_id'],row['pair_revision'],
                     row['status'],row['retry_count'],row['last_error'],time.time()))
                self.con.execute("UPDATE pairs SET status=?,updated_at=? WHERE path_key=? AND asin=? AND goods_id=? AND pair_revision=?",
                    (target_status,utcnow(),row['path_key'],row['asin'],row['goods_id'],row['pair_revision']))
                awakened+=1
        return awakened

    def _prefetch_images(self, products: Iterable[dict[str, Any]]) -> tuple[dict[str, Exception], float]:
        started = time.monotonic()
        if self.stop_requested():return {},0
        pending: dict[str, tuple[dict[str, Any], str, int]] = {}
        errors: dict[str, Exception] = {}
        due=self.image_retry.due(max(1,min(4,self.image_download_workers//2)))
        if due:
            active_keys=set()
            for row in self.con.execute('SELECT record_json FROM products'):
                self._raise_if_stopped()
                try:active_keys.add(self._image_identity(json.loads(row[0]))[1])
                except ImageSourceMissing:pass
            for row in due:
                if row['image_key'] not in active_keys:
                    self.con.execute("UPDATE image_retry_queue SET status='INACTIVE',updated_at=? WHERE image_key=?",(time.time(),row['image_key']))
                    self.image_retry._event(row['image_key'],row['attempts'],'ASSET_NO_LONGER_REFERENCED')
            due=[row for row in due if row['image_key'] in active_keys]
        due_keys={row['image_key'] for row in due}
        # A bounded retry share joins the same pool as fresh assets; no extra
        # serial retry pool can hold up newly collected successful images.
        combined=[json.loads(row['product_json']) for row in due]+list(products)
        for product in combined:
            self._raise_if_stopped()
            try:
                ref, image_key = self._image_identity(product)
            except ImageSourceMissing:
                continue
            if image_key in pending or image_key in errors:
                continue
            known = self.con.execute("SELECT * FROM images WHERE image_key=?", (image_key,)).fetchone()
            if known and known["status"] == "READY" and Path(known["local_path"]).exists():
                continue
            retry_count = int(known["retry_count"] if known else 0)
            scheduled=self.image_retry.get(image_key)
            if scheduled and scheduled['status']!='READY' and image_key not in due_keys:
                errors[image_key] = self.image_retry.deferred(image_key)
                continue
            if (product.get("platform") == "TEMU" and self.require_temu_image_proxy
                    and not self.temu_image_proxy_url and not product.get("local_image_path")):
                errors[image_key] = ImageProxyNotConfigured("TEMU image proxy is required but not configured")
                continue
            pending[image_key] = (product, ref, retry_count)
        if not pending:
            self.con.commit()
            return errors, time.monotonic() - started

        with self.con:
            for key,(product,ref,retry_count) in pending.items():
                self.image_retry.started(key,ref,product,retry_count)

        successes: list[tuple[str, str, bytes, int]] = []
        failures: list[tuple[str, str, int, Exception]] = []
        with ThreadPoolExecutor(max_workers=min(self.image_download_workers, len(pending)), thread_name_prefix="image") as pool:
            def download_if_running(product):
                from .image_retry import ImageRetryCancelled
                if self.stop_requested():raise ImageRetryCancelled('task stopped before download')
                return self._download_image_bytes(product)
            futures = {pool.submit(download_if_running, item[0]): (key, item[1], item[2])
                       for key, item in pending.items()}
            for future in as_completed(futures):
                image_key, ref, retry_count = futures[future]
                try:
                    successes.append((image_key, ref, future.result(), retry_count))
                except Exception as exc:
                    failures.append((image_key, ref, retry_count, exc))
                    errors[image_key] = exc
        with self.con:
            for image_key, ref, data, retry_count in successes:
                self._persist_image(image_key, ref, data, retry_count)
                self.image_retry.finish(image_key)
                self._wake_image_pairs(image_key)
            for image_key, ref, retry_count, exc in failures:
                from .image_retry import ImageRetryCancelled
                if isinstance(exc,ImageRetryCancelled):
                    self.image_retry.cancel(image_key);errors[image_key]=self.image_retry.deferred(image_key)
                    continue
                if isinstance(exc, (ImageProxyNotConfigured, ImageSourceMissing)):
                    continue
                self.con.execute("""INSERT INTO images VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(image_key) DO UPDATE SET
                    status='FAILED',retry_count=images.retry_count+1,last_error=excluded.last_error,updated_at=excluded.updated_at""",
                    (image_key, ref, None, None, "FAILED", retry_count + 1, str(exc), utcnow()))
                self.image_retry.finish(image_key,exc)
                if self.image_retry.get(image_key)['status']=='ACTION_REQUIRED':
                    self._wake_image_pairs(image_key,target_status='FAILED_IMAGE_ACTION',decode_failures_only=True)
                errors[image_key]=self.image_retry.deferred(image_key)
            self._inc("image_downloaded", len(successes))
        return errors, time.monotonic() - started

    def _prefetch_vectors(self, products: Iterable[dict[str, Any]]) -> tuple[dict[str, Exception], float]:
        started = time.monotonic()
        missing: dict[str, Path] = {}
        for product in products:
            self._raise_if_stopped()
            try:
                _, image_key = self._image_identity(product)
            except ImageSourceMissing:
                continue
            image = self.con.execute("SELECT content_hash,local_path,status FROM images WHERE image_key=?", (image_key,)).fetchone()
            if not image or image["status"] != "READY" or not image["content_hash"]:
                continue
            known = self.con.execute("SELECT status FROM vectors WHERE content_hash=? AND vector_revision=?",
                                     (image["content_hash"], self.vectorizer.revision)).fetchone()
            if not known or known["status"] != "READY":
                missing[image["content_hash"]] = Path(image["local_path"])
        errors: dict[str, Exception] = {}
        if not missing:
            return errors, time.monotonic() - started

        items = list(missing.items())
        successes: list[tuple[str, np.ndarray]] = []
        for offset in range(0, len(items), self.vector_batch_size):
            self._raise_if_stopped()
            chunk = items[offset:offset + self.vector_batch_size]
            try:
                encode_many = getattr(self.vectorizer, "encode_many", None)
                if encode_many:
                    values = encode_many([path for _, path in chunk])
                else:
                    values = []
                    for _, path in chunk:
                        self._raise_if_stopped()
                        values.append(self.vectorizer.encode(path))
                self._raise_if_stopped()
                if len(values) != len(chunk):
                    raise RuntimeError("vector batch returned unexpected row count")
                chunk_successes = []
                for (content_hash, _), value in zip(chunk, values):
                    self._raise_if_stopped()
                    vector = np.asarray(value, dtype=np.float32).reshape(-1)
                    if not vector.size or not np.isfinite(vector).all():
                        raise RuntimeError("invalid vector")
                    chunk_successes.append((content_hash, vector))
                successes.extend(chunk_successes)
            except CancelledError:
                raise
            except Exception:
                # One corrupt image must not discard the rest of a batch.
                for content_hash, path in chunk:
                    self._raise_if_stopped()
                    try:
                        vector = np.asarray(self.vectorizer.encode(path), dtype=np.float32).reshape(-1)
                        self._raise_if_stopped()
                        if not vector.size or not np.isfinite(vector).all():
                            raise RuntimeError("invalid vector")
                        successes.append((content_hash, vector))
                    except CancelledError:
                        raise
                    except Exception as exc:
                        errors[content_hash] = exc
        self._raise_if_stopped()
        with self.con:
            self.con.executemany("INSERT OR REPLACE INTO vectors VALUES(?,?,?,?,?,?,?)", [
                (content_hash, self.vectorizer.revision, int(vector.size), vector.tobytes(), "READY", None, utcnow())
                for content_hash, vector in successes
            ])
            self.con.executemany("INSERT OR REPLACE INTO vectors VALUES(?,?,?,?,?,?,?)", [
                (content_hash, self.vectorizer.revision, None, None, "FAILED", str(exc), utcnow())
                for content_hash, exc in errors.items()
            ])
            self._inc("vectors_computed", len(successes))
        return errors, time.monotonic() - started

    def _ensure_image(self, product: dict[str, Any]) -> tuple[str, Path, bool]:
        ref, image_key = self._image_identity(product)
        known = self.con.execute("SELECT * FROM images WHERE image_key=?", (image_key,)).fetchone()
        if known and known["status"] == "READY" and Path(known["local_path"]).exists():
            self._inc("image_reused")
            return known["content_hash"], Path(known["local_path"]), True
        retry_count = int(known["retry_count"] if known else 0)
        scheduled=self.image_retry.get(image_key)
        if scheduled and scheduled['status']!='READY':raise self.image_retry.deferred(image_key)
        if self.stop_requested():
            from .image_retry import ImageRetryPending
            raise ImageRetryPending('task stopped before download')
        if product.get('platform')=='TEMU' and self.require_temu_image_proxy and not self.temu_image_proxy_url and not product.get('local_image_path'):
            raise ImageProxyNotConfigured('TEMU image proxy is required but not configured')
        with self.con:self.image_retry.started(image_key,ref,product,retry_count)
        try:
            data = self._download_image_bytes(product)
            with self.con:
                content_hash, destination = self._persist_image(image_key, ref, data, retry_count)
                self.image_retry.finish(image_key)
                self._wake_image_pairs(image_key)
                self._inc("image_downloaded")
            return content_hash, destination, False
        except (ImageProxyNotConfigured, ImageSourceMissing):
            raise
        except Exception as exc:
            with self.con:
                self.con.execute("""INSERT INTO images VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(image_key) DO UPDATE SET
                    status='FAILED',retry_count=images.retry_count+1,last_error=excluded.last_error,updated_at=excluded.updated_at""",
                    (image_key, str(ref), None, None, "FAILED", retry_count + 1, str(exc), utcnow()))
                self.image_retry.finish(image_key,exc)
                if image_key in self._corrupt_cache_assets and self.image_retry.get(image_key)['status']=='ACTION_REQUIRED':
                    self._wake_image_pairs(image_key,target_status='FAILED_IMAGE_ACTION')
            raise self.image_retry.deferred(image_key) from exc

    def _vector(self, content_hash: str, image_path: Path) -> tuple[np.ndarray, bool]:
        row = self.con.execute("SELECT * FROM vectors WHERE content_hash=? AND vector_revision=?", (content_hash, self.vectorizer.revision)).fetchone()
        if row and row["status"] == "READY":
            self._inc("vector_reused")
            return np.frombuffer(row["vector_blob"], dtype=np.float32).copy(), True
        try:
            vector = np.asarray(self.vectorizer.encode(image_path), dtype=np.float32).reshape(-1)
            if not vector.size or not np.isfinite(vector).all():
                raise RuntimeError("invalid vector")
            with self.con:
                self.con.execute("INSERT OR REPLACE INTO vectors VALUES(?,?,?,?,?,?,?)", (content_hash, self.vectorizer.revision, int(vector.size), vector.tobytes(), "READY", None, utcnow()))
                self._inc("vectors_computed")
            return vector, False
        except Exception as exc:
            with self.con:
                self.con.execute("INSERT OR REPLACE INTO vectors VALUES(?,?,?,?,?,?,?)", (content_hash, self.vectorizer.revision, None, None, "FAILED", str(exc), utcnow()))
            raise

    def _raise_if_stopped(self) -> None:
        if self.stop_requested():
            raise CancelledError('Stage 1/2 stopped at a processing boundary')

    def process_batch(self, batch_size: int = 50) -> dict[str, Any]:
        self._batch_phase = 'selection'
        self._batch_selected = self._batch_stage1_committed = 0
        started = time.monotonic()
        try:
            return self._process_batch(batch_size)
        except CancelledError:
            # Stage 1 is an independent truthful checkpoint. Final pair status
            # and candidates stay pending until the whole final transaction.
            return {'selected': self._batch_selected, 'completed': 0, 'failed': 0,
                    'new_candidates': 0, 'cancelled': True, 'cancelled_phase': self._batch_phase,
                    'stage1_committed': self._batch_stage1_committed,
                    'processing_seconds': round(time.monotonic() - started, 3)}

    def _process_batch(self, batch_size: int) -> dict[str, int | float]:
        self._raise_if_stopped()
        self.ensure_processing_contract()
        self._raise_if_stopped()
        batch_started = time.monotonic()
        phase_started = batch_started
        path_keys = [r[0] for r in self.con.execute("SELECT path_key FROM paths ORDER BY path_key")]
        per_path = max(1, batch_size // max(1, len(path_keys)))
        eligible = ("FAILED_RETRYABLE", "WAIT_IMAGE_PROXY", "PENDING") if self.temu_image_proxy_url else ("FAILED_RETRYABLE", "PENDING")

        def fetch_path_rows(path_key: str, limit: int, skip: set[tuple[str, str, str]] | None = None) -> list[sqlite3.Row]:
            """Use the (status, updated_at) index without a million-row CASE sort."""
            found: list[sqlite3.Row] = []
            skipped = skip or set()
            for status in eligible:
                self._raise_if_stopped()
                remaining = limit - len(found)
                if remaining <= 0:
                    break
                # A refill can encounter rows selected during the fair-share pass.
                # Ask for enough headroom and filter those few keys in Python.
                query_limit = remaining + min(len(skipped), batch_size)
                candidates = self.con.execute("""SELECT * FROM pairs
                    WHERE path_key=? AND status=? ORDER BY updated_at LIMIT ?""",
                    (path_key, status, query_limit)).fetchall()
                for row in candidates:
                    self._raise_if_stopped()
                    key = (row["path_key"], row["asin"], row["goods_id"])
                    if key not in skipped:
                        found.append(row)
                        if len(found) >= limit:
                            break
            return found

        rows: list[sqlite3.Row] = []
        for path_key in path_keys:
            self._raise_if_stopped()
            rows.extend(fetch_path_rows(path_key, per_path))
        if len(rows) < batch_size:
            selected_keys = {(r["path_key"], r["asin"], r["goods_id"]) for r in rows}
            for path_key in path_keys:
                self._raise_if_stopped()
                if len(rows) >= batch_size:
                    break
                extras = fetch_path_rows(path_key, batch_size - len(rows), selected_keys)
                rows.extend(extras)
                selected_keys.update((r["path_key"], r["asin"], r["goods_id"]) for r in extras)
        self._batch_selected = len(rows)
        self._batch_phase = 'text_preparation'
        selection_seconds = time.monotonic() - phase_started
        phase_started = time.monotonic()
        base_contexts = []
        products_to_prepare: dict[tuple[str, str], dict[str, Any]] = {}
        path_cache = {row["path_key"]: row for row in self.con.execute("SELECT * FROM paths")}
        product_cache: dict[tuple[str, str], dict[str, Any]] = {}
        for pair in rows:
            self._raise_if_stopped()
            path = path_cache[pair["path_key"]]
            amazon_key = (path["amazon_source"], pair["asin"])
            temu_key = (path["temu_source"], pair["goods_id"])
            if amazon_key not in product_cache:
                product_cache[amazon_key] = self._product(*amazon_key)
            if temu_key not in product_cache:
                product_cache[temu_key] = self._product(*temu_key)
            amazon = product_cache[amazon_key]
            temu = product_cache[temu_key]
            base_contexts.append((pair, amazon, temu, amazon_key, temu_key))
        gate_inputs = []
        for _, amazon, temu, _, _ in base_contexts:
            self._raise_if_stopped()
            values = (self.text_gate_name, amazon.get("gate_text") or "", temu.get("gate_text") or "")
            if self._declarative_snapshot is not None:
                # Workers use the frozen contract, never a reopened live path.
                values = (*values, self._declarative_snapshot)
            gate_inputs.append(values)
        self._batch_phase = 'text_evaluation'
        gates = self._evaluate_text_gates(gate_inputs)
        self._raise_if_stopped()
        # Stage 1 is already complete. Commit its actual decisions before slow
        # downloads/inference, so independent dashboard readers can report them.
        # Pending image work remains retryable if the process is interrupted.
        with self.con:
            self.con.executemany("""UPDATE pairs SET stage1_status=?,stage1_json=?
                WHERE path_key=? AND asin=? AND goods_id=?""", [
                (gate["stage1_status"], stable_json(gate), pair["path_key"], pair["asin"], pair["goods_id"])
                for (pair, _, _, _, _), gate in zip(base_contexts, gates)
            ])
        self._batch_stage1_committed = len(gates)
        self._raise_if_stopped()
        text_seconds = time.monotonic() - phase_started
        phase_started = time.monotonic()
        contexts = []
        for (pair, amazon, temu, amazon_key, temu_key), gate in zip(base_contexts, gates):
            self._raise_if_stopped()
            contexts.append((pair, amazon, temu, gate))
            if gate["stage1_status"] != "TEXT_HARD_EXCLUDE":
                products_to_prepare[amazon_key] = amazon
                products_to_prepare[temu_key] = temu

        self._batch_phase = 'image_download'
        image_errors, download_seconds = self._prefetch_images(products_to_prepare.values())
        self._raise_if_stopped()
        self._batch_phase = 'vector_inference'
        vector_errors, vector_seconds = self._prefetch_vectors(products_to_prepare.values())
        self._raise_if_stopped()
        self._batch_phase = 'pair_preparation'
        # Prepare each distinct image/vector once for the whole pair batch.  The
        # former per-pair path repeated four SQLite lookups for the same product.
        prepared_images: dict[str, tuple[str, Path, np.ndarray, np.floating[Any]]] = {}
        prepared_products: dict[int, tuple[str, Path, np.ndarray, np.floating[Any]]] = {}
        preparation_errors: dict[int, Exception] = {}
        for product in products_to_prepare.values():
            self._raise_if_stopped()
            try:
                _, image_key = self._image_identity(product)
                if image_key in image_errors:
                    raise image_errors[image_key]
                prepared = prepared_images.get(image_key)
                if prepared is None:
                    content_hash, image_path, _ = self._ensure_image(product)
                    if content_hash in vector_errors:
                        raise vector_errors[content_hash]
                    vector, _ = self._vector(content_hash, image_path)
                    self._raise_if_stopped()
                    vector = np.asarray(vector, dtype=np.float64)
                    norm = np.linalg.norm(vector)
                    validate_features(vector,self.vectorizer.revision)
                    if not vector.size or not np.isfinite(vector).all() or not np.isfinite(norm) or norm <= 0:
                        raise RuntimeError("invalid or zero-norm vector")
                    prepared = (content_hash, image_path, vector / norm, 1.0)
                    prepared_images[image_key] = prepared
                prepared_products[id(product)] = prepared
            except CancelledError:
                raise
            except Exception as exc:
                preparation_errors[id(product)] = exc
        preparation_seconds = time.monotonic() - phase_started
        phase_started = time.monotonic()

        completed = failed = candidates = 0
        pair_processing = 0.0
        success_updates: list[tuple[Any, ...]] = []
        wait_updates: list[tuple[Any, ...]] = []
        failure_updates: list[tuple[Any, ...]] = []
        candidate_rows: list[tuple[Any, ...]] = []
        candidate_deletes: list[tuple[str, str, str]] = []
        ready: list[tuple[Any, ...]] = []
        batch_now = utcnow()

        self._batch_phase = 'pair_evaluation'
        for pair, amazon, temu, gate in contexts:
            self._raise_if_stopped()
            started = time.monotonic()
            if gate["stage1_status"] == "TEXT_HARD_EXCLUDE":
                elapsed = time.monotonic() - started; pair_processing += elapsed
                success_updates.append((gate["stage1_status"], stable_json(gate), None, "STAGE1_EXCLUDED",
                    None, None, elapsed, batch_now, pair["path_key"], pair["asin"], pair["goods_id"]))
                candidate_deletes.append((pair["path_key"], pair["asin"], pair["goods_id"]))
                completed += 1
                continue
            try:
                if id(amazon) in preparation_errors:
                    raise preparation_errors[id(amazon)]
                if id(temu) in preparation_errors:
                    raise preparation_errors[id(temu)]
                a_hash, a_path, av, a_norm = prepared_products[id(amazon)]
                t_hash, t_path, tv, t_norm = prepared_products[id(temu)]
                if av.shape != tv.shape:
                    raise RuntimeError("vector dimension mismatch")
                ready.append((pair, amazon, temu, gate, a_hash, a_path, av, a_norm,
                    t_hash, t_path, tv, t_norm, started))
            except (ImageRetryPending,ImageNeedsAction) as exc:
                elapsed=time.monotonic()-started;pair_processing+=elapsed
                wait_status='WAIT_IMAGE_RETRY' if isinstance(exc,ImageRetryPending) else 'FAILED_IMAGE_ACTION'
                wait_updates.append((gate['stage1_status'],stable_json(gate),wait_status,str(exc),elapsed,
                    batch_now,pair['path_key'],pair['asin'],pair['goods_id']))
                candidate_deletes.append((pair['path_key'],pair['asin'],pair['goods_id']))
                failed+=1
            except (ImageProxyNotConfigured, ImageSourceMissing) as exc:
                elapsed = time.monotonic() - started; pair_processing += elapsed
                wait_status = "WAIT_IMAGE_PROXY" if isinstance(exc, ImageProxyNotConfigured) else "WAIT_IMAGE_SOURCE"
                wait_updates.append((gate["stage1_status"], stable_json(gate), wait_status, str(exc), elapsed,
                    batch_now, pair["path_key"], pair["asin"], pair["goods_id"]))
                candidate_deletes.append((pair["path_key"], pair["asin"], pair["goods_id"]))
                failed += 1
            except Exception as exc:
                elapsed = time.monotonic() - started; pair_processing += elapsed
                next_retry_count = int(pair["retry_count"] or 0) + 1
                failure_status = "FAILED_BOUNDED" if next_retry_count >= self.max_image_retries else "FAILED_RETRYABLE"
                failure_updates.append((gate["stage1_status"], stable_json(gate), failure_status, str(exc), elapsed,
                    batch_now, pair["path_key"], pair["asin"], pair["goods_id"]))
                candidate_deletes.append((pair["path_key"], pair["asin"], pair["goods_id"]))
                failed += 1

        # NumPy evaluates all ready cosine scores in contiguous chunks instead
        # of dispatching one Python/BLAS call per pair.
        dimension_groups: dict[int, list[tuple[Any, ...]]] = {}
        for item in ready:
            self._raise_if_stopped()
            dimension_groups.setdefault(item[6].size, []).append(item)
        chunks = (group[offset:offset + 256] for group in dimension_groups.values()
                  for offset in range(0, len(group), 256))
        for chunk in chunks:
            self._raise_if_stopped()
            left = np.stack([item[6] for item in chunk])
            right = np.stack([item[10] for item in chunk])
            chunk_scores = similarity_scores(left, right, self.vectorizer.revision)
            for item, raw_score in zip(chunk, chunk_scores):
                self._raise_if_stopped()
                pair, amazon, temu, gate, a_hash, a_path, _, _, t_hash, t_path, _, _, started = item
                score = round(float(raw_score), 4)
                status = "CANDIDATE" if float(raw_score) >= self.threshold else "BELOW_VECTOR_THRESHOLD"
                elapsed = time.monotonic() - started; pair_processing += elapsed
                success_updates.append((gate["stage1_status"], stable_json(gate), score, status, a_hash, t_hash,
                    elapsed, batch_now, pair["path_key"], pair["asin"], pair["goods_id"]))
                if status == "CANDIDATE":
                    payload = {
                        "source_path": pair["path_key"], "asin": pair["asin"], "goods_id": pair["goods_id"],
                        "amazon_title": amazon.get("title"), "temu_title": temu.get("title"),
                        "amazon_image_url": amazon.get("main_image_url"), "temu_image_url": temu.get("main_image_url"),
                        "amazon_image_path": str(a_path), "temu_image_path": str(t_path),
                        "amazon_product_facts": amazon, "temu_product_facts": temu,
                        "amazon_fact_version": pair["amazon_version"], "temu_fact_version": pair["temu_version"],
                        "pair_revision": pair["pair_revision"], "vector_revision": self.vectorizer.revision,
                        "vector_score": score, **gate,
                    }
                    candidate_rows.append((pair["path_key"], pair["asin"], pair["goods_id"],
                        pair["pair_revision"], stable_json(payload), batch_now))
                    candidates += 1
                else:
                    candidate_deletes.append((pair["path_key"], pair["asin"], pair["goods_id"]))
                completed += 1
        pair_build_seconds = time.monotonic() - phase_started
        phase_started = time.monotonic()
        # One transaction per batch keeps interruption atomic and removes tens
        # of thousands of fsync/transaction boundaries from the hot path.
        self._batch_phase = 'pair_commit'
        self._raise_if_stopped()
        with self.con:
            self.con.executemany("""UPDATE pairs SET stage1_status=?,stage1_json=?,vector_score=?,status=?,amazon_content_hash=?,
                temu_content_hash=?,last_error=NULL,processing_seconds=?,updated_at=? WHERE path_key=? AND asin=? AND goods_id=?""", success_updates)
            self.con.executemany("""UPDATE pairs SET stage1_status=?,stage1_json=?,status=?,last_error=?,
                processing_seconds=processing_seconds+?,updated_at=? WHERE path_key=? AND asin=? AND goods_id=?""", wait_updates)
            self.con.executemany("""UPDATE pairs SET stage1_status=?,stage1_json=?,status=?,retry_count=retry_count+1,
                last_error=?,processing_seconds=processing_seconds+?,updated_at=? WHERE path_key=? AND asin=? AND goods_id=?""", failure_updates)
            self.con.executemany("INSERT OR REPLACE INTO candidates VALUES(?,?,?,?,?,?)", candidate_rows)
            self.con.executemany("DELETE FROM candidates WHERE path_key=? AND asin=? AND goods_id=?", candidate_deletes)
            self._inc("pairs_processed", completed)
            self._inc("pairs_waiting_image", len(wait_updates))
            self._inc("pair_failures", len(failure_updates))
        database_seconds = time.monotonic() - phase_started
        return {
            "selected": len(rows), "completed": completed, "failed": failed,
            "new_candidates": candidates,
            "processing_seconds": round(time.monotonic() - batch_started, 3),
            "download_seconds": round(download_seconds, 3),
            "vector_seconds": round(vector_seconds, 3),
            "pair_seconds": round(pair_processing, 3),
            "selection_seconds": round(selection_seconds, 3),
            "text_seconds": round(text_seconds, 3),
            "preparation_seconds": round(preparation_seconds, 3),
            "pair_build_seconds": round(pair_build_seconds, 3),
            "database_seconds": round(database_seconds, 3),
        }

    def materialize_candidates(self, destination_root: Path | None = None) -> dict[str, str | int]:
        outputs: dict[str, str | int] = {}
        output_root = Path(destination_root) if destination_root else self.root
        output_root.mkdir(parents=True, exist_ok=True)
        for path in self.con.execute("SELECT path_key FROM paths ORDER BY path_key"):
            destination = output_root / f"layer3_candidates_{path['path_key']}.jsonl"
            temporary = unique_temporary_path(destination)
            if os.name == "nt" and len(str(temporary.absolute())) >= 260:
                raise ValueError("候选导出路径超过Windows兼容长度：请使用较短的安装/输出目录或类别key后重试；已有数据库和候选文件保留，未修改系统长路径设置。")
            count = 0
            try:
                query = self.con.execute(
                    "SELECT payload_json FROM candidates WHERE path_key=? ORDER BY asin,goods_id",
                    (path["path_key"],),
                )
                with temporary.open("w", encoding="utf-8", newline="\n") as output:
                    for row in query:
                        output.write(row[0])
                        output.write("\n")
                        count += 1
                # Idle watch cycles can export the exact same candidate bytes.
                # Keep their timestamp so web/Excel readers do not treat that as
                # new evidence; real byte changes still replace atomically.
                if not same_snapshot_bytes(temporary, destination):
                    replace_snapshot_file(temporary, destination)
            finally:
                temporary.unlink(missing_ok=True)
            outputs[path["path_key"]] = str(destination)
            outputs[f"{path['path_key']}_count"] = count
        return outputs

    def materialize_layer12_export(self, path_key: str, destination_root: Path | None = None) -> dict[str, Any]:
        """Write rows and summary from one SQLite read snapshot."""
        owns_transaction = not self.con.in_transaction
        if owns_transaction:
            self.con.execute("BEGIN")
        output_root = Path(destination_root) if destination_root else self.root
        output_root.mkdir(parents=True, exist_ok=True)
        destination = output_root / f"layer12_rows_{path_key}.jsonl"
        temporary = unique_temporary_path(destination)
        summary_path = output_root / f"layer12_summary_{path_key}.json"
        summary_tmp = unique_temporary_path(summary_path)
        try:
            path = self.con.execute("SELECT * FROM paths WHERE path_key=?", (path_key,)).fetchone()
            if not path:
                raise KeyError(path_key)
            snapshot_at = utcnow()
            # Parse each product once in this same read transaction. Retain only
            # export fields, rather than repeating large source JSON per pair.
            products = {}
            for source in {path["amazon_source"], path["temu_source"]}:
                fields = {}
                for product in self.con.execute(
                    "SELECT product_id,record_json FROM products WHERE source_key=?", (source,),
                ):
                    record = json.loads(product["record_json"])
                    fields[product["product_id"]] = {
                        key: record.get(key) for key in ("title", "main_image_url", "product_url")
                    }
                products[source] = fields
            amazon_products = products[path["amazon_source"]]
            temu_products = products[path["temu_source"]]
            query = self.con.execute("""SELECT asin,goods_id,stage1_json,stage1_status,
                vector_score,status,last_error,pair_revision FROM pairs
                WHERE path_key=? ORDER BY asin,goods_id""", (path_key,))
            count = 0
            with temporary.open("w", encoding="utf-8", newline="\n") as output:
                for row in query:
                    amazon = amazon_products.get(row["asin"])
                    temu = temu_products.get(row["goods_id"])
                    # Preserve the previous inner JOIN's orphan exclusion.
                    if amazon is None or temu is None:
                        continue
                    count += 1
                    gate = json.loads(row["stage1_json"]) if row["stage1_json"] else {}
                    reasons = [str(x.get("reason") or "").strip() for x in gate.get("hard_conflicts", []) if x.get("reason")]
                    if row["stage1_status"] == "TEXT_HARD_EXCLUDE":
                        text_fact = "；".join(reasons) or "双边文字存在明确身份级硬冲突"
                    elif row["stage1_status"] == "TEXT_INSUFFICIENT":
                        text_fact = "文字证据不足，未发现可确认的硬冲突，继续图片向量层"
                    elif row["stage1_status"]:
                        text_fact = "未发现文字或规格硬冲突，普通规格差异不作为排除"
                    else:
                        text_fact = "第一层待处理"
                    payload = {
                        "序号": count, "asin": row["asin"], "goods_id": row["goods_id"],
                        "amazon_title": amazon.get("title"), "temu_title": temu.get("title"),
                        "amazon_image_url": amazon.get("main_image_url"), "temu_image_url": temu.get("main_image_url"),
                        "amazon_product_url": amazon.get("product_url"), "temu_product_url": temu.get("product_url"),
                        "stage1_text_fact": text_fact, "stage1_status": row["stage1_status"],
                        "vector_score": row["vector_score"], "pair_status": row["status"],
                        "last_error": row["last_error"], "pair_revision": row["pair_revision"],
                    }
                    output.write(json.dumps(payload, ensure_ascii=False) + "\n")
            status = self.status()
            group = next(x for x in status["groups"] if x["path_key"] == path_key)
            summary = {
                "schema_version": "incremental-layer12-excel-v1", "snapshot_at": snapshot_at,
                "path_key": path_key, "label": path["label"], "rows": count,
                "amazon_products": group["amazon_products"], "temu_products": group["temu_products"],
                "expected_pairs": group["expected_pairs"], "actual_pairs": group["actual_pairs"],
                "states": group["states"], "candidates": group["candidates"],
                "candidate_threshold": self.threshold, "vector_revision": self.vectorizer.revision,
                "third_layer_status": "未启动，等待用户指令", "api_requests": 0,
                "input_jsonl": str(destination),
            }
            summary_tmp.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
            if owns_transaction:
                self.con.commit()
            replace_snapshot_file(temporary, destination)
            replace_snapshot_file(summary_tmp, summary_path)
            return {"rows": str(destination), "summary": str(summary_path), "row_count": count,
                    "snapshot_at": snapshot_at}
        except Exception:
            if owns_transaction:
                self.con.rollback()
            raise
        finally:
            temporary.unlink(missing_ok=True)
            summary_tmp.unlink(missing_ok=True)

    def materialize_layer3_snapshot(self, path_key: str, destination_root: Path) -> dict[str, Any]:
        """Freeze candidates, all pair rows and their summary from one SQLite read view."""
        self.con.execute("BEGIN")
        try:
            queues = self.materialize_candidates(destination_root)
            layer12 = self.materialize_layer12_export(path_key, destination_root)
            self.con.commit()
        except Exception:
            self.con.rollback()
            raise
        return {"candidate": queues[path_key], "candidate_count": queues[f"{path_key}_count"], **layer12}

    def status(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "database": str(self.db_path), "threshold": self.threshold,
            "vector_revision": self.vectorizer.revision,
            "temu_image_proxy_required": self.require_temu_image_proxy,
            "temu_image_proxy_configured": bool(self.temu_image_proxy_url),
            "temu_image_network_route": "CONFIGURED_PROXY" if self.temu_image_proxy_url else "LOCAL_DIRECT_WITHOUT_SYSTEM_PROXY",
            "image_cache_mode": "CONTENT_HASH_LOCAL_CACHE",
            "image_download_workers": self.image_download_workers,
            "vector_batch_size": self.vector_batch_size,
            "text_gate_workers": self.text_gate_workers,
        }
        result["sources"] = [dict(r) for r in self.con.execute("SELECT source_key,platform,snapshot_mode,received_records,last_success_at,last_error FROM sources ORDER BY source_key")]
        groups = []
        for path in self.con.execute("SELECT * FROM paths ORDER BY path_key"):
            amazon_n = self.con.execute("SELECT count(*) FROM products WHERE source_key=?", (path["amazon_source"],)).fetchone()[0]
            temu_n = self.con.execute("SELECT count(*) FROM products WHERE source_key=?", (path["temu_source"],)).fetchone()[0]
            states = {r[0]: r[1] for r in self.con.execute("SELECT status,count(*) FROM pairs WHERE path_key=? GROUP BY status", (path["path_key"],))}
            groups.append({"path_key": path["path_key"], "label": path["label"], "amazon_products": amazon_n, "temu_products": temu_n,
                           "expected_pairs": amazon_n * temu_n, "actual_pairs": sum(states.values()), "states": states,
                           "candidates": self.con.execute("SELECT count(*) FROM candidates WHERE path_key=?", (path["path_key"],)).fetchone()[0]})
        result["groups"] = groups
        result["image_failures"] = self.con.execute("SELECT count(*) FROM images WHERE status='FAILED'").fetchone()[0]
        result["missing_image_products"] = self.con.execute("""SELECT count(*) FROM products
            WHERE coalesce(json_extract(record_json,'$.main_image_url'),'')=''
              AND coalesce(json_extract(record_json,'$.local_image_path'),'')=''""").fetchone()[0]
        result["counters"] = {r[0]: r[1] for r in self.con.execute("SELECT name,value FROM counters ORDER BY name")}
        return result

    def comparison_rows(self) -> dict[str, Any]:
        paths = self.con.execute("SELECT * FROM paths ORDER BY path_key").fetchall()
        summary = self.status()
        if len(paths) < 2:
            return {"summary": summary, "common": [], "only": []}
        a, b = paths[0], paths[1]
        a_goods = {r[0] for r in self.con.execute("SELECT product_id FROM products WHERE source_key=?", (a["temu_source"],))}
        b_goods = {r[0] for r in self.con.execute("SELECT product_id FROM products WHERE source_key=?", (b["temu_source"],))}
        common = []
        for goods_id in sorted(a_goods & b_goods):
            for asin_row in self.con.execute("SELECT product_id FROM products WHERE source_key=? ORDER BY product_id", (a["amazon_source"],)):
                asin = asin_row[0]
                rows = {}
                for path in (a, b):
                    pair = self.con.execute("SELECT * FROM pairs WHERE path_key=? AND asin=? AND goods_id=?", (path["path_key"], asin, goods_id)).fetchone()
                    product = self._product(path["temu_source"], goods_id)
                    rows[path["path_key"]] = {"title": product.get("title"), "fields": product.get("fields"), "image": product.get("main_image_url"),
                                                   "evidence_hash": product.get("evidence_hash"), "pair_status": pair["status"] if pair else "MISSING",
                                                   "stage1_status": pair["stage1_status"] if pair else None, "vector_score": pair["vector_score"] if pair else None}
                common.append({"asin": asin, "goods_id": goods_id, "groups": rows})
        only = ([{"source_path": a["path_key"], "goods_id": x} for x in sorted(a_goods-b_goods)] +
                [{"source_path": b["path_key"], "goods_id": x} for x in sorted(b_goods-a_goods)])
        return {"summary": summary, "snapshot_at": utcnow(), "common": common, "only": only}
