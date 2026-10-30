"""Private stdin/stdout IPC for the installation's browser collectors."""
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
from selection_automation.captcha import step, reply

if __name__ == '__main__':
    try:
        raw = sys.stdin.read(4_000_001)
        if len(raw) > 4_000_000:
            raise ValueError('CAPTCHA request too large')
        result = step(ROOT, json.loads(raw))
    except Exception:
        # SDK and HTTP exceptions can contain the key or response payload.
        # Expose only a fixed message at this process boundary.
        result = reply('SERVICE_FAILED')
    sys.stdout.write(json.dumps(result, ensure_ascii=False))
