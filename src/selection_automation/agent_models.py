"""Read model catalogs through each installed agent's native control interface."""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import json
import os
import queue
import re
import subprocess
import threading
import time

_cache={}
_lock=threading.RLock()


class Peer:
    def __init__(self,command,cwd,env):
        self.process=subprocess.Popen(command,cwd=cwd,env=env,stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0)
        self.messages=queue.Queue();self.sequence=0
        def reader():
            for line in self.process.stdout:
                try:self.messages.put(json.loads(line.decode('utf-8-sig')))
                except (ValueError,UnicodeError):pass
            self.messages.put(None)
        threading.Thread(target=reader,daemon=True).start()

    def send(self,message):
        self.process.stdin.write((json.dumps(message)+'\n').encode());self.process.stdin.flush()

    def wait(self,predicate,timeout=25):
        deadline=time.monotonic()+timeout
        while time.monotonic()<deadline:
            try:message=self.messages.get(timeout=max(.01,deadline-time.monotonic()))
            except queue.Empty:break
            if message is None:break
            if predicate(message):return message
        raise RuntimeError('Agent 模型列表接口未响应，请检查登录和客户端版本')

    def rpc(self,method,params):
        self.sequence+=1;identifier=self.sequence
        self.send({'jsonrpc':'2.0','id':identifier,'method':method,'params':params})
        message=self.wait(lambda row:row.get('id')==identifier)
        if message.get('error'):raise RuntimeError('Agent 未提供可用模型列表，请检查登录与客户端版本')
        return message.get('result',{})

    def close(self):
        if self.process.poll() is None:self.process.terminate()
        try:self.process.wait(timeout=5)
        except subprocess.TimeoutExpired:self.process.kill();self.process.wait(timeout=5)


def _model(identifier,name=None,vision=None,**extra):
    if not isinstance(identifier,str) or not identifier.strip() or len(identifier)>200 or any(ord(c)<32 for c in identifier):return None
    return {'id':identifier,'name':str(name or identifier),'vision':vision,**extra}


def _codex(root,agent):
    from .local_agents import command_prefix,environment
    peer=Peer(command_prefix(agent['executable'],'codex')+['app-server'],root,environment())
    try:
        peer.rpc('initialize',{'clientInfo':{'name':'selection-model-reader','version':'1.0'}})
        peer.send({'method':'initialized'})
        rows=[];cursor=None
        for _ in range(10):
            params={'limit':100,'includeHidden':False}
            if cursor:params['cursor']=cursor
            data=peer.rpc('model/list',params)
            rows.extend(data.get('data',[]));cursor=data.get('nextCursor')
            if not cursor:break
        models=[_model(row.get('model') or row.get('id'),row.get('displayName'),
            'image' in row['inputModalities'] if isinstance(row.get('inputModalities'),list) else None,
            efforts=[e['reasoningEffort'] for e in row.get('supportedReasoningEfforts',[])],
            default_effort=row.get('defaultReasoningEffort'),is_default=bool(row.get('isDefault')))
            for row in rows if not row.get('hidden')]
        return models,next((m['id'] for m in models if m and m['is_default']),None),'Codex app-server model/list'
    finally:peer.close()


def _acp(root,agent):
    from .local_agents import command_prefix,environment,workbuddy_environment
    env=environment();prefix=command_prefix(agent['executable'],agent['id'])
    if agent['id']=='workbuddy':
        env.update(workbuddy_environment(agent['executable']))
        args=['--acp','--no-session-persistence','--strict-mcp-config','--mcp-config','{"mcpServers":{}}']
    else:
        help_text=subprocess.run(prefix+['--help'],capture_output=True,text=True,encoding='utf-8',errors='replace',env=env,timeout=15).stdout
        flag='--acp' if re.search(r'--acp\b',help_text) else '--experimental-acp'
        if flag not in help_text:raise RuntimeError('此版本 Agent 尚未提供模型读取接口，请更新客户端')
        args=[flag]
    peer=Peer(prefix+args,root,env)
    try:
        peer.rpc('initialize',{'protocolVersion':1,'clientCapabilities':{},'clientInfo':{'name':'selection-model-reader','version':'1.0'}})
        data=peer.rpc('session/new',{'cwd':str(Path(root).resolve()),'mcpServers':[]})
        state=data.get('models') or {};models=[]
        for row in state.get('availableModels',[]):
            models.append(_model(row.get('modelId'),row.get('name'),(row.get('_meta') or {}).get('supportsImages')))
        default=state.get('currentModelId')
        if not models:
            for option in data.get('configOptions',[]):
                if option.get('category')!='model' and option.get('id')!='model':continue
                default=option.get('currentValue')
                for row in option.get('options',[]):
                    for item in row.get('options',[row]):models.append(_model(item.get('value'),item.get('name')))
        return models,default,agent['name']+' ACP session/new'
    finally:peer.close()


def _claude(root,agent):
    from .local_agents import command_prefix,environment
    peer=Peer(command_prefix(agent['executable'],'claude')+['--print','--input-format','stream-json',
        '--output-format','stream-json','--verbose','--no-session-persistence','--strict-mcp-config','--mcp-config','{"mcpServers":{}}'],root,environment())
    try:
        peer.send({'type':'control_request','request_id':'selection-model-reader','request':{'subtype':'initialize','hooks':{}}})
        reply=peer.wait(lambda row:row.get('type')=='control_response' and row.get('response',{}).get('request_id')=='selection-model-reader')
        response=reply.get('response',{})
        if response.get('subtype')=='error':raise RuntimeError('Claude 模型列表读取失败，请检查登录')
        data=response.get('response',{})
        return [_model(r.get('value') or r.get('id'),r.get('displayName') or r.get('name')) for r in data.get('models',[])],None,'Claude SDK initialize.models'
    finally:peer.close()


def _opencode(root,agent):
    from .local_agents import command_prefix,environment
    result=subprocess.run(command_prefix(agent['executable'],'opencode')+['models'],cwd=root,env=environment(),
        capture_output=True,text=True,encoding='utf-8',errors='replace',timeout=30)
    if result.returncode:raise RuntimeError('OpenCode 模型列表读取失败，请检查已配置的服务商')
    text=re.sub(r'\x1b\[[0-9;]*m','',result.stdout)
    rows=[_model(line.strip()) for line in text.splitlines() if re.fullmatch(r'[\w.-]+/[\w./:-]+',line.strip())]
    return rows,None,'OpenCode models'


def _accio(root,agent):
    from .desktop_agents import accio_gateway,gateway_request
    data=gateway_request(accio_gateway(),'/models');models=[]
    for provider in data.get('data',[]):
        for row in provider.get('modelList',[]):
            if row.get('visible') is False or row.get('disabled') is True:continue
            models.append(_model(row.get('modelCode') or row.get('modelName'),row.get('modelDisplayName'),row.get('multimodal'),
                provider=provider.get('providerDisplayName',provider.get('provider')),
                efforts=row.get('reasoningEfforts',[]),default_effort=row.get('defaultReasoningEffort')))
    return models,None,'AccioWork 本机网关 /models'


def _click(cdp,selector):
    point=cdp.evaluate("(()=>{const e=document.querySelector("+json.dumps(selector)+");if(!e||!e.getClientRects().length)return null;const r=e.getBoundingClientRect();return {x:r.x+r.width/2,y:r.y+r.height/2};})()")
    if not point:raise RuntimeError('豆包模型选择入口不可用')
    for kind in ('mousePressed','mouseReleased'):cdp.call('Input.dispatchMouseEvent',{'type':kind,**point,'button':'left','clickCount':1})


def _escape(cdp):
    for kind in ('keyDown','keyUp'):cdp.call('Input.dispatchKeyEvent',{'type':kind,'key':'Escape','code':'Escape','windowsVirtualKeyCode':27})


def doubao_menu(cdp):
    from .agent_blockers import require_doubao_ready
    require_doubao_ready(cdp)
    current=cdp.evaluate("document.querySelector('[data-testid=chat_input_action_model]')?.innerText")
    _click(cdp,'[data-testid=chat_input_action_model]')
    deadline=time.monotonic()+3
    while time.monotonic()<deadline:
        require_doubao_ready(cdp)
        rows=cdp.evaluate("[...document.querySelectorAll('[role=menuitem],[role=option],[role=menuitemradio]')].filter(e=>e.getClientRects().length).map(e=>e.innerText.trim())")
        if rows:return [_model(' '.join(name.split()),' '.join(name.split())) for name in rows if name.strip()],' '.join((current or '').split())
        time.sleep(.1)
    raise RuntimeError('豆包未返回模型菜单，请检查客户端版本')


def select_doubao_model(cdp,model):
    from .agent_blockers import require_doubao_ready
    require_doubao_ready(cdp)
    current=cdp.evaluate("document.querySelector('[data-testid=chat_input_action_model]')?.innerText")
    if ' '.join((current or '').split())==model:return
    try:
        rows,_=doubao_menu(cdp)
        if model not in {row['id'] for row in rows if row}:raise RuntimeError('所选豆包模型已不可用，未发送任务')
        selected=cdp.evaluate("(()=>{const e=[...document.querySelectorAll('[role=menuitem],[role=option],[role=menuitemradio]')].find(e=>e.getClientRects().length&&e.innerText.trim().replace(/\\s+/g,' ')==="+json.dumps(model)+");if(!e||e.getAttribute('aria-disabled')==='true')return false;e.click();return true;})()")
        if not selected:raise RuntimeError('所选豆包模型已不可用，未发送任务')
        for _ in range(100):
            value=cdp.evaluate("document.querySelector('[data-testid=chat_input_action_model]')?.innerText")
            if ' '.join((value or '').split())==model:return
            time.sleep(.1)
        raise RuntimeError('豆包未确认模型切换，未发送任务')
    finally:_escape(cdp)


def _doubao(root,agent):
    from .native_agent_transport import CDP,doubao_targets
    from .process_lock import ProcessLock
    path=Path(os.environ.get('LOCALAPPDATA',Path.home()))/'SelectionWorkbench/agent-locks/doubao.lock'
    path.parent.mkdir(parents=True,exist_ok=True)
    with ProcessLock(path):
        for target in doubao_targets(root,include_owned=False):
            cdp=CDP(target['webSocketDebuggerUrl'])
            try:
                if not cdp.evaluate("Boolean(document.querySelector('[data-testid=chat_input_action_model]'))"):continue
                rows,current=doubao_menu(cdp)
                return rows,current,'豆包客户端实际模型菜单'
            finally:_escape(cdp);cdp.close()
    raise RuntimeError('豆包模型选择入口不可用')


def catalog(root,agent_id,refresh=False,agent=None):
    from .local_agents import discover
    agent=agent or next(a for a in discover(root) if a['id']==agent_id)
    result={'id':agent_id,'name':agent['name'],'models':[],'default':None,'source':None,'status':'unavailable','message':'尚未读取模型'}
    if not agent['installed']:return {**result,'status':'not_installed','message':'未安装'}
    if agent.get('authenticated') is False:return {**result,'message':'请先在该 Agent 中登录'}
    stamp=Path(agent['executable']).stat()
    key=(str(Path(root).resolve()),agent_id,agent['executable'],stamp.st_mtime_ns,stamp.st_size)
    cache_path=Path(root)/'runtime/agent-config/model-catalogs'/f'{agent_id}.json'
    with _lock:
        cached=_cache.get(key)
        if not refresh and cached and time.monotonic()-cached[0]<300:return dict(cached[1])
    readers={'codex':_codex,'claude':_claude,'gemini':_acp,'opencode':_opencode,'workbuddy':_acp,'acciowork':_accio,'doubao':_doubao}
    try:
        rows,default,source=readers[agent_id](Path(root),agent)
        seen=set();models=[]
        for row in rows:
            if row and row['id'] not in seen:models.append(row);seen.add(row['id'])
        if not models:raise RuntimeError('该 Agent 未提供模型列表，请检查登录、权限或客户端版本')
        result.update(models=models,default=default,source=source,status='ready',message='已从本机 Agent 读取')
    except (OSError,ValueError,RuntimeError,subprocess.TimeoutExpired,queue.Empty) as error:
        from .agent_blockers import AgentActionRequired
        if agent_id=='doubao' and type(error) is RuntimeError and str(error)=='Another supervisor owns this installation':
            # Model menus share the native upload composer. Never interrupt it;
            # retain recently observed models, labelled as cached rather than
            # claiming another live menu read. Runtime caches are not deployed.
            try:
                prior=json.loads(cache_path.read_text(encoding='utf-8'))
                if (time.time()-cache_path.stat().st_mtime<900 and prior.get('id')==agent_id
                        and prior.get('status')=='ready' and prior.get('models')
                        and prior.get('native_identity',list(key[1:]))==list(key[1:])):
                    return {**prior,'cached':True,'message':'豆包发送队列忙，暂用本机最近读取的模型列表；可稍后刷新。'}
            except (OSError,ValueError):pass
            result['message']='豆包发送队列忙，请稍后刷新模型列表'
            return result
        result['message']=str(error) if type(error) is RuntimeError or isinstance(error,AgentActionRequired) else '模型读取失败，请检查客户端与网络'
        if isinstance(error,AgentActionRequired):result.update(status='action_required',action_required=True,code=error.code)
    if result['status']=='ready':
        result['native_identity']=list(key[1:])
        with _lock:_cache[key]=time.monotonic(),dict(result)
        from .merge_temu_snapshots import atomic_write
        atomic_write(cache_path,json.dumps(result,ensure_ascii=False))
    return result


def catalogs(root,ids=None,refresh=False):
    from .local_agents import discover
    agents=[a for a in discover(root) if not ids or a['id'] in ids]
    with ThreadPoolExecutor(max_workers=4) as pool:return list(pool.map(lambda a:catalog(root,a['id'],refresh,a),agents))


def validate_selection(root,models):
    for agent_id,model in models.items():
        data=catalog(root,agent_id)
        chosen=next((row for row in data['models'] if row['id']==model),None)
        if not chosen:raise ValueError('所选模型不在该本机 Agent 的可用列表中，请重新检测模型')
        if chosen.get('vision') is False:raise ValueError('所选模型不支持图片，不能用于商品看图分析')
