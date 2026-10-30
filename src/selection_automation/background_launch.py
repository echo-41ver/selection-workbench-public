"""Bounded launcher execution when its background descendants inherit stdio.

PIPE capture waits for EOF from every inherited writer. On Windows that can
outlive the launcher indefinitely, even after subprocess.run's timeout. Real
files let us wait only for the launcher and retain diagnostics for inspection.
"""
from __future__ import annotations

import os
import subprocess
import uuid
from pathlib import Path
from typing import Sequence


def _tail(path: Path, encoding: str, errors: str, limit: int = 1024 * 1024) -> str:
    with path.open("rb") as stream:
        stream.seek(max(0, path.stat().st_size - limit))
        return stream.read(limit).decode(encoding, errors=errors)


def run_background_launcher(
    command: Sequence[str], *, cwd: Path, timeout: float,
    log_dir: Path | None = None, encoding: str = "utf-8", errors: str = "replace",
) -> subprocess.CompletedProcess[str]:
    if timeout <= 0:
        raise ValueError("launcher timeout must be positive")
    directory = Path(log_dir) if log_dir else Path(cwd) / "runtime" / "launcher-logs"
    directory.mkdir(parents=True, exist_ok=True)
    run_id = uuid.uuid4().hex
    stdout_path = directory / f"{run_id}.stdout.log"
    stderr_path = directory / f"{run_id}.stderr.log"
    environment = {**os.environ, "PYTHONIOENCODING": "utf-8"}
    try:
        with stdout_path.open("wb") as stdout, stderr_path.open("wb") as stderr:
            result = subprocess.run(
                list(command), cwd=cwd, timeout=timeout, stdin=subprocess.DEVNULL,
                stdout=stdout, stderr=stderr, env=environment,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
    except subprocess.TimeoutExpired as error:
        # No PIPE handles exist, so run() kills/waits only for its direct child;
        # retained daemon handles cannot make the timeout cleanup await EOF.
        error.output = _tail(stdout_path, encoding, errors)
        error.stderr = _tail(stderr_path, encoding, errors)
        error.stdout_path, error.stderr_path = stdout_path, stderr_path
        raise
    completed = subprocess.CompletedProcess(
        result.args, result.returncode,
        _tail(stdout_path, encoding, errors), _tail(stderr_path, encoding, errors),
    )
    completed.stdout_path, completed.stderr_path = stdout_path, stderr_path
    return completed
