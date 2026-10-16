"""Non-blocking delivery copies for continuously refreshed Excel workbooks."""
from __future__ import annotations

import os
import shutil
from datetime import datetime
from pathlib import Path
from typing import Any


def data_workbook_path(view_path: Path) -> Path:
    """Return the private writer target that users should not open in Excel."""
    return view_path.with_name(f"{view_path.stem}_data{view_path.suffix}")


def _atomic_copy(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(
        f".{destination.stem}.{os.getpid()}.tmp{destination.suffix}"
    )
    try:
        shutil.copy2(source, temporary)
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)


def publish_view_copies(
    source: Path, view_path: Path, *, keep_snapshots: int = 3,
    timestamp: datetime | None = None,
) -> dict[str, Any]:
    """Publish an immutable snapshot plus a best-effort fixed view copy.

    Excel may lock ``view_path`` while the user has it open.  The immutable
    snapshot uses a new name on every publication, so that lock never blocks
    the writer-owned source workbook or later analysis.
    """
    stamp = (timestamp or datetime.now().astimezone()).strftime("%Y%m%d_%H%M%S_%f")
    snapshot = view_path.with_name(f"{view_path.stem}_查看快照_{stamp}{view_path.suffix}")
    result: dict[str, Any] = {
        "数据源工作簿": str(source),
        "固定查看副本": str(view_path),
        "时间戳查看快照": str(snapshot),
        "固定查看副本已更新": False,
        "时间戳查看快照已生成": False,
    }
    try:
        _atomic_copy(source, snapshot)
        result["时间戳查看快照已生成"] = True
    except OSError as exc:
        result["时间戳查看快照已生成"] = False
        result["查看快照错误"] = str(exc)

    try:
        _atomic_copy(source, view_path)
        result["固定查看副本已更新"] = True
    except OSError as exc:
        # An open Excel window commonly locks this path on Windows.  Keep the
        # old view open and continue; the timestamped snapshot is current.
        result["固定查看副本错误"] = str(exc)

    result["当前查看产物已交付"] = bool(
        result["时间戳查看快照已生成"] or result["固定查看副本已更新"]
    )
    result["固定查看副本待重试"] = not result["固定查看副本已更新"]

    keep = max(1, int(keep_snapshots))
    snapshots = sorted(
        view_path.parent.glob(f"{view_path.stem}_查看快照_*{view_path.suffix}"),
        key=lambda item: item.stat().st_mtime_ns,
        reverse=True,
    )
    for old in snapshots[keep:]:
        try:
            old.unlink()
        except OSError:
            # A user may have an older snapshot open.  It can be pruned later.
            pass
    result["保留查看快照数"] = min(len(snapshots), keep)
    return result


def retry_fixed_view(source: Path, view_path: Path) -> dict[str, Any]:
    """Retry only the locked fixed copy, without creating repeated snapshots."""
    result = {"数据源工作簿": str(source), "固定查看副本": str(view_path),
              "固定查看副本已更新": False, "固定查看副本待重试": True}
    try:
        _atomic_copy(source, view_path)
        result.update({"固定查看副本已更新": True, "固定查看副本待重试": False})
    except OSError as exc:
        result["固定查看副本错误"] = str(exc)
    return result
