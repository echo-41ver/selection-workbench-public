from __future__ import annotations

import argparse
import json
import os
import tempfile
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    source = json.loads(args.input.read_text(encoding="utf-8"))
    rows = list(source.get("records") or [])
    target = int(source.get("record_count") or len(rows))
    if not rows or len({str(row.get("asin") or "") for row in rows}) != len(rows) or len(rows) != target:
        raise SystemExit("Amazon snapshot is empty, duplicated, or count-inconsistent")
    for row in rows:
        row["observed_at"] = source.get("observed_at")
        row["source_url"] = source.get("source_url")
        row["source"] = source.get("source")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary = tempfile.mkstemp(prefix=f".{args.output.name}.", suffix=".tmp", dir=args.output.parent)
    os.close(handle)
    temporary_path = Path(temporary)
    try:
        temporary_path.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows), encoding="utf-8")
        os.replace(temporary_path, args.output)
    finally:
        temporary_path.unlink(missing_ok=True)
    print(json.dumps({"状态": "已转换", "Amazon商品": len(rows), "输出": str(args.output)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
