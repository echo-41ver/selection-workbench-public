"""Discover installed CLI agents without reading their credential stores."""
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import threading
import time

IDS = ('codex', 'claude', 'gemini', 'opencode', 'workbuddy', 'acciowork', 'doubao')
NAMES = {'codex':'Codex CLI', 'claude':'Claude Code', 'gemini':'Gemini CLI', 'opencode':'OpenCode',
         'workbuddy':'WorkBuddy', 'acciowork':'AccioWork', 'doubao':'豆包'}
_cache = {}
_lock = threading.RLock()


def environment():
    # Do not pass the workbench's remote decision keys to a local reviewer.
    omitted = {'DEEPSEEK_API_KEY','OPENAI_API_KEY','LIQUID_API_KEY','D1_API_KEY',
               'SELECTION_STAGE3_API_KEY','SELECTION_STAGE3_ENDPOINT'}
    env = {k:v for k,v in os.environ.items() if k.upper() not in omitted}
    env.update(PYTHONUTF8='1', PYTHONIOENCODING='utf-8', CI='1', NO_COLOR='1')
    if os.name=='nt' and not env.get('HTTPS_PROXY') and not env.get('https_proxy'):
        import winreg
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER,r'Software\Microsoft\Windows\CurrentVersion\Internet Settings') as registry:
                enabled=winreg.QueryValueEx(registry,'ProxyEnable')[0]
                proxy=winreg.QueryValueEx(registry,'ProxyServer')[0]
            if enabled and proxy:
                if '=' in proxy:
                    parts=dict(part.split('=',1) for part in proxy.split(';') if '=' in part)
                    proxy=parts.get('https') or parts.get('http')
                if proxy:
                    address=proxy if '://' in proxy else 'http://'+proxy
                    env.update(HTTP_PROXY=address,HTTPS_PROXY=address)
        except OSError:
            pass
    return env


def find_executable(root, agent_id):
    if agent_id not in IDS:
        raise ValueError('未知 Agent')
    if agent_id in {'workbuddy','acciowork','doubao'}:
        from .desktop_agents import installations,workbuddy_cli
        found=workbuddy_cli() if agent_id=='workbuddy' else installations(agent_id)
        return str(found) if found else None
    root, home = Path(root), Path.home()
    candidates = [root/'runtime/tools'/agent_id/(agent_id+'.exe'),
                  root/'deployment-assets/tools'/agent_id/(agent_id+'.exe')]
    found = shutil.which(agent_id)
    if found:
        candidates.append(Path(found))
    for directory in (home/'.local/bin', home/'scoop/shims',
                      Path(os.environ.get('APPDATA',str(home)))/'npm',
                      Path(os.environ.get('LOCALAPPDATA',str(home)))/'Programs'/agent_id):
        candidates.extend(directory/(agent_id+suffix) for suffix in ('.exe','.cmd',''))
    if agent_id == 'codex':
        folder = Path(os.environ.get('LOCALAPPDATA', str(home)))/'OpenAI/Codex/bin'
        candidates.extend(sorted(folder.glob('*/codex.exe'), key=lambda p:p.stat().st_mtime, reverse=True))
    return next((str(p.resolve()) for p in candidates if p.is_file()), None)


def command_prefix(executable, agent_id):
    path = Path(executable)
    if agent_id=='workbuddy' and path.suffix.lower()!='.exe':
        node=shutil.which('node')
        if not node:raise RuntimeError('WorkBuddy 内置 CLI 需要 Node.js')
        return [node,str(path)]
    if path.suffix.lower() not in {'.cmd','.bat','.ps1'}:
        return [str(path)]
    # npm's Windows shell shim is not an executable. Invoke its JS entry with
    # Node directly so spaces and metacharacters never pass through cmd.exe.
    entries = {'codex':'@openai/codex/bin/codex.js',
               'claude':'@anthropic-ai/claude-code/cli.js',
               'gemini':'@google/gemini-cli/dist/index.js',
               'opencode':'opencode-ai/bin/opencode'}
    entry = path.parent/'node_modules'/entries[agent_id]
    node = path.parent/'node.exe'
    runtime = str(node) if node.is_file() else shutil.which('node')
    if runtime and entry.is_file():
        return [runtime, str(entry)]
    raise RuntimeError('Agent 的命令包装器缺少 Node 或实际程序入口')


def _run(prefix, args, root, timeout=12, text=None):
    env=environment()
    if any('codebuddy' in str(part).lower() for part in prefix):
        env.update(workbuddy_environment(prefix[-1]))
    return subprocess.run([*prefix,*args], cwd=root, env=env, input=text,
                          capture_output=True, encoding='utf-8', errors='replace', timeout=timeout,
                          creationflags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0)


def connection_receipt(root,agent_id,model=None):
    import hashlib
    suffix='-'+hashlib.sha256(model.encode()).hexdigest()[:12] if model else ''
    return Path(root)/'runtime/agent-config'/f'{agent_id}{suffix}-connection.json'


def _probe_cli(root, agent_id,model=None):
    path = find_executable(root, agent_id)
    result = {'id':agent_id,'name':NAMES[agent_id],'installed':bool(path),'executable':path,
              'version':None,'authenticated':None,'connected':False,'ready':False,
              'status':'未安装','checked_at':time.time()}
    if not path:
        return result
    try:
        prefix = command_prefix(path, agent_id)
        version = _run(prefix,['--version'],root)
        if version.returncode:
            result['status'] = '程序启动失败'
            return result
        match = re.search(r'\b\d+\.\d+[\w.+-]*', version.stdout)
        result['version'] = match.group() if match else 'unknown'
        if agent_id in {'codex','claude'}:
            args = ['login','status'] if agent_id=='codex' else ['auth','status']
            auth = _run(prefix,args,root)
            result['authenticated'] = auth.returncode == 0
            result['status'] = '已登录，待连接验证' if result['authenticated'] else '需要登录'
        else:
            result['status'] = '已发现，待连接验证'
        # Vision capability is bound to installation/version/model, not a one-hour
        # timer. Current login is checked above; a failed new probe replaces it.
        receipt = connection_receipt(root,agent_id,model)
        try:
            saved = json.loads(receipt.read_text(encoding='utf-8'))
            if (saved.get('executable')==path and saved.get('version')==result['version']
                    and saved.get('connected') and saved.get('probe_revision')==2
                    and result['authenticated'] is not False and saved.get('selected_model')==model):
                result.update(authenticated=True, connected=True, ready=True, status='已连接，可看图分析')
            elif (saved.get('executable')==path and saved.get('version')==result['version']
                  and time.time()-saved.get('tested_at',0)<3600 and not saved.get('connected')):
                result['status']='连接或看图验证失败，请检查登录、模型权限与网络'
        except (OSError,ValueError):
            pass
    except subprocess.TimeoutExpired:
        result['status'] = '程序响应超时'
    except (OSError,RuntimeError):
        result['status'] = '程序不可调用'
    return result


def workbuddy_environment(executable):
    entry=Path(executable)
    product=entry.parent.parent/'product.json'
    result={'CODEBUDDY_SKIP_GIT_BASH_CHECK':'1'}
    if product.exists():
        result.update(ACC_PRODUCT_CONFIG_PATH=str(product),CODEBUDDY_CONFIG_DIR=str(Path.home()/'.workbuddy-ai'),
                      WORKBUDDY_CONFIG_DIR=str(Path.home()/'.workbuddy-ai'))
    return result


def _probe(root,agent_id,model=None):
    if agent_id not in {'workbuddy','acciowork','doubao'}:
        result=_probe_cli(root,agent_id,model)
        result.update(recommended_parallel=6,max_group_size=12,selected_model=model)
        return result
    from .desktop_agents import probe
    result=probe(root,agent_id)
    if not result['installed']:return result
    if agent_id=='workbuddy' and result['executable']:
        try:
            version=_run(command_prefix(result['executable'],agent_id),['--version'],root)
            found=re.search(r'\b\d+\.\d+[\w.+-]*',version.stdout)
            result['version']=found.group() if found else None
            if version.returncode:result['status']='WorkBuddy 内置 CLI 启动失败'
        except (OSError,RuntimeError,subprocess.TimeoutExpired):result['status']='WorkBuddy 内置 CLI 不可调用'
    stamp=Path(result['desktop_path']).stat()
    result['installation_stamp']=str(stamp.st_mtime_ns)+':'+str(stamp.st_size)
    try:
        receipt=connection_receipt(root,agent_id,model)
        saved=json.loads(receipt.read_text(encoding='utf-8'))
        if (saved.get('executable')==result['executable'] and saved.get('probe_revision')==2
                and saved.get('version')==result['version'] and saved.get('installation_stamp')==result['installation_stamp']
                and saved.get('connected')
                and (agent_id=='workbuddy' or result.get('transport_available'))
                and result['authenticated'] is not False and not result.get('requires_enable') and saved.get('selected_model')==model):
            result.update(authenticated=True,connected=True,ready=True,status='已连接，可看图分析')
    except (OSError,ValueError):pass
    result['selected_model']=model
    return result


def discover(root, refresh=False,models=None):
    key = str(Path(root).resolve())+('|'+json.dumps(models,sort_keys=True,ensure_ascii=False) if models else '')
    with _lock:
        cached = _cache.get(key)
        if not refresh and cached and time.monotonic()-cached[0]<60:
            return [dict(row) for row in cached[1]]
    with ThreadPoolExecutor(max_workers=4) as pool:
        rows = list(pool.map(lambda agent:_probe(root,agent,(models or {}).get(agent)), IDS))
    with _lock:
        _cache[key] = time.monotonic(), rows
    return [dict(row) for row in rows]


def analysis_command(agent, job, model=None, effort='medium'):
    """All long task instructions go over stdin; native image inputs stay bound."""
    model=model or agent.get('selected_model')
    job = Path(job)
    if agent['id'] in {'workbuddy','acciowork','doubao'}:
        from .desktop_agents import command
        return command(Path(__file__).resolve().parents[2],{**agent,'selected_model':model},job)
    prefix = command_prefix(agent['executable'], agent['id'])
    images = [str(job/'contacts/contact-001.jpg')]
    inputs = json.loads((job/'image-inputs.json').read_text(encoding='utf-8'))
    if inputs.get('attachments'):images=[item['path'] for item in inputs['attachments']]
    else:images.extend(row['path'] for row in inputs.get('images',[]))
    per_drive={}
    for image in images:
        folder=str(Path(image).parent)
        per_drive.setdefault(Path(folder).anchor,[]).append(folder)
    directories=list(dict.fromkeys([str(job),*(os.path.commonpath(folders) for folders in per_drive.values())]))
    if agent['id']=='codex':
        args = ['exec','--ignore-user-config','--ephemeral','--skip-git-repo-check',
                '-m',model or 'gpt-6.1-sol',
                *(['-c',f'model_reasoning_effort="{effort}"'] if effort not in (None,'default') else []),
                '-c','service_tier="default"','-c','approval_policy="never"',
                '-c','sandbox_mode="danger-full-access"','--json',
                '--output-last-message',str(job/'summary.txt')]
        for image in images:
            args.extend(['-i',image])
        return prefix+args+['-']
    if agent['id']=='claude':
        return prefix+(['--model',model] if model else [])+['-p','--output-format','json','--no-session-persistence',
                       '--permission-mode','acceptEdits','--allowedTools','Read,Write,Edit',
                       '--add-dir',*directories]
    if agent['id']=='gemini':
        return prefix+(['--model',model] if model else [])+['--prompt','按标准输入中的任务要求处理；用 read_file 查看其中列出的原图，写入指定结果文件。',
                       '--output-format','json','--approval-mode','auto_edit',
                       *[part for directory in directories[:5] for part in ['--include-directories',directory]]]
    return prefix+['run','--format','json',*(['--model',model] if model else []),*[part for path in images for part in ['--file',path]],
                   '按标准输入中的任务要求处理并写入指定结果文件。']


def check_connection(root, agent_id,model=None):
    """One tiny real image call verifies login, model access and vision together."""
    from PIL import Image, ImageDraw, ImageFont
    import tempfile
    from contextlib import nullcontext
    rows=discover(root,refresh=True,models={agent_id:model}) if model else discover(root,refresh=True)
    agent = next(row for row in rows if row['id']==agent_id)
    if not agent['installed'] or agent['authenticated'] is False:
        return agent
    if agent.get('requires_enable'):return agent
    if agent_id=='acciowork':
        from .desktop_agents import accio_gateway,open_app
        try:accio_gateway()
        except RuntimeError:
            open_app(agent_id)
            deadline=time.monotonic()+25
            while time.monotonic()<deadline:
                try:accio_gateway();break
                except RuntimeError:time.sleep(.5)
    native=agent_id in {'acciowork','doubao'}
    if native:
        parent=Path(root)/'runtime/agent-config/probes'/connection_receipt(root,agent_id,model).stem
        parent.mkdir(parents=True,exist_ok=True)
        pending=[]
        for candidate in parent.iterdir():
            try:
                state=json.loads((candidate/'native-session.json').read_text(encoding='utf-8'))
                if state.get('state')!='complete' and (candidate/'probe-expectation.json').is_file():pending.append(candidate)
            except (OSError,ValueError):pass
        folder=max(pending,key=lambda p:p.stat().st_mtime) if pending else Path(tempfile.mkdtemp(prefix='probe-',dir=parent))
        context=nullcontext(folder)
    else:
        parent=Path(root)/'runtime/agent-config/probes'/connection_receipt(root,agent_id,model).stem
        parent.mkdir(parents=True,exist_ok=True)
        context=nullcontext(Path(tempfile.mkdtemp(prefix='probe-',dir=parent)))
    with context as folder:
        job = Path(folder); (job/'contacts').mkdir(exist_ok=True)
        expectation=job/'probe-expectation.json'
        token = json.loads(expectation.read_text(encoding='utf-8'))['digits'] if expectation.exists() else str(int.from_bytes(os.urandom(3),'big')%900000+100000)
        expectation.write_text(json.dumps({'digits':token}),encoding='utf-8')
        picture = Image.new('RGB',(600,180),'white')
        font_path = Path(os.environ.get('WINDIR','C:/Windows'))/'Fonts/arial.ttf'
        font = ImageFont.truetype(str(font_path),72) if font_path.exists() else ImageFont.load_default(size=72)
        ImageDraw.Draw(picture).text((30,35),token,fill='black',font=font)
        image = job/'contacts/contact-001.jpg'; picture.save(image)
        (job/'image-inputs.json').write_text('{"images":[]}',encoding='utf-8')
        answer_path=job/'vision-probe.txt'
        (job/'transport-result.json').write_text(json.dumps({'type':'vision_probe','path':str(answer_path)}),encoding='utf-8')
        request = (f'Open/read the image at {image}. Read its six digits and write ONLY those digits '
                   f'to {answer_path} using the file write tool. Then return the digits. Do not use shell commands.')
        command = analysis_command(agent,job,effort='default')
        # Non-Codex agents use their native image-reading tool or attachment.
        try:
            response = _run(command,[],job,timeout=120,text=request)
            (job/'stdout.log').write_text(response.stdout or '',encoding='utf-8')
            (job/'stderr.log').write_text(response.stderr or '',encoding='utf-8')
            output = response.stdout
            summary = job/'summary.txt'
            if summary.exists():
                output += summary.read_text(encoding='utf-8')
            connected = (response.returncode==0 and answer_path.is_file()
                         and answer_path.read_text(encoding='utf-8-sig').strip()==token)
        except (subprocess.TimeoutExpired,OSError,RuntimeError):
            connected = False
        agent.update(connected=connected,ready=connected,probe_job=str(job),
                     status='已连接，可看图分析' if connected else '连接或看图验证失败，请检查登录、模型权限与网络')
        if not connected:
            try:
                failure=json.loads((job/'transport-error.json').read_text(encoding='utf-8'))
                agent['status']=failure['message']
            except (OSError,ValueError,KeyError):pass
        if connected:agent['authenticated'] = True
        from .merge_temu_snapshots import atomic_write
        receipt = connection_receipt(root,agent_id,model)
        receipt.parent.mkdir(parents=True,exist_ok=True)
        atomic_write(receipt,json.dumps({'executable':agent['executable'],'version':agent['version'],
                     'tested_at':time.time(),'connected':connected,'probe_revision':2,
                     'installation_stamp':agent.get('installation_stamp'),'selected_model':model},ensure_ascii=False))
    with _lock:
        prefix=str(Path(root).resolve())
        for key in list(_cache):
            if key==prefix or key.startswith(prefix+'|'):_cache.pop(key,None)
    return agent


def ready_agents(root, selected=None, connect=False,models=None):
    found=discover(root,models=models) if models else discover(root)
    rows = [row for row in found if row['installed'] and (not selected or row['id'] in selected)]
    if connect:
        with ThreadPoolExecutor(max_workers=4) as pool:
            rows=list(pool.map(lambda row:check_connection(root,row['id'],(models or {}).get(row['id'])) if not row['ready'] and row['authenticated'] is not False else row,rows))
    return [row for row in rows if row['ready']]


def login(root, agent_id):
    agent = next(row for row in discover(root) if row['id']==agent_id)
    if not agent['installed']:
        raise ValueError('请先安装该 Agent')
    if agent_id in {'workbuddy','acciowork','doubao'}:
        from .desktop_agents import open_app
        open_app(agent_id)
        return {'ok':True,'message':'已打开客户端；完成登录后返回工作台连接测试。'}
    args = {'codex':['login'],'claude':['auth','login'],'gemini':[], 'opencode':['auth','login']}[agent_id]
    prefix = command_prefix(agent['executable'],agent_id)
    # Login is an explicit UI button action; its interactive window must be visible.
    command = '& '+ ' '.join("'"+part.replace("'","''")+"'" for part in prefix+args)
    subprocess.Popen(['powershell.exe','-NoProfile','-NoExit','-Command',command], cwd=root,
                     env=environment(), creationflags=subprocess.CREATE_NEW_CONSOLE if os.name=='nt' else 0)
    return {'ok':True,'message':'已打开 Agent 登录窗口；完成登录后点击重新检测。'}
