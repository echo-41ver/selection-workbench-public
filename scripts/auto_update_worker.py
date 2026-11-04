"""Apply a verified update outside the console, then make sure the UI returns."""
from __future__ import annotations

import argparse
import subprocess
import sys
import time
import urllib.request
from pathlib import Path


def alive(port, timeout=1.5):
    try:
        with urllib.request.urlopen(f'http://127.0.0.1:{port}/api/health', timeout=timeout) as response:
            return response.status == 200
    except Exception:
        return False


def relaunch(root, port):
    if alive(port):
        return
    logs = Path(root) / 'runtime/web-console'
    logs.mkdir(parents=True, exist_ok=True)
    with (logs / f'service-{port}.log').open('ab') as output:
        subprocess.Popen(
            [sys.executable, str(Path(root) / 'scripts/web_console.py'), '--host', '127.0.0.1', '--port', str(port)],
            cwd=str(root), stdin=subprocess.DEVNULL, stdout=output, stderr=subprocess.STDOUT,
            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument('--package-root', required=True)
    parser.add_argument('--install-root', required=True)
    parser.add_argument('--port', type=int, default=8765)
    parser.add_argument('--wait-console', type=float, default=25.0)
    args = parser.parse_args(argv)
    root = Path(args.install_root).resolve()
    # The caller exits as soon as this process starts, so give it a moment.
    time.sleep(2.0)
    try:
        code = subprocess.call([sys.executable, str(root / 'scripts/apply_update.py'),
                                '--package-root', args.package_root, '--install-root', str(root)])
    except Exception:
        code = 1
    deadline = time.time() + args.wait_console
    while time.time() < deadline and not alive(args.port):
        time.sleep(1.0)
    relaunch(root, args.port)
    return 0 if code == 0 else 1


if __name__ == '__main__':
    raise SystemExit(main())
