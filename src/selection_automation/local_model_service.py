"""Discover and recover installed local vision services without running inference."""
from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED, TimeoutError as FuturesTimeout
import json
import hashlib
from http.client import HTTPException
import os
import re
from pathlib import Path
import shutil
import socket
import subprocess
import time
from urllib.parse import urlsplit
from urllib.error import HTTPError, URLError
from urllib.request import build_opener, ProxyHandler, Request, HTTPRedirectHandler

from .process_lock import ProcessLock
from .local_model_inventory import service_endpoints, vision_capability, presentation


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class _UnavailableMetadata(OSError):
    """A silent or non-HTTP listener must not consume each protocol timeout."""


def _json(base, path, payload=None):
    request = Request(base + path, data=None if payload is None else json.dumps(payload).encode(),
                      headers={'Content-Type': 'application/json'})
    try:
        with build_opener(ProxyHandler({}), _NoRedirect()).open(request, timeout=1) as response:
            data = response.read(2 * 1024 * 1024 + 1)
    except (HTTPException, TimeoutError, ConnectionError) as error:
        raise _UnavailableMetadata('Local listener does not provide valid HTTP metadata') from error
    except URLError as error:
        if isinstance(error, HTTPError):raise
        raise _UnavailableMetadata('Local listener does not provide valid HTTP metadata') from error
    if len(data) > 2 * 1024 * 1024:
        raise ValueError('Local model metadata is too large')
    return json.loads(data)


def _endpoints():
    return service_endpoints()


def _installed():
    found = shutil.which('ollama')
    if found:
        return found
    if not os.environ.get('LOCALAPPDATA'):
        return None
    candidate = Path(os.environ['LOCALAPPDATA']) / 'Programs/Ollama/ollama.exe'
    return str(candidate) if candidate.is_file() else None


def _start(root):
    executable = _installed()
    if not executable:
        return False
    folder = Path(root) / 'runtime/local-models'
    folder.mkdir(parents=True, exist_ok=True)
    try:
        with ProcessLock(folder / 'recovery.lock'):
            marker = folder / 'start.json'
            if marker.exists() and time.time() - marker.stat().st_mtime < 60:
                return False
            env = {**os.environ, 'OLLAMA_HOST': '127.0.0.1:11434'}
            with (folder / 'ollama.log').open('ab') as log:
                child = subprocess.Popen([executable, 'serve'], env=env, stdin=subprocess.DEVNULL,
                    stdout=log, stderr=subprocess.STDOUT,
                    creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
            marker.write_text(json.dumps({'pid': child.pid, 'started_at': time.time()}), encoding='utf-8')
            return True
    except (OSError, RuntimeError):
        return False


def _tags(base):
    try:
        data = _json(base, '/api/tags')
        return data.get('models') if isinstance(data, dict) and isinstance(data.get('models'), list) else None
    except (OSError, ValueError):
        return None


def _describe(base, row):
    name = row.get('name') or row.get('model')
    if not isinstance(name, str) or not name or len(name) > 200:
        return None
    try:
        info = _json(base, '/api/show', {'model': name})
        capabilities = info.get('capabilities')
        if not isinstance(capabilities, list) or any(
                data.get(key) for data in (row, info) for key in ('remote_host', 'remote_model')):
            return None
        vision = 'vision' in capabilities
        sources = re.findall(r'(?mi)^(?:FROM|ADAPTER|PROJECTOR)\s+(.+)$', info.get('modelfile', ''))
        fingerprint = hashlib.sha256(json.dumps({'sources': sources,
            'model': info.get('model_info'), 'projector': info.get('projector_info'),
            'details': info.get('details')}, sort_keys=True).encode()).hexdigest() if sources else None
        return {'id': name, 'name': name, 'vision': vision, 'source': 'Ollama',
                'base_url': base + '/v1', 'protocol': 'chat_completions', 'size': row.get('size', 0),
                'status': 'ready' if vision else 'text_only', 'identity': fingerprint,
                **presentation(name, info=info)}
    except (OSError, ValueError, AttributeError, TypeError):
        return None


def _probe(base):
    try:
        data=_json(base,'/api/tags')
        if isinstance(data,dict) and isinstance(data.get('models'),list):
            return {'base_url':base,'source':'Ollama','rows':data['models']}
    except _UnavailableMetadata:
        return None
    except HTTPError as error:
        if error.code in (401,403):
            return {'base_url':base,'source':'需授权的本地服务','rows':[],'authentication_required':True}
    except (OSError,ValueError):
        pass
    for path, source, key in (('/api/v1/models', 'LM Studio', 'models'),
                              ('/api/v0/models', 'LM Studio', 'data'),
                              ('/v1/models', 'OpenAI 兼容本地服务', 'data')):
        try:
            data = _json(base, path)
            if isinstance(data, dict) and isinstance(data.get(key), list):
                return {'base_url': base, 'source': source, 'rows': data[key]}
        except HTTPError as error:
            if error.code in (401, 403):
                return {'base_url': base, 'source': '需授权的本地服务', 'rows': [], 'authentication_required': True}
        except (OSError, ValueError):
            pass
    return None


def _probe_native(base):
    from .local_d1 import discover,SOURCE
    if model:=discover(base,_json):
        return {'base_url':base,'source':SOURCE,'rows':[model]}
    return None


def _api_models(service):
    base, source = service['base_url'], service['source']
    from .local_d1 import SOURCE
    if source==SOURCE:return service['rows'],[]
    result, candidates = [], []
    props = None
    if source != 'LM Studio':
        try:
            # In router mode, explicitly prevent metadata reads loading weights.
            props = _json(base, '/props?autoload=false')
        except (OSError, ValueError):
            pass
    for row in service['rows']:
        if not isinstance(row, dict) or any(row.get(key) for key in ('remote_host', 'remote_model', 'remote_url')):
            continue
        name = row.get('key') or row.get('id') or row.get('name')
        if not isinstance(name, str) or not name or len(name) > 200:
            continue
        vision = vision_capability(row)
        row_source = source
        # Server-wide props are valid only when exactly one model is advertised.
        if vision is None and len(service['rows']) == 1 and isinstance(props, dict):
            vision = vision_capability(props)
            if vision is not None:
                row_source = 'llama.cpp'
        model = {'id': name, 'name': str(row.get('display_name') or name)[:240], 'vision': vision,
                 'source': row_source, 'base_url': base + '/v1', 'protocol': 'chat_completions',
                 'size': row.get('size_bytes', row.get('size', 0)),
                 'status': 'ready' if vision is True else 'text_only' if vision is False else 'vision_unknown'}
        model.update(presentation(name, info=row))
        if row.get('type') == 'embedding':
            model.update(vision=False, status='text_only')
        if model['vision'] is True:
            result.append(model)
        else:
            candidates.append(model)
    return result, candidates


def _completed(pending, seconds, cancel=None):
    """Yield metadata completions while keeping cancellation independently live."""
    deadline=time.monotonic()+seconds
    pending=set(pending)
    while pending:
        if cancel is not None and cancel.is_set() or time.monotonic()>=deadline:
            raise FuturesTimeout()
        done,pending=wait(pending,timeout=min(.1,max(0,deadline-time.monotonic())),return_when=FIRST_COMPLETED)
        yield from done


def _catalog_result(models, unconfirmed, services, started, partial, phase='done', checked=0, total=0):
    models = list({(row['base_url'], row['id']): row for row in models}.values())
    models.sort(key=lambda m: (m['id'].startswith('llamacpp:'), urlsplit(m['base_url']).port != 11434,
                              m['size'] if isinstance(m['size'], int) else 0, m['id']))
    grouped = {}
    for model in models:
        identity = model.get('identity') or (model['base_url'], model['id'])
        if identity not in grouped:
            grouped[identity] = {**model, 'connections': []}
        grouped[identity]['connections'].append(dict(model))
    models = list(grouped.values())
    connected = any(not service.get('authentication_required') for service in services)
    from .local_d1 import SOURCE
    native_d1=any(model['source']==SOURCE for model in models)
    message = ('D1 原生图片决策接口已连接；检测未发送推理请求，实际图片推理尚待验证。' if native_d1 else
               '已读取本机实际安装的看图模型；检测不会发送分析请求。' if models else
               '正在读取本机模型服务，已发现结果会立即显示。' if phase!='done' else
               '本机模型服务已连接，但没有检测到已安装的看图模型；其他模型及授权状态见扫描结果。' if connected else
               '本机服务暂未就绪，请检查服务启动日志；其他分析方案不受影响。' if _installed() else
               '未安装可自动启动的 Ollama 服务；可填写自定义本地服务，或选择 API / Agent 分析。')
    return {'models':models,'service_connected':connected,'started':started,'partial':partial,
            'phase':phase,'checked_endpoints':checked,'total_endpoints':total,'message':message,
            'candidates':[dict(row) for row in unconfirmed],
            'services':[{key:value for key,value in item.items() if key!='rows'} for item in services]}


def catalog(root, start=False, *, full=False, cancel=None, preferred=None, on_progress=None):
    deadline=time.monotonic()+(24 if full else 16)
    def stopped():return (cancel is not None and cancel.is_set()) or time.monotonic()>=deadline
    if stopped():return _catalog_result([],[],[],False,True)
    endpoints = _endpoints()
    if preferred:
        address=urlsplit(preferred)
        base=address._replace(path='',query='',fragment='').geturl().rstrip('/')
        if address.hostname in {'localhost','127.0.0.1','::1'}:
            endpoints=[base]+[item for item in endpoints if item!=base]
    services, partial, models, unconfirmed, checked, started = [], False, [], [], 0, False
    from .local_d1 import SOURCE
    def publish(phase):
        if on_progress is not None:
            on_progress(_catalog_result(models,unconfirmed,services,started,True,phase,checked,len(endpoints)))
    publish('services')
    native_bases=set()
    # Probe each listener's native health once before lengthy compatibility
    # probes can occupy all workers. Ports still come from this machine.
    pool=ThreadPoolExecutor(max_workers=min(32,max(1,len(endpoints))))
    pending=[pool.submit(_probe_native,base) for base in endpoints] if not stopped() else []
    try:
        for future in _completed(pending,min(8,max(0,deadline-time.monotonic())),cancel):
            checked+=1
            if service:=future.result():
                services.append(service);models.extend(service['rows']);native_bases.add(service['base_url'])
                publish('native_services')
    except FuturesTimeout:
        partial=True
    finally:
        pool.shutdown(wait=False,cancel_futures=True)
    checked=len(native_bases)
    # Listening ports include silent/non-HTTP services. Bound each first HTTP
    # probe once, and keep a late model listener from sitting behind 80 ports.
    pool = ThreadPoolExecutor(max_workers=min(32,max(1,len(endpoints))))
    pending = [pool.submit(_probe, base) for base in endpoints if base not in native_bases] if not stopped() else []
    try:
        for future in _completed(pending,min(8,max(0,deadline-time.monotonic())),cancel):
            if cancel is not None and cancel.is_set():
                partial = True
                break
            checked+=1
            if service := future.result():
                services.append(service)
                if service['source']==SOURCE:models.extend(service['rows'])
            publish('services')
    except FuturesTimeout:
        partial = True
    finally:
        pool.shutdown(wait=False, cancel_futures=True)
    if not any(not service.get('authentication_required') for service in services) and start and not (cancel and cancel.is_set()):
        listening = False
        default_base = 'http://127.0.0.1:11434'
        try:
            with socket.create_connection(('127.0.0.1', 11434), timeout=.2):
                listening = True
        except OSError:
            pass
        if not listening:
            started = _start(root)
            start_deadline = min(deadline,time.monotonic()+(5 if _installed() else 0))
            while time.monotonic() < start_deadline and not (cancel and cancel.is_set()):
                rows = _tags(default_base)
                if rows is not None:
                    services.append({'base_url': default_base, 'source': 'Ollama', 'rows': rows})
                    break
                time.sleep(.2)
    candidates = {}
    api_services = []
    for service in services:
        base, rows = service['base_url'], service['rows']
        if not rows or service.get('authentication_required'):
            continue
        if service['source']==SOURCE:continue
        if service['source'] != 'Ollama':
            api_services.append(service)
            continue
        for row in rows:
            name = (row.get('name') or row.get('model')) if isinstance(row, dict) else None
            if isinstance(name, str):
                candidates.setdefault((urlsplit(base).port, name), (base, row))
    if candidates and not stopped():
        pool = ThreadPoolExecutor(max_workers=4)
        pending = [pool.submit(_describe, base, row) for base, row in candidates.values()]
        try:
            for future in _completed(pending,min(4,max(0,deadline-time.monotonic())),cancel):
                if cancel is not None and cancel.is_set():
                    partial = True
                    break
                if model := future.result():
                    (models if model['vision'] else unconfirmed).append(model)
                    publish('model_capabilities')
        except FuturesTimeout:
            partial = True
        finally:
            pool.shutdown(wait=False, cancel_futures=True)
    if api_services and not stopped():
        pool = ThreadPoolExecutor(max_workers=8)
        pending = [pool.submit(_api_models, service) for service in api_services]
        try:
            for future in _completed(pending,min(4,max(0,deadline-time.monotonic())),cancel):
                if cancel is not None and cancel.is_set():
                    partial = True
                    break
                ready, unknown = future.result()
                models.extend(ready)
                unconfirmed.extend(unknown)
                publish('model_capabilities')
        except FuturesTimeout:
            partial = True
        finally:
            pool.shutdown(wait=False, cancel_futures=True)
    return _catalog_result(models,unconfirmed,services,started,partial or stopped(),checked=checked,total=len(endpoints))


def repair_provider(root, provider, allow_default=False, *, background=False):
    address = urlsplit(provider['base_url'])
    from .local_d1 import discover,native_endpoint,SOURCE
    if native_endpoint({'mode':'local_model',**provider}):
        base=address._replace(path='',query='',fragment='').geturl().rstrip('/')
        model=discover(base,_json)
        if model and model['id']==provider['model']:
            # An explicitly selected native endpoint needs only its own health
            # and contract, not a wait on every unrelated Windows listener.
            return {'models':[{**model,'connections':[model]}], 'candidates':[],
                    'service_connected':True,'started':False,'partial':False,
                    'services':[{'base_url':base,'source':SOURCE}],
                    'provider':{**provider,'base_url':model['base_url'],'protocol':model['protocol']},
                    'message':'D1 原生图片决策接口已连接；实际图片推理尚待验证。'}
    legacy_default = (address.port == 18181 and address.path.rstrip('/') == '/v1/systemone'
                      and provider['model'] == 'd1-3b' and provider['protocol'] == 'decisions')
    eligible = legacy_default or address.port == 11434 or allow_default
    if background:
        from .local_model_inventory import start_scan
        scan=start_scan(root,restart=True)
        data=dict(scan.get('services') or _catalog_result([],[],[],False,True,'services'))
        data.update(pending=scan['running'],scan=scan)
    else:data = catalog(root, start=eligible,preferred=provider['base_url'])
    connections = [connection for model in data['models'] for connection in model.get('connections', [model])]
    matching = sorted([model for model in connections if model['id'] == provider['model']],
                      key=lambda model: model['base_url'].rstrip('/') != provider['base_url'].rstrip('/'))
    selected = (matching[0] if matching else data['models'][0] if data['models'] and (legacy_default or allow_default) else None)
    data['provider'] = None
    native_exact=bool(selected and selected.get('source')==SOURCE
                      and selected['base_url'].rstrip('/')==provider['base_url'].rstrip('/'))
    if (eligible or native_exact) and selected:
        data['provider'] = {**provider, 'model': selected['id'],
                            **{key: selected[key] for key in ('base_url', 'protocol')}}
        data['message'] = ('D1 原生图片决策接口已连接；实际图片推理尚待验证。' if selected.get('source')==SOURCE else
                           f"已连接{selected.get('display_name', '本机看图模型')}，点击开始分析后使用。")
    elif data['models']:
        data['message'] = '已发现其他可用的本地看图模型；保留你指定的服务和模型，可在下拉框中选择。'
    return data
