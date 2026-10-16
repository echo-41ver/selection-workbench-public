"""Verify/repair local collection dependencies, without any model API request."""
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT/'src'))
from selection_automation.node_runtime import ensure_node_runtime, NodeRuntimeError

if __name__ == '__main__':
    try:
        print(json.dumps(ensure_node_runtime(ROOT), ensure_ascii=False))
    except (NodeRuntimeError, RuntimeError) as error:
        print(json.dumps({'ready': False, 'message': str(error)}, ensure_ascii=False))
        raise SystemExit(1)
