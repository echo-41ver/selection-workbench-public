"""Persistent dispatch controls. In-flight work is always allowed to settle."""
from pathlib import Path
import json
import time
from .merge_temu_snapshots import atomic_write


def read_control(folder):
    try:return json.loads((Path(folder)/'runtime_control.json').read_text(encoding='utf-8'))
    except (OSError,ValueError):return {}


def update_control(folder, **changes):
    value={**read_control(folder),**changes,'updated_at':time.time()}
    atomic_write(Path(folder)/'runtime_control.json',json.dumps(value,ensure_ascii=False))
    return value


def dispatch_allowed(folder, agent=None):
    value=read_control(folder)
    return not value.get('paused') and not value.get('switch_pending') and agent not in value.get('paused_agents',[])
