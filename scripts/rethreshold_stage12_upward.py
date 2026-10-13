"""Raise an existing stage-2 candidate threshold without recomputing vectors.

This migration is intentionally one-way.  Raising a threshold only removes
existing candidates, so every retained candidate already has complete facts
and cached image/vector evidence.  Lowering a threshold needs normal stage-2
reprocessing because previously excluded rows may not have materialized
candidate payloads.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def resolve(value: str | Path) -> Path:
    path = Path(value)
    return path if path.is_absolute() else ROOT / path


def revision(*parts: object) -> str:
    text = "|".join(str(part) for part in parts)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def main() -> None:
    parser = argparse.ArgumentParser(description="只提高第二层候选门槛，并复用现有本地向量")
    parser.add_argument("--config", type=Path, required=True)
    args = parser.parse_args()

    config = json.loads(args.config.read_text(encoding="utf-8"))
    output_dir = resolve(config["output_dir"])
    database = output_dir / "stage12.sqlite3"
    path_key = str((config.get("paths") or [{}])[0].get("key") or "")
    summary_path = output_dir / f"layer12_summary_{path_key}.json"
    if not database.is_file() or not summary_path.is_file():
        raise SystemExit("缺少现有stage12数据库或汇总；没有修改任何文件")
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    old_threshold = float(summary["candidate_threshold"])
    new_threshold = float(config["candidate_threshold"])
    if new_threshold < old_threshold:
        raise SystemExit(f"拒绝把门槛从{old_threshold}降到{new_threshold}：降低门槛必须正常重跑第二层")
    if new_threshold == old_threshold:
        print(json.dumps({"状态": "无需迁移", "门槛": new_threshold}, ensure_ascii=False))
        return

    rule_version = str(config["rule_version"])
    vector_revision = str(summary["vector_revision"])
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    connection = sqlite3.connect(database, timeout=120)
    connection.row_factory = sqlite3.Row
    retained = removed = updated = 0
    try:
        connection.execute("PRAGMA busy_timeout=120000")
        connection.execute("BEGIN IMMEDIATE")
        connection.execute("DROP TABLE IF EXISTS temp.rethreshold_candidates")
        connection.execute("""CREATE TEMP TABLE rethreshold_candidates(
            path_key TEXT NOT NULL, asin TEXT NOT NULL, goods_id TEXT NOT NULL,
            pair_revision TEXT NOT NULL, payload_json TEXT NOT NULL, queued_at TEXT NOT NULL,
            PRIMARY KEY(path_key,asin,goods_id))""")
        cursor = connection.execute("""SELECT p.*, c.payload_json, c.queued_at
            FROM pairs p LEFT JOIN candidates c
            ON c.path_key=p.path_key AND c.asin=p.asin AND c.goods_id=p.goods_id
            ORDER BY p.path_key,p.asin,p.goods_id""")
        pair_updates: list[tuple[str, str, str, str, str, str]] = []
        candidate_inserts: list[tuple[str, str, str, str, str, str]] = []
        for row in cursor:
            new_revision = revision(
                row["amazon_version"], row["temu_version"], rule_version,
                vector_revision, str(new_threshold),
            )
            new_status = str(row["status"])
            if row["vector_score"] is not None and row["stage1_status"] != "TEXT_HARD_EXCLUDE":
                new_status = "CANDIDATE" if float(row["vector_score"]) >= new_threshold else "BELOW_VECTOR_THRESHOLD"
            if new_status == "CANDIDATE":
                if not row["payload_json"]:
                    raise RuntimeError(f"保留候选缺少payload: {row['asin']} / {row['goods_id']}")
                payload = json.loads(row["payload_json"])
                payload["pair_revision"] = new_revision
                candidate_inserts.append((
                    row["path_key"], row["asin"], row["goods_id"], new_revision,
                    json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
                    row["queued_at"] or now,
                ))
                retained += 1
            elif row["payload_json"]:
                removed += 1
            pair_updates.append((new_revision, new_status, now, row["path_key"], row["asin"], row["goods_id"]))
            if len(pair_updates) >= 1000:
                connection.executemany("UPDATE pairs SET pair_revision=?,status=?,updated_at=? WHERE path_key=? AND asin=? AND goods_id=?", pair_updates)
                connection.executemany("INSERT INTO rethreshold_candidates VALUES(?,?,?,?,?,?)", candidate_inserts)
                updated += len(pair_updates)
                pair_updates.clear(); candidate_inserts.clear()
        if pair_updates:
            connection.executemany("UPDATE pairs SET pair_revision=?,status=?,updated_at=? WHERE path_key=? AND asin=? AND goods_id=?", pair_updates)
            connection.executemany("INSERT INTO rethreshold_candidates VALUES(?,?,?,?,?,?)", candidate_inserts)
            updated += len(pair_updates)
        connection.execute("DELETE FROM candidates")
        connection.execute("INSERT INTO candidates SELECT * FROM rethreshold_candidates")
        connection.execute("DROP TABLE rethreshold_candidates")
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()

    print(json.dumps({
        "状态": "门槛迁移完成", "旧门槛": old_threshold, "新门槛": new_threshold,
        "更新商品对": updated, "保留第三层候选": retained, "移出第三层候选": removed,
        "API请求": 0,
    }, ensure_ascii=False))


if __name__ == "__main__":
    main()
