"""Installed desktop-agent entry points and app-owned local transport discovery."""
from pathlib import Path
import base64
import json
import os
import re
import subprocess
import sys
import time
from urllib.parse import urlsplit
from urllib.request import Request,build_opener,ProxyHandler

DESKTOP_IDS=('workbuddy','acciowork','doubao')
NAMES={'workbuddy':'WorkBuddy（内置 CLI）','acciowork':'AccioWork','doubao':'豆包'}


def installations(agent_id):
    home=Path.home();local=Path(os.environ.get('LOCALAPPDATA',home/'AppData/Local'))
    program=Path(os.environ.get('ProgramFiles','C:/Program Files'))
    candidates={
        'workbuddy':[program/'WorkBuddyAI/WorkBuddyAI.exe',local/'Programs/WorkBuddyAI/WorkBuddyAI.exe',local/'WorkBuddyAI/WorkBuddyAI.exe'],
        'acciowork':[program/'Accio/Accio.exe',local/'Programs/Accio/Accio.exe',local/'Accio/Accio.exe'],
        'doubao':[local/'Doubao/Application/app/Doubao.exe',local/'Doubao/Application/Doubao.exe',program/'Doubao/Doubao.exe'],
    }[agent_id]
    return next((p.resolve() for p in candidates if p.is_file()),None)


def workbuddy_cli():
    app=installations('workbuddy')
    if app:
        entry=app.parent/'resources/app.asar.unpacked/cli/bin/codebuddy'
        if entry.is_file():return entry
    import shutil
    value=shutil.which('codebuddy')
    return Path(value) if value else None


def local_url(value,schemes=('http',)):
    try:p=urlsplit(value);port=p.port
    except (ValueError,TypeError):raise ValueError('本机 Agent 地址无效') from None
    if p.scheme not in schemes or p.hostname not in {'127.0.0.1','localhost','::1'} or not port or p.username or p.password or p.query or p.fragment:
        raise ValueError('Agent 控制接口必须为无凭据的本机回环地址')
    return p


def owned_listener(port,agent_id,pid=None):
    import psutil
    installed=installations(agent_id)
    if not installed:return False
    for connection in psutil.net_connections(kind='tcp'):
        if connection.status!=psutil.CONN_LISTEN or connection.laddr.port!=port or not connection.pid:continue
        if agent_id=='doubao' and connection.laddr.ip not in {'127.0.0.1','::1'}:continue
        try:
            process=psutil.Process(connection.pid)
            chain=[process,*process.parents()]
            if pid and not any(p.pid==pid for p in chain):continue
            if any(Path(p.exe()).resolve()==installed for p in chain):return True
        except (psutil.Error,OSError):pass
    return False


def accio_gateway():
    # This is the app's deliberately exported local CLI pairing file, not its
    # account/OAuth store. Never print or copy the local gateway password.
    root=Path.home()/'.accio/accounts'
    for file in sorted(root.glob('*/.accio/runtime/gateway-cli.json'),key=lambda p:p.stat().st_mtime,reverse=True):
        try:
            cfg=json.loads(file.read_text(encoding='utf-8'))
            parsed=local_url(cfg['url'])
            if parsed.path not in ('','/') or cfg.get('schemaVersion')!=1:continue
            if owned_listener(parsed.port,'acciowork',cfg.get('pid')):return cfg
        except (OSError,ValueError,KeyError):pass
    raise RuntimeError('AccioWork 本机任务网关未就绪，请打开客户端并登录后重新连接')


def gateway_headers(config):
    headers={'Content-Type':'application/json'}
    if config.get('authMode')=='basic':
        text=str(config.get('username',''))+':'+str(config.get('password',''))
        headers['Authorization']='Basic '+base64.b64encode(text.encode()).decode()
    return headers


def gateway_request(config,path,body=None):
    if not path.startswith('/') or path.startswith('//'):raise ValueError('本机路径无效')
    request=Request(config['url'].rstrip('/')+path,
        data=json.dumps(body,ensure_ascii=False).encode() if body is not None else None,
        headers=gateway_headers(config),method='POST' if body is not None else 'GET')
    with build_opener(ProxyHandler({})).open(request,timeout=20) as response:return json.load(response)


def doubao_port(root):
    file=Path(root)/'runtime/agent-config/doubao-control.json'
    try:
        cfg=json.loads(file.read_text(encoding='utf-8'))
        if cfg.get('enabled') and type(cfg.get('port')) is int and 1024<=cfg['port']<=65535:return cfg['port']
    except (OSError,ValueError):pass
    return 9225


def doubao_endpoint(root):
    port=doubao_port(root)
    if not owned_listener(port,'doubao'):raise RuntimeError('豆包本机控制未启用；首次连接需要退出并重启豆包')
    return 'http://127.0.0.1:'+str(port)


def probe(root,agent_id):
    app=installations(agent_id)
    result={'id':agent_id,'name':NAMES[agent_id],'installed':bool(app),'desktop_path':str(app) if app else None,
            'executable':str(app) if app else None,'version':None,'authenticated':None,'connected':False,
            'ready':False,'status':'未安装','checked_at':time.time(),
            'transport_available':False,
            'recommended_parallel':6,'max_group_size':12}
    if not app:return result
    if agent_id=='workbuddy':
        entry=workbuddy_cli();result['executable']=str(entry) if entry else None
        result['transport']='codebuddy-cli';result['status']='已安装，待验证内置 CLI'
    elif agent_id=='acciowork':
        result['transport']='local-gateway'
        try:
            cfg=accio_gateway();auth=gateway_request(cfg,'/auth/status')
            result['transport_available']=True
            result['authenticated']=auth.get('authenticated') is True
            result['status']='本机网关已连接，待看图验证' if result['authenticated'] else '请在 AccioWork 中完成登录'
        except Exception:result['status']='已安装；请打开 AccioWork 并登录'
    else:
        result['transport']='desktop-cdp';result['requires_enable']=True
        try:doubao_endpoint(root);result.update(requires_enable=False,transport_available=True,status='本机控制已连接，待看图验证')
        except RuntimeError:result['status']='已安装；首次需启用仅本机控制并重启客户端'
    return result


def open_app(agent_id):
    app=installations(agent_id)
    if not app:raise ValueError('未找到该 Agent 的安装目录')
    subprocess.Popen([str(app)],stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,
                     creationflags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0)


def enable_doubao(root,confirmation):
    if confirmation!='restart-doubao-and-enable-loopback-control':raise ValueError('需要确认重启豆包并启用本机控制')
    import psutil
    app=installations('doubao')
    if not app:raise ValueError('豆包未安装')
    port=9225
    # Do not claim or terminate a different application's chosen port.
    listeners=[c for c in psutil.net_connections(kind='tcp') if c.status==psutil.CONN_LISTEN and c.laddr.port==port]
    if listeners and not owned_listener(port,'doubao'):raise RuntimeError('9225 端口由其他程序占用，未修改任何程序')
    if not listeners:
        owned=[]
        current_user=psutil.Process().username()
        for process in psutil.process_iter(['pid','exe','create_time']):
            try:
                if process.info['exe'] and Path(process.info['exe']).resolve()==app and process.username()==current_user:owned.append(process)
            except (psutil.Error,OSError):pass
        # The explicit action describes the restart and loss of unsent drafts.
        for process in reversed(owned):
            try:process.terminate()
            except psutil.NoSuchProcess:pass
        _,alive=psutil.wait_procs(owned,timeout=10)
        if alive:raise RuntimeError('豆包尚未完全退出，未强制杀死未确认进程，请从托盘退出后重试')
        subprocess.Popen([str(app),'--remote-debugging-address=127.0.0.1','--remote-debugging-port='+str(port)],
            stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
        deadline=time.monotonic()+30
        while time.monotonic()<deadline:
            if owned_listener(port,'doubao'):break
            time.sleep(.3)
        else:raise RuntimeError('豆包尚未开放仅本机控制接口，请检查客户端启动情况')
    from .merge_temu_snapshots import atomic_write
    file=Path(root)/'runtime/agent-config/doubao-control.json';file.parent.mkdir(parents=True,exist_ok=True)
    atomic_write(file,json.dumps({'enabled':True,'port':port,'confirmed_at':time.time()}))
    return {'ok':True,'message':'豆包已按确认开启仅本机控制；请完成登录后点击连接测试。'}


def command(root,agent,job):
    if agent['id']=='workbuddy':
        from .local_agents import command_prefix
        try:probe=json.loads((Path(job)/'transport-result.json').read_text(encoding='utf-8')).get('type')=='vision_probe'
        except (OSError,ValueError):probe=False
        if probe:
            return command_prefix(agent['executable'],'workbuddy')+(['--model',agent['selected_model']] if agent.get('selected_model') else [])+[
                '-p','--output-format','json','--no-session-persistence','--permission-mode','acceptEdits',
                '--allowedTools','Read,Write,Edit','--strict-mcp-config','--mcp-config','{"mcpServers":{}}','--add-dir',str(job)]
    return [sys.executable,str(Path(root)/'scripts/run_desktop_agent.py'),
            '--agent',agent['id'],'--job',str(job),*(['--model',agent['selected_model']] if agent.get('selected_model') else [])]
