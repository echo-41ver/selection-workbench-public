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
    return not value.get('paused') and not value.get('switch_pending') and agent not in value.get('paused_agents',[]) and agent not in value.get('agent_blocks',{})


def block_agent(folder,agent,code,message):
    value=read_control(folder);blocks=dict(value.get('agent_blocks',{}))
    blocks[agent]={'code':code,'message':message,'observed_at':time.time()}
    return update_control(folder,agent_blocks=blocks,paused_agents=sorted(set(value.get('paused_agents',[]))|{agent}))


def clear_agent_block(folder,agent):
    value=read_control(folder);blocks=dict(value.get('agent_blocks',{}));blocks.pop(agent,None)
    return update_control(folder,agent_blocks=blocks,paused_agents=[a for a in value.get('paused_agents',[]) if a!=agent])
