"""Attach this installation to Codex and verify its local stdio connection."""
import argparse
import hashlib
import json
from pathlib import Path
import queue
import subprocess
import sys
import threading
import time

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'src'))
from selection_automation.local_agents import command_prefix, discover, environment, check_connection, login
from selection_automation.merge_temu_snapshots import atomic_write


def mcp_ready(root):
    child=subprocess.Popen([sys.executable,str(root/'scripts/mcp_server.py')],cwd=root,env=environment(),
        stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,
        creationflags=subprocess.CREATE_NO_WINDOW if sys.platform=='win32' else 0)
    messages=queue.Queue()
    def receive():
        for line in child.stdout:
            try:messages.put(json.loads(line))
            except ValueError:pass
    threading.Thread(target=receive,daemon=True).start()
    try:
        child.stdin.write((json.dumps({'jsonrpc':'2.0','id':1,'method':'initialize',
            'params':{'protocolVersion':'2024-11-05','capabilities':{},'clientInfo':{'name':'selection-deploy','version':'1'}}})+'\n').encode());child.stdin.flush()
        initialized=messages.get(timeout=20)
        if initialized.get('id')!=1 or 'error' in initialized:return False
        child.stdin.write((json.dumps({'jsonrpc':'2.0','method':'notifications/initialized'})+'\n'+
                           json.dumps({'jsonrpc':'2.0','id':2,'method':'tools/list','params':{}})+'\n').encode());child.stdin.flush()
        response=messages.get(timeout=20)
        tools=response.get('result',{}).get('tools',[])
        return response.get('id')==2 and any(row.get('name')=='selection_status' for row in tools)
    except (queue.Empty,OSError,ValueError):
        return False
    finally:
        if child.poll() is None:
            child.terminate()
        try:child.wait(timeout=5)
        except subprocess.TimeoutExpired:child.kill();child.wait(timeout=5)


def register_codex(root, agent):
    prefix=command_prefix(agent['executable'],'codex')
    python=str(root/'.venv/Scripts/python.exe')
    script=str(root/'scripts/mcp_server.py')
    def existing(name):
        result=subprocess.run(prefix+['mcp','get',name,'--json'],capture_output=True,
                              text=True,encoding='utf-8',errors='replace',env=environment(),timeout=15)
        try:return json.loads(result.stdout) if result.returncode==0 else None
        except ValueError:return None
    name='amazon-temu-selection'
    previous=existing(name)
    def matches(value):
        transport=(value or {}).get('transport',value or {})
        return (transport.get('command')==python and transport.get('args')==[script])
    if previous and not matches(previous):
        name+='-'+hashlib.sha256(str(root).casefold().encode()).hexdigest()[:8]
        previous=existing(name)
        if previous and not matches(previous):
            return {'registered':False,'message':'同名 MCP 属于其他安装，已保留其配置'}
    if not previous:
        result=subprocess.run(prefix+['mcp','add',name,'--env','PYTHONUTF8=1',
            '--env','PYTHONIOENCODING=utf-8','--',python,script],capture_output=True,
            env=environment(),timeout=15,creationflags=subprocess.CREATE_NO_WINDOW if sys.platform=='win32' else 0)
        if result.returncode:return {'registered':False,'message':'Codex MCP 注册失败，原配置保留'}
    return {'registered':matches(existing(name)),'name':name}


def connect(root, vision=False, login_if_needed=False):
    root=Path(root).resolve()
    rows=discover(root,refresh=True)
    codex=next(row for row in rows if row['id']=='codex')
    result={'mcp_ready':mcp_ready(root),'agents':rows,'checked_at':time.time()}
    if codex['installed']:
        try:result['codex_mcp']=register_codex(root,codex)
        except (OSError,subprocess.TimeoutExpired,RuntimeError):result['codex_mcp']={'registered':False}
        if vision and codex['authenticated']:
            checked=check_connection(root,'codex')
            result['agents']=[checked if row['id']=='codex' else row for row in rows]
        elif login_if_needed and codex['authenticated'] is False:
            login(root,'codex');result['login_required']=True
    output=root/'runtime/agent-config/connection.json';output.parent.mkdir(parents=True,exist_ok=True)
    atomic_write(output,json.dumps(result,ensure_ascii=False,indent=2))
    return result


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--vision',action='store_true');parser.add_argument('--login-if-needed',action='store_true')
    args=parser.parse_args();result=connect(ROOT,args.vision,args.login_if_needed)
    print(json.dumps(result,ensure_ascii=False))
